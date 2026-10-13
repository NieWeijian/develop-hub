"""End-to-end server contracts; fake controller only, never hardware."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_testing_platform import context, fixture_round
from develop_hub import testing_platform as p, special_scenarios as s, release_special as r
from develop_hub import special_catalog as catalog
from develop_hub.models import TestCase as Case, SpecialRun, TestRound as Round, TestRoundStatus as Status, ValidationEvidence, ReleasePlan
from develop_hub.seed_basic_cases import install_missing
from develop_hub.test_workbench import TestRoundWorkflow as RoundWorkflow, TestWorkbenchError as WorkbenchError

@pytest.fixture
def release(context,monkeypatch):
    install_missing(context.db)
    source,candidate,plan,_=fixture_round(context)
    images=[{'filename':name+'.img','size_bytes':10,'sha256':char*64,'local_path':'/unused/'+name+'.img'}
        for name,char in [('oem','a'),('system','b'),('boot','c')]]
    source.image_snapshot=images
    candidate.fingerprint=p.digest(p.image_identity(images))
    candidate.snapshot={**candidate.snapshot,'images':images,'product':{},'build':{'passed':True}}
    plan.requirements=p.templates(context.db);context.db.commit()
    device={'serial':'CPU123','usb_path':'/sys/devices/usb1/1-2','usb_port':'1-2','video':'/dev/video0','version':'old a'}
    async def devices(*args):return [deepcopy(device)]
    async def ready(*args):return {'passed':True}
    import develop_hub.bench_readiness as br
    monkeypatch.setattr(p,'bench_devices',devices);monkeypatch.setattr(br,'inspect_bench',ready)
    monkeypatch.setattr(s,'freeze_package',lambda rid:{**catalog.package_identity(),'archive_sha256':'d'*64})
    monkeypatch.setattr(s,'SessionLocal',context.factory)
    async def start(rid):
        row=context.db.get(Round,rid);row.status=Status.BENCH_RUNNING;context.db.commit()
    context.candidate=candidate;context.plan=plan;context.device=device;context.workflow=SimpleNamespace(start=start)
    return context

def launch(env,code='SPECIAL-SN-POWER-CYCLE'):
    req=next(item for item in env.plan.requirements if item['code']==code)
    data=asyncio.run(p.start_special(env.plan.id,req['id'],p.StartSpecial(revision=env.plan.revision,
        bench_id=env.bench.id,device_serial='CPU123',operator='tester'),env.db,env.workflow))
    return env.db.get(SpecialRun,data['id']),req

def complete_preparation(env,run,boot_version=None):
    rnd=env.db.get(Round,run.context['release_binding']['preparation_round_id'])
    config=RoundWorkflow._config(rnd,rnd.image_snapshot,env.bench)
    digest=hashlib.sha256(json.dumps(config,ensure_ascii=False,indent=2).encode()).hexdigest()
    rnd.generated_config=config
    rnd.delivery_result={'passed':True,'files':[{'filename':'test-run.json','sha256':digest,'size_bytes':1},*[{**item,'filename':'firmware/'+item['filename']} for item in p.image_identity(rnd.image_snapshot)]]}
    case=next(c for c in rnd.test_case_snapshot if c['code']=='BASIC-VERSION')
    rnd.controller_result={'round_id':f'TEST-{rnd.display_number:06d}','config_sha256':digest,'state':'finished',
        'boot_verified':True,'boot_version':boot_version or 'FW_'+rnd.commit_sha[:7]+' b','flash_exit_code':0,
        'device':deepcopy(env.device),'usb_location':'/sys/devices/controller/2','source_slot':'a','target_slot':'b','video':'/dev/video2',
        'flash_partitions':[{'partition':i['filename'][:-4]+'_b','sha256':i['sha256'],'download_confirmed':True,
            'exit_code':251 if i['filename']=='boot.img' else 0} for i in rnd.image_snapshot],
        'cases':[{'id':case['id'],'state':'finished','outcome':'passed','assertions_total':1,'assertions_passed':1}]}
    rnd.status=Status.TEST_COMPLETE;env.db.commit()
    r.refresh_preparation(env.db,run)
    return rnd

def complete_gate(env,run,variant=None):
    if variant:run.context={**run.context,'parameters':{**run.context['parameters'],'variant':variant}}
    run.steps=[{'step_id':spec['id'],'status':'passed','result':{'outcome':'passed','assertions':[{'passed':True}],'files':[]}} for spec in run.definition_snapshot['steps']]
    run.current_step_index=len(run.steps);run.status='passed';run.bench_reserved=False;env.db.commit()

def requirement(env,req):
    return next(x for x in p.coverage(env.db,env.plan)['requirements'] if x['id']==req['id'])

def test_registry_idempotent_and_only_two_pending(context):
    added=install_missing(context.db);context.db.commit()
    ids={c.code:c.id for c in context.db.scalars(select(Case))}
    assert set(catalog.CODES)<=set(added) and len(ids)==30
    assert install_missing(context.db)==[]
    assert ids=={c.code:c.id for c in context.db.scalars(select(Case))}
    reqs=p.templates(context.db)
    assert len(reqs)==32
    assert {x['code'] for x in reqs if x['mode']=='pending'}=={'SPECIAL-RGB-IMU-ALIGN','SYNC-MULTI'}
    assert len([x for x in reqs if x['mode']=='gated'])==4
    assert all(x['case_id'] and x['adapter']['steps'] for x in reqs if x['mode']=='gated')

def test_sync_preserves_ids_regular_history_and_frozen_plan(release):
    env=release
    original=deepcopy(env.plan.requirements)
    env.plan.requirements=[{**x,'mode':'pending','adapter':None,'case_id':None} if x['code'] in catalog.CODES else x for x in original]
    env.db.commit()
    result=p.sync_special(env.plan.id,p.SyncSpecial(revision=1,operator='tester',reason='接入门控'),env.db)
    assert len(result['updated'])==4 and result['revision']==2
    assert env.plan.requirements==original
    assert len(list(env.db.scalars(select(ValidationEvidence).where(ValidationEvidence.plan_id==env.plan.id))))==4
    assert p.sync_special(env.plan.id,p.SyncSpecial(revision=2,operator='tester',reason='重复'),env.db)['updated']==[]
    env.plan.final_report={'frozen':True};env.db.commit()
    with pytest.raises(HTTPException):p.sync_special(env.plan.id,p.SyncSpecial(revision=2,operator='tester',reason='重试'),env.db)

def test_exclusive_candidate_preparation_and_read_reconciliation(release):
    run,req=launch(release)
    prep=release.db.get(Round,run.context['release_binding']['preparation_round_id'])
    assert run.status=='preparing_firmware' and run.bench_reserved
    assert p.bench_busy(release.db,release.bench.id)
    assert not p.bench_busy(release.db,release.bench.id,prep.id)
    assert requirement(release,req)['status']=='preparing_firmware'
    assert [c['code'] for c in prep.test_case_snapshot]==['BASIC-VERSION']
    with pytest.raises(HTTPException):launch(release,'SPECIAL-LOG-FULL')
    with pytest.raises(HTTPException):asyncio.run(s.advance(run.id,s.Advance(expected_revision=1,step_id='inspect',operator='tester'),release.db,None))
    with pytest.raises(HTTPException):asyncio.run(s.cancel(run.id,s.Cancel(expected_revision=1,operator='tester',reason='stop',hardware_stopped=True),release.db,None))
    complete_preparation(release,run)
    assert run.status=='awaiting_operator' and run.device_snapshot['version'].endswith(' b')
    count=len(run.events);r.refresh_preparation(release.db,run);assert len(run.events)==count
    assert requirement(release,req)['status']=='awaiting_operator'
    complete_gate(release,run)
    current=requirement(release,req)
    assert current['status']=='passed' and current['latest']['special_run_id']==run.id

@pytest.mark.parametrize('fault',['fingerprint','config_digest','cpu','usb','slot','source_slot','partition','boot','delivery','basic'])
def test_preparation_rejects_incomplete_or_wrong_identity(release,fault):
    run,req=launch(release);prep=complete_preparation(release,run)
    report=deepcopy(prep.controller_result)
    if fault=='fingerprint':prep.image_snapshot=[{**prep.image_snapshot[0],'sha256':'e'*64},*prep.image_snapshot[1:]]
    elif fault=='config_digest':report['config_sha256']='e'*64
    elif fault=='cpu':report['device']['serial']='OTHER'
    elif fault=='usb':report['device']['usb_port']='1-3'
    elif fault=='slot':report['target_slot']='a'
    elif fault=='source_slot':report['source_slot']='b'
    elif fault=='partition':report['flash_partitions'][0]['download_confirmed']=False
    elif fault=='boot':report['boot_verified']=False
    elif fault=='delivery':prep.delivery_result={'passed':False}
    else:report['cases'][0]['assertions_passed']=0
    prep.controller_result=report;release.db.commit()
    complete_gate(release,run)
    assert requirement(release,req)['status']=='blocked'

@pytest.mark.parametrize('fault',['old_definition','wrong_plan','device','parameters','assertions','unarchived'])
def test_gate_coverage_requires_exact_binding_and_evidence(release,fault):
    run,req=launch(release);complete_preparation(release,run);complete_gate(release,run)
    if fault=='old_definition':run.definition_snapshot={**run.definition_snapshot,'version':'0.9'}
    elif fault=='wrong_plan':run.context={**run.context,'release_binding':{**run.context['release_binding'],'validation_plan_id':'other'}}
    elif fault=='device':run.device_snapshot={**run.device_snapshot,'serial':'OTHER'}
    elif fault=='parameters':
        changed={**req,'parameters':{'total_mib':48}}
        release.plan.requirements=[changed if x['id']==req['id'] else x for x in release.plan.requirements]
    else:
        steps=deepcopy(run.steps)
        if fault=='assertions':steps[0]['result']['assertions']=[]
        else:steps[0]['result']['files']=[{'archived':False}]
        run.steps=steps
    release.db.commit()
    assert requirement(release,req)['status'] in ('blocked','stale')

def test_ota_needs_all_variants_and_latest_attempt_wins(release):
    for index,variant in enumerate(r.VARIANTS):
        run,req=launch(release,'SPECIAL-OTA');complete_preparation(release,run);complete_gate(release,run,variant)
        assert requirement(release,req)['status']==('passed' if index==2 else 'unmeasured')
    run,req=launch(release,'SPECIAL-OTA')
    assert requirement(release,req)['status']=='preparing_firmware'
    complete_preparation(release,run)
    assert requirement(release,req)['status']=='awaiting_operator'
    run.context={**run.context,'parameters':{'variant':'corrupt'}};run.status='failed';run.bench_reserved=False;release.db.commit()
    coverage=requirement(release,req)
    assert coverage['status']=='failed' and coverage['variant_statuses']=={'corrupt':'failed','wrong_name':'passed','interrupted':'passed'}

def test_unbound_payload_pass_or_ordinary_link_cannot_pass_gate(release):
    req=next(r for r in release.plan.requirements if r['mode']=='gated')
    release.db.add(ValidationEvidence(plan_id=release.plan.id,requirement_id=req['id'],requirement_hash=p.spec_hash(req),payload={'outcome':'passed'}))
    release.db.commit()
    assert requirement(release,req)['status']=='blocked'
    with pytest.raises(HTTPException):p.finalize(release.plan.id,p.Finalize(revision=1,operator='tester'),release.db)
    with pytest.raises(HTTPException):p.link_evidence(release.plan.id,p.LinkEvidence(round_id=release.candidate.source_round_id,requirement_ids=[req['id']]),release.db)

def test_start_failure_and_restart_do_not_leave_created_deadlock(release):
    async def fail(rid):raise WorkbenchError('not dispatched')
    release.workflow=SimpleNamespace(start=fail)
    run,req=launch(release)
    prep=release.db.get(Round,run.context['release_binding']['preparation_round_id'])
    assert prep.status==Status.BLOCKED and run.status=='interrupted' and run.bench_reserved
    response=asyncio.run(s.cancel(run.id,s.Cancel(expected_revision=run.revision,operator='tester',reason='verified stop',hardware_stopped=True),release.db,None))
    assert response['status']=='cancelled' and not response['bench_reserved']
    async def noop(rid):pass
    release.workflow=SimpleNamespace(start=noop)
    run,req=launch(release);prep=release.db.get(Round,run.context['release_binding']['preparation_round_id'])
    assert prep.status==Status.CREATED
    asyncio.run(s.SpecialWorkflow(None).recover());release.db.expire_all()
    assert release.db.get(Round,prep.id).status==Status.BLOCKED
    assert release.db.get(SpecialRun,run.id).status=='interrupted'

def test_stale_get_cannot_resurrect_cancelled_run(release):
    run,_=launch(release);complete_preparation(release,run)
    with release.factory() as other:
        cancelled=other.get(SpecialRun,run.id);cancelled.status='cancelled';cancelled.bench_reserved=False;other.commit()
    r.refresh_preparation(release.db,run)
    assert run.status=='cancelled' and not run.bench_reserved

def test_final_transaction_rechecks_plan_after_async_probe(release,monkeypatch):
    async def race(*args):
        with release.factory() as other:
            plan=other.get(ReleasePlan,release.plan.id);plan.revision+=1;other.commit()
        return [release.device]
    monkeypatch.setattr(p,'bench_devices',race)
    with pytest.raises(HTTPException) as exc:launch(release)
    assert exc.value.status_code==409
    assert not list(release.db.scalars(select(SpecialRun)))


def test_same_requirement_cannot_run_on_two_benches(release):
    from develop_hub.models import TestBench
    run,req=launch(release)
    second=TestBench(name='second',host='never.invalid',user='test',remote_root='/qa')
    release.db.add(second);release.db.commit()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(p.start_special(release.plan.id,req['id'],p.StartSpecial(revision=1,
            bench_id=second.id,device_serial='CPU123',operator='tester'),release.db,release.workflow))
    assert exc.value.status_code==409 and '该清单项' in exc.value.detail
    release.db.rollback()
    assert len(list(release.db.scalars(select(SpecialRun))))==1

def test_final_report_refuses_older_reserved_gate_even_if_newer_passes(release):
    old,req=launch(release);complete_preparation(release,old);complete_gate(release,old)
    latest,_=launch(release);complete_preparation(release,latest);complete_gate(release,latest)
    release.plan.requirements=[req]
    old.status='awaiting_operator';old.bench_reserved=True;release.db.commit()
    assert requirement(release,req)['status']=='passed'
    with pytest.raises(HTTPException) as exc:p.finalize(release.plan.id,p.Finalize(revision=1,operator='tester'),release.db)
    assert '占用测试台' in exc.value.detail

def test_created_preparation_can_be_cancelled_without_dispatch(release):
    async def noop(rid):pass
    release.workflow=SimpleNamespace(start=noop)
    run,_=launch(release)
    result=asyncio.run(s.cancel(run.id,s.Cancel(expected_revision=1,operator='tester',reason='stop before dispatch',hardware_stopped=True),release.db,None))
    assert result['status']=='cancelled' and not result['bench_reserved']
    prep=release.db.get(Round,run.context['release_binding']['preparation_round_id'])
    assert prep.status==Status.BLOCKED

def test_cancel_during_start_does_not_get_overwritten(release):
    async def racing_start(rid):
        prep=release.db.get(Round,rid)
        run=release.db.get(SpecialRun,prep.product_snapshot['special_run_id'])
        await s.cancel(run.id,s.Cancel(expected_revision=1,operator='tester',reason='stop',hardware_stopped=True),release.db,None)
        raise WorkbenchError('Only a newly created Test Round may start')
    release.workflow=SimpleNamespace(start=racing_start)
    run,_=launch(release)
    assert run.status=='cancelled' and not run.bench_reserved


@pytest.mark.parametrize('fault',[None,'policy','path','hash','size','timestamp','missing','invalid_path'])
def test_remote_retained_evidence_is_valid_only_with_frozen_policy_and_verified_metadata(release,fault):
    run,req=launch(release);complete_preparation(release,run);complete_gate(release,run)
    steps=deepcopy(run.steps)
    item={'path':'protocol.jsonl','size_bytes':208000000,'sha256':'a'*64,'archived':False,'remote_only':True,
        'remote_path':run.remote_path+'/steps/00-inspect/protocol.jsonl',
        'remote_verified_at':'2026-10-09T01:00:00+00:00','remote_verified_sha256':'a'*64,
        'remote_verified_size_bytes':208000000}
    if fault=='policy':run.tool_snapshot={k:v for k,v in run.tool_snapshot.items() if k!='evidence_policy'}
    if fault=='path':item['remote_path']='/elsewhere/protocol.jsonl'
    if fault=='hash':item['remote_verified_sha256']='b'*64
    if fault=='size':item['remote_verified_size_bytes']=1
    if fault=='timestamp':item['remote_verified_at']='not a time'
    if fault=='missing':item={}
    if fault=='invalid_path':item['path']={'not':'a string'}
    steps[0]['result']['files']=[item];run.steps=steps;release.db.commit()
    actual=requirement(release,req)['status']
    assert actual=='passed' if fault is None else actual in ('blocked','stale')


@pytest.mark.parametrize('observed,expected_status', [
    ('RK struct_light Multi UVC APP version: factory-V2.0.7-1-gabcdef0 (compile 12:00:00 Oct 9 2026) _b', 'awaiting_operator'),
    ('RK struct_light Multi UVC APP version: factory-V2.0.7-2-gabcdef0 (compile 12:00:00 Oct 9 2026) _b', 'interrupted'),
    ('RK struct_light Multi UVC APP version: factory-V2.0.7-1-gabcdef0 (compile 12:00:00 Oct 9 2026) _a', 'interrupted'),
])
def test_prebuilt_candidate_unlocks_gate_only_after_exact_identity_and_slot(release,observed,expected_status):
    candidate=release.candidate
    candidate.source_round_id=None
    candidate.snapshot={**candidate.snapshot,'commit':'','build':None,
        'board_config':release.product.board_config,'manifest_commit':'',
        'source':{'kind':'prebuilt','image_directory':'/sdk/images','operator':'tester'},
        'identity':{'kind':'version','value':'factory-V2.0.7-1-gabcdef0'}}
    release.db.commit()
    run,req=launch(release)
    prep=complete_preparation(release,run,observed)
    assert prep.build_result is None and prep.test_point_id is None
    assert prep.generated_config['source']['type']=='prebuilt'
    assert prep.generated_config['source']['identity']==candidate.snapshot['identity']
    assert run.status==expected_status
    if expected_status=='awaiting_operator':
        complete_gate(release,run)
        assert requirement(release,req)['status']=='passed'


def test_preparation_rejects_empty_legacy_commit(release):
    run,_=launch(release)
    prep=complete_preparation(release,run)
    release.candidate.snapshot={**release.candidate.snapshot,'commit':''}
    with pytest.raises(ValueError):r.preparation_proof(prep,run,release.candidate)
