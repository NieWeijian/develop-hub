"""Prebuilt directory import and release execution without fake Git/build evidence."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_testing_platform import context
from develop_hub import prebuilt_candidates as imports
from develop_hub import testing_platform as platform
from develop_hub.bench_readiness import BOARD
from develop_hub.db import get_db
from develop_hub.models import ReleaseCandidate, ReleasePlan, TestRound as Round, TestCase as Case
from develop_hub.test_workbench import TestRoundWorkflow as RoundWorkflow


@pytest.fixture
def images(context, monkeypatch):
    context.product.board_config = BOARD
    root = Path(context.product.sdk_repository.root_path)
    directory = root/'output/image'
    directory.mkdir(parents=True)
    for name in imports.FILENAMES:
        (directory/name).write_bytes(('existing SDK image '+name).encode())
    context.product.sdk_repository.artifact_root = str(directory)
    context.db.commit()
    artifacts = context.tmp/'artifacts'
    monkeypatch.setattr(imports, 'settings', SimpleNamespace(artifact_root=artifacts))
    return SimpleNamespace(ctx=context, root=root, directory=directory, artifacts=artifacts)


def payload(images, **changes):
    return imports.PrebuiltCreate(**({'product_id':images.ctx.product.id, 'name':'Local RC',
        'image_directory':str(images.directory), 'operator':'tester',
        'identity':{'kind':'version','value':'factory-V2.0.7-local'}} | changes))


def test_import_has_real_checksums_and_no_fabricated_round_or_build(images):
    result=imports.create(payload(images), images.ctx.db)
    candidate=images.ctx.db.get(ReleaseCandidate,result['id'])
    assert candidate.source_round_id is None
    assert candidate.snapshot['commit']=='' and candidate.snapshot['build'] is None
    assert candidate.snapshot['source']['kind']=='prebuilt'
    assert candidate.snapshot['source']['validation']['passed'] is True
    assert len(candidate.snapshot['source']['validation']['files'])==3
    assert candidate.snapshot['product']['firmware_identity']==candidate.snapshot['identity']
    assert not list(images.ctx.db.scalars(select(Round)))
    for image in candidate.snapshot['images']:
        frozen=Path(image['local_path'])
        assert frozen.read_bytes()==(images.directory/image['filename']).read_bytes()
        assert platform.sha256_file(frozen)==image['sha256']
    (images.directory/'oem.img').write_bytes(b'next build')
    assert platform.sha256_file(Path(candidate.snapshot['images'][0]['local_path']))==candidate.snapshot['images'][0]['sha256']


def test_options_and_import_api_without_hardware_or_build(images):
    app=FastAPI();app.include_router(platform.router)
    app.dependency_overrides[get_db]=lambda:images.ctx.db
    with TestClient(app) as client:
        options=client.get('/api/qa/platform/prebuilt-options',params={'product_id':images.ctx.product.id})
        assert options.status_code==200
        assert options.json()['allowed_root']==str(images.root)
        assert options.json()['recommended_image_directory']==str(images.directory)
        response=client.post('/api/qa/platform/candidates/prebuilt',json=payload(images).model_dump())
        assert response.status_code==200,response.text
        candidates=client.get('/api/qa/platform/candidates').json()
        assert candidates[0]['source_round_id'] is None
        assert candidates[0]['source']['operator']=='tester'
        assert candidates[0]['identity']['kind']=='version'


@pytest.mark.parametrize('identity', [
    {'kind':'commit','value':''}, {'kind':'commit','value':'abc'}, {'kind':'commit','value':'not-hexadecimal'},
    {'kind':'version','value':''}, {'kind':'version','value':'_a'}, {'kind':'unknown','value':'example'}])
def test_invalid_identity_rejected_without_candidate(images, identity):
    with pytest.raises(HTTPException) as error:
        imports.create(payload(images,identity=identity),images.ctx.db)
    assert error.value.status_code==422
    assert not list(images.ctx.db.scalars(select(ReleaseCandidate)))


@pytest.mark.parametrize('problem',['missing','empty','outside_directory','symlink_escape','relative'])
def test_import_rejects_invalid_images_and_paths(images,problem):
    directory=images.directory
    if problem=='missing':(directory/'boot.img').unlink()
    elif problem=='empty':(directory/'boot.img').write_bytes(b'')
    elif problem=='relative':directory=Path('output/image')
    elif problem=='outside_directory':directory=images.ctx.tmp
    else:
        outside=images.ctx.tmp/'outside.img';outside.write_bytes(b'outside')
        (directory/'boot.img').unlink();(directory/'boot.img').symlink_to(outside)
    with pytest.raises(HTTPException) as error:
        imports.create(payload(images,image_directory=str(directory)),images.ctx.db)
    assert error.value.status_code==422
    assert not list(images.ctx.db.scalars(select(ReleaseCandidate)))
    assert not list((images.artifacts/'release-candidates').glob('*'))


def test_symlinks_to_images_within_sdk_are_supported(images):
    source=images.root/'built-oem.img'
    (images.directory/'oem.img').rename(source)
    (images.directory/'oem.img').symlink_to(source)
    result=imports.create(payload(images),images.ctx.db)
    assert images.ctx.db.get(ReleaseCandidate,result['id']).source_round_id is None


def test_changes_to_earlier_image_during_later_copy_abort_entire_import(images,monkeypatch):
    original=imports.freeze_image
    def mutate(path,before,target):
        result=original(path,before,target)
        if target.name=='boot.img':(images.directory/'oem.img').write_bytes(b'new build after first file copied')
        return result
    monkeypatch.setattr(imports,'freeze_image',mutate)
    with pytest.raises(HTTPException) as error:
        imports.create(payload(images),images.ctx.db)
    assert error.value.status_code==409
    assert not list(images.ctx.db.scalars(select(ReleaseCandidate)))
    assert not list((images.artifacts/'release-candidates').glob('*'))


def test_duplicate_name_does_not_overwrite_frozen_images(images):
    result=imports.create(payload(images),images.ctx.db)
    with pytest.raises(HTTPException) as error:
        imports.create(payload(images),images.ctx.db)
    assert error.value.status_code==409
    assert len(list((images.artifacts/'release-candidates').glob('*')))==1
    assert images.ctx.db.get(ReleaseCandidate,result['id'])


def test_import_commit_is_explicit_and_not_a_fabricated_full_sha(images):
    result=imports.create(payload(images,identity={'kind':'commit','value':'abc1234'}),images.ctx.db)
    candidate=images.ctx.db.get(ReleaseCandidate,result['id'])
    assert candidate.snapshot['commit']=='abc1234' and candidate.snapshot['build'] is None


def test_prebuilt_execute_and_deliver_without_source_round(images,monkeypatch):
    ctx=images.ctx
    cid=imports.create(payload(images),ctx.db)['id']
    candidate=ctx.db.get(ReleaseCandidate,cid)
    case=Case(code='BASIC-VERSION',name='Version',description='fixture',command='fixture')
    ctx.db.add(case);ctx.db.flush()
    adapter={'id':'BASIC-VERSION','version':'fixture'}
    requirement={'id':'requirement','code':case.code,'name':case.name,'mode':'automatic','case_id':case.id,
                 'adapter':adapter,'parameters':{},'required':True,'group':'基础读写'}
    plan=ReleasePlan(candidate_id=cid,name='prebuilt test',requirements=[requirement])
    ctx.db.add(plan);ctx.db.commit()
    monkeypatch.setattr(platform,'definition',lambda code:adapter)
    monkeypatch.setattr(platform,'validate_parameters',lambda *args:None)
    async def devices(*args):return [{'serial':'CPU123','usb_path':'1-1'}]
    async def ready(*args):return {'passed':True}
    monkeypatch.setattr(platform,'bench_devices',devices)
    import develop_hub.bench_readiness as readiness
    monkeypatch.setattr(readiness,'inspect_bench',ready)
    started=[]
    async def start(rid):started.append(rid)
    result=asyncio.run(platform.execute_plan(plan.id,platform.ExecutePlan(requirement_ids=['requirement'],
        revision=1,bench_id=ctx.bench.id,device_serial='CPU123'),ctx.db,SimpleNamespace(start=start)))
    row=ctx.db.get(Round,result['round_id'])
    assert row.test_point_id is None and row.commit_sha=='' and row.build_result is None
    assert row.gerrit_project=='' and row.change_number==0 and row.patchset_number==0
    assert row.image_snapshot==candidate.snapshot['images']
    assert row.product_snapshot['firmware_source']['validation']['passed']
    config=RoundWorkflow._config(row,row.image_snapshot,ctx.bench)
    assert config['source']['type']=='prebuilt'
    assert config['source']['identity']==candidate.snapshot['identity']
    assert config['build'] is None and config['execution_boundary']=='firmware_flash'
    assert started==[row.id]
    events=[]
    async def transfer(rnd,bench,path,artifacts):
        assert artifacts==candidate.snapshot['images']
        assert 'prebuilt' in path.read_text()
        events.append('transfer');return '/qa/prebuilt'
    async def verify(*args):events.append('verify');return {'passed':True,'files':[]}
    async def dispatch(rid):events.append('dispatch')
    workflow=SimpleNamespace(settings=SimpleNamespace(artifact_root=images.artifacts),_config=RoundWorkflow._config,
        _transfer=transfer,_verify_remote=verify,_dispatch_bench=dispatch)
    asyncio.run(platform.dispatch_candidate(workflow,row.id))
    assert events==['transfer','verify','dispatch']


def test_failed_copy_cleans_only_new_candidate_folder(images,monkeypatch):
    original=imports.freeze_image
    def fail(path,before,target):
        if target.name=='system.img':raise OSError('simulated copy failure')
        return original(path,before,target)
    monkeypatch.setattr(imports,'freeze_image',fail)
    with pytest.raises(HTTPException) as error:
        imports.create(payload(images),images.ctx.db)
    assert error.value.status_code==422
    assert not list(images.ctx.db.scalars(select(ReleaseCandidate)))
    assert not list((images.artifacts/'release-candidates').glob('*'))


def test_active_sdk_build_prevents_import(images):
    from test_testing_platform import fixture_round
    from develop_hub.models import TestRoundStatus
    row,_,_,_=fixture_round(images.ctx)
    row.status=TestRoundStatus.BUILDING
    row.product_snapshot={'sdk_repository_id':images.ctx.product.sdk_repository_id}
    images.ctx.db.commit()
    with pytest.raises(HTTPException) as error:
        imports.create(payload(images),images.ctx.db)
    assert error.value.status_code==409 and 'SDK' in error.value.detail
    assert not list((images.artifacts/'release-candidates').glob('*'))
