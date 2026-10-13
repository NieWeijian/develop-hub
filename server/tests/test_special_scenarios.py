"""Operator gates must survive refresh, stale clicks and uncertain hardware outcomes."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from develop_hub.db import Base, get_db
from develop_hub.models import Repository, SdkRepository, GerritRepository, TestProduct as Product, TestBench as Bench, SpecialRun, TestRound as Round, TestRoundStatus as RoundStatus
from develop_hub import special_scenarios as s
from develop_hub import test_workbench_api as workbench_api
from develop_hub import test_workbench
from develop_hub.testing_platform import bench_busy

PASS=dict(outcome='passed',message='verified',data={},assertions=[{'name':'actual evidence','passed':True}],commands=[],files=[])

@pytest.fixture
def env(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'special.db'),connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(s,'SessionLocal',factory)
    monkeypatch.setattr(test_workbench,'SessionLocal',factory)
    cancellations=[]
    async def marker_remote(bench,command,*args,**kwargs):
        cancellations.append(command)
        return {'exit_code':0,'stdout_tail':'','stderr_tail':''}
    service=s.SpecialWorkflow(SimpleNamespace(_remote=marker_remote))
    starts=[];monkeypatch.setattr(service,'start',lambda rid:starts.append(rid))
    monkeypatch.setattr(s,'freeze_package',lambda rid:{'package_sha256':{'runtime':'frozen'},'archive_sha256':'a'*64})
    async def devices(*args):
        return [{'serial':'abcd1234','usb_port':'1-2','usb_path':'/sys/devices/1-2','video':'/dev/video0','version':'FW-before'}]
    monkeypatch.setattr(workbench_api,'bench_devices',devices)
    with factory() as db:
        repo=Repository(name='repo',root_path='/repo')
        sdk=SdkRepository(name='sdk',root_path='/sdk',artifact_root='/images')
        gerrit=GerritRepository(name='gerrit',project_path='project')
        db.add_all([repo,sdk,gerrit]);db.flush()
        product=Product(code='P',name='Product',sdk_repository_id=sdk.id,tested_repository_id=repo.id,gerrit_repository_id=gerrit.id,gerrit_project='project',board_config='RV1126B')
        bench=Bench(name='Bench',host='never-contact.invalid',user='test',remote_root='/qa')
        db.add_all([product,bench]);db.commit()
        pid,bid=product.id,bench.id
    application=FastAPI();application.include_router(s.router);application.state.special_workflow=service
    def database():
        with factory() as db:yield db
    application.dependency_overrides[get_db]=database
    with TestClient(application) as client:
        yield SimpleNamespace(client=client,factory=factory,service=service,starts=starts,pid=pid,bid=bid,cancellations=cancellations)
    engine.dispose()

def create(env,code='SPECIAL-SN-POWER-CYCLE'):
    response=env.client.post('/api/qa/special-runs',json={'scenario_code':code,'product_id':env.pid,'bench_id':env.bid,'device_serial':'abcd1234','operator':'tester'})
    assert response.status_code==201,response.text
    return response.json()
def advance(env,row,values=None,**overrides):
    return env.client.post('/api/qa/special-runs/'+row['id']+'/advance',json={'expected_revision':row['revision'],'step_id':row['current_step_id'],'operator':'tester','values':values or {},**overrides})
def refresh(env,row):
    return env.client.get('/api/qa/special-runs/'+row['id']).json()
def finish(env,row,result=PASS):
    env.service.finish(row['id'],deepcopy(result));return refresh(env,row)

@pytest.mark.parametrize('code',['SPECIAL-SN-POWER-CYCLE','SPECIAL-LOG-FULL','FACTORY-AGING','SPECIAL-OTA'])
def test_definitions_create_freeze_and_report(env,monkeypatch,code):
    async def forbidden(*args,**kwargs):
        pytest.fail('Creating a current-firmware gate must not dispatch firmware or hardware commands')
    monkeypatch.setattr(env.service.workflow,'start',forbidden,raising=False)
    monkeypatch.setattr(env.service.workflow,'_remote',forbidden)
    defs=env.client.get('/api/qa/special-scenarios').json()
    assert len(defs)==4
    row=create(env,code)
    assert row['scenario_code']==code
    assert row['status']=='awaiting_operator' and row['current_step_id']=='inspect'
    assert row['device_snapshot']['serial']=='abcd1234'
    assert row['tool_snapshot']['package_sha256']=={'runtime':'frozen'}
    with env.factory() as db:
        assert bench_busy(db,env.bid)
        assert not list(db.scalars(select(Round)))
    assert not row['context'].get('release_binding')
    response=env.client.get('/api/qa/special-runs/'+row['id']+'/report')
    assert response.status_code==200 and 'attachment' in response.headers['content-disposition']
    assert response.json()['definition_snapshot']==row['definition_snapshot']
    assert env.starts==[]

def test_stale_duplicate_and_out_of_order_never_repeat_hardware(env):
    initial=create(env)
    assert advance(env,initial,step_id='verify_sn').status_code==409
    started=advance(env,initial)
    assert started.status_code==200 and started.json()['status']=='running'
    duplicate=advance(env,initial)
    assert duplicate.status_code==200 and len(env.starts)==1
    assert advance(env,initial,operator='different').status_code==409
    done=finish(env,initial)
    assert done['current_step_id']=='set_sn' and done['status']=='awaiting_operator'
    assert advance(env,initial).status_code==200 and len(env.starts)==1
    assert advance(env,done,{'target_sn':'x'*57,'confirmed':True}).status_code==422
    assert advance(env,done,{'target_sn':'SN-001','confirmed':False}).status_code==422
    assert advance(env,done,{'target_sn':'SN-001','confirmed':True,'shell':'rm'}).status_code==422
    assert advance(env,done,{'target_sn':'SN-001','confirmed':True}).status_code==200

def test_complete_sn_sequence_records_all_gates(env):
    row=create(env)
    for values in ({},{'target_sn':'TEST-123','confirmed':True},{'confirmed':True},{'confirmed':True}):
        response=advance(env,row,values);assert response.status_code==200,response.text
        row=finish(env,row)
    assert row['current_step_id']=='finish'
    row=advance(env,row,{'confirmed':True}).json()
    assert row['status']=='passed' and not row['bench_reserved']
    assert all(step['status']=='passed' for step in row['steps'])
    assert all(step['operator']=='tester' and step['started_at'] and step['finished_at'] for step in row['steps'])
    assert row['context']['parameters']['target_sn']=='TEST-123'

@pytest.mark.parametrize('result',[
    {'outcome':'passed','assertions':[]},
    {'outcome':'passed','assertions':[{'name':'missing','passed':False}]},
    {'outcome':'unknown'},
    {'outcome':'blocked','message':'missing rolling evidence'},
    {'outcome':'failed','message':'SN changed'},
])
def test_unknown_failed_or_missing_evidence_cannot_pass(env,result):
    row=create(env);advance(env,row)
    row=finish(env,row,result)
    assert row['status'] in ('blocked','failed') and row['current_step_id']=='inspect'
    assert advance(env,row).status_code==409

def test_restart_retains_reservation_and_never_replays(env):
    row=create(env);advance(env,row)
    asyncio.run(env.service.recover())
    row=refresh(env,row)
    assert row['status']=='interrupted' and row['bench_reserved']
    assert len(env.starts)==1
    assert env.client.post('/api/qa/special-runs',json={'scenario_code':'SPECIAL-OTA','product_id':env.pid,'bench_id':env.bid,'device_serial':'abcd1234','operator':'tester'}).status_code==409
    cancel={'expected_revision':row['revision'],'operator':'tester','reason':'checked hardware'}
    assert env.client.post('/api/qa/special-runs/'+row['id']+'/cancel',json=cancel).status_code==422
    response=env.client.post('/api/qa/special-runs/'+row['id']+'/cancel',json={**cancel,'hardware_stopped':True})
    assert response.json()['status']=='cancelled' and not response.json()['bench_reserved']

def test_cancel_running_waits_for_current_command(env):
    row=create(env);row=advance(env,row).json()
    response=env.client.post('/api/qa/special-runs/'+row['id']+'/cancel',json={'expected_revision':row['revision'],'operator':'tester','reason':'stop'})
    assert response.json()['status']=='cancelling' and response.json()['bench_reserved']
    row=finish(env,row)
    assert row['status']=='cancelled' and not row['bench_reserved']

def test_cancel_retains_active_remote_ota_until_operator_verifies_stop(env):
    row=create(env,'SPECIAL-OTA')
    row=advance(env,row).json();row=finish(env,row)
    row=advance(env,row,{'variant':'corrupt','package_path':'/tmp/update.tar.gz','expected_sha256':'a'*64,'confirmed':True}).json()
    assert row['current_step_id']=='ota_prepare'
    row=advance(env,row).json();row=finish(env,row)
    assert row['hardware_active']
    cancel={'expected_revision':row['revision'],'operator':'tester','reason':'stop'}
    assert env.client.post('/api/qa/special-runs/'+row['id']+'/cancel',json=cancel).status_code==422
    assert refresh(env,row)['bench_reserved']

def test_ordinary_round_cannot_start_on_reserved_bench(env):
    create(env)
    with env.factory() as db:
        row=Round(display_number=1,product_id=env.pid,bench_id=env.bid,product_snapshot={},bench_snapshot={},
            manifest_commit='',board_config='RV1126B',gerrit_project='',change_number=0,patchset_number=0,commit_sha='',work_branch='',status=RoundStatus.CREATED)
        db.add(row);db.commit();rid=row.id
    workflow=test_workbench.TestRoundWorkflow(s.settings)
    with pytest.raises(test_workbench.TestWorkbenchError,match='人工'):
        asyncio.run(workflow.start(rid))
    with env.factory() as db:assert db.get(Round,rid).status==RoundStatus.CREATED

def test_aging_validation_requires_real_manual_logs_and_valid_times(env):
    row=create(env,'FACTORY-AGING')
    for _ in range(2):
        advance(env,row);row=finish(env,row)
    values={'capture_mode':'manual','baud_rate':115200,'duration_seconds':14400,'capture_timeout_seconds':15000,
        'max_temperature_c':65,'aging_reset_command':'uvc_host_ctl set-ft-aging -2','confirmed':True}
    assert advance(env,row,{**values,'capture_timeout_seconds':120}).status_code==422
    row=advance(env,row,values).json()
    advance(env,row);row=finish(env,row)
    assert row['current_step_id']=='aging_verify'
    assert advance(env,row,{'confirmed':True}).status_code==422
    assert advance(env,row,{'confirmed':True,'manual_log':'raw','started_at':'2026-10-08','ended_at':'2026-10-09'}).status_code==422

def test_creation_rejects_wrong_product_and_missing_port(env):
    with env.factory() as db:
        product=db.get(Product,env.pid);product.board_config='RK3572';db.commit()
    response=env.client.post('/api/qa/special-runs',json={'scenario_code':'SPECIAL-OTA','product_id':env.pid,'bench_id':env.bid,'device_serial':'abcd1234','operator':'tester'})
    assert response.status_code==422


@pytest.mark.parametrize('upload_mode',[False,True])
@pytest.mark.parametrize('remote_retained',[False,True])
@pytest.mark.parametrize('archive_after_failure',[False,True])
def test_remote_transport_freezes_requests_and_archives_complete_files(env,tmp_path,monkeypatch,upload_mode,remote_retained,archive_after_failure):
    import json
    import shutil
    import tarfile
    from pathlib import Path
    row=create(env)
    artifact_root=tmp_path/'artifacts'
    monkeypatch.setattr(s,'settings',SimpleNamespace(artifact_root=artifact_root))
    local=artifact_root/'special-runs'/row['id']
    package=local/'package';package.mkdir(parents=True)
    runtime="""import argparse,json,pathlib,hashlib
p=argparse.ArgumentParser();p.add_argument('--request');p.add_argument('--output');a=p.parse_args()
r=json.loads(pathlib.Path(a.request).read_text())
if r['parameters'].get('manual_log_path'):
    source=pathlib.Path(r['parameters']['manual_log_path'])
    assert source.parent==pathlib.Path(a.output).parent
    assert source.stat().st_size==800000 and hashlib.sha256(source.read_bytes()).hexdigest()==r['parameters']['manual_log_sha256']
else:assert len(r['parameters']['manual_log'])==300000
out=pathlib.Path(a.output);f=out.parent/'raw.log';f.write_bytes(b'x'*40000)
out.write_text(json.dumps({'outcome':'failed' if r['parameters'].get('simulate_write_failure') else 'passed','message':'write failed' if r['parameters'].get('simulate_write_failure') else 'captured','data':{},'assertions':[{'name':'complete','passed':True}],'commands':[],'files':[{'path':'raw.log','sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'size_bytes':40000}]}))
"""
    (package/'special_runtime.py').write_text(runtime);(package/'tool').write_text('frozen')
    with tarfile.open(local/'package.tar','w') as handle:handle.add(package,arcname='package')
    snapshot=deepcopy(row)
    snapshot['remote_path']=str(tmp_path/'remote')
    snapshot['context']={'parameters':{'manual_log':'a'*300000},'results':{}}
    if upload_mode:
        uploads=local/'manual-uploads';uploads.mkdir()
        source=uploads/'opaque-id.log';source.write_bytes(b'a'*800000)
        snapshot['context']={'parameters':{'manual_log_artifact_id':'opaque-id'},'results':{},
            'manual_uploads':{'opaque-id':{'sha256':s.sha(source),'size_bytes':800000}}}
    snapshot['tool_snapshot']={'archive_sha256':s.sha(local/'package.tar'),
        'package_sha256':{name:s.sha(package/name) for name in ('special_runtime.py','tool')},
        'tools_package':{'binary_path':'tool','binary_sha256':s.sha(package/'tool')}}
    if remote_retained:snapshot['tool_snapshot']['evidence_policy']=deepcopy(s.EVIDENCE_POLICY)
    if archive_after_failure:
        snapshot['context']['parameters']['simulate_write_failure']=True
        async def archive_failure(*args):raise RuntimeError('remote hash or network check failed')
        monkeypatch.setattr(env.service,'archive_files',archive_failure)
    copies=[]
    real_command=s.run_command
    async def fake_command(command,cwd,timeout,**kwargs):
        if command[0]=='scp':
            copies.append(command[-2:])
            source,target=command[-2:]
            if source.startswith('user@host:'):source=source.split(':',1)[1]
            if target.startswith('user@host:'):target=target.split(':',1)[1]
            shutil.copyfile(source,target)
            return {'exit_code':0,'stdout_tail':'','stderr_tail':''}
        return await real_command(command,cwd,timeout,**kwargs)
    monkeypatch.setattr(s,'run_command',fake_command)
    class LocalWorkflow:
        def _ssh_base(self,bench):return ['-p','22']
        async def _remote(self,bench,command,timeout=120,**kwargs):
            return await real_command(['bash','-c',command],tmp_path,timeout,**kwargs)
    env.service.workflow=LocalWorkflow()
    result=asyncio.run(env.service.run_remote(SimpleNamespace(user='user',host='host'),snapshot,{'id':'inspect','operation':'aging_verify' if upload_mode else 'inspect'}))
    if archive_after_failure:
        assert result['outcome']=='failed' and result['message'].startswith('write failed；原始证据校验')
        assert result['data']['archive_error']=='remote hash or network check failed'
        advance(env,row);env.service.finish(row['id'],result)
        assert refresh(env,row)['status']=='failed'
        return
    assert result['outcome']=='passed',result
    item=result['files'][0]
    assert item['size_bytes']==40000
    if remote_retained:
        assert item['archived'] is False and item['remote_only'] is True
        assert item['remote_verified_sha256']==item['sha256']
        assert item['remote_verified_size_bytes']==40000 and item['remote_verified_at']
        assert item['remote_path']==snapshot['remote_path']+'/steps/00-inspect/raw.log'
        assert not (local/'steps/inspect/files/raw.log').exists()
        assert not any('/raw.log' in origin for origin,destination in copies)
        assert 'download_url' not in item
    else:
        assert item['archived']
        assert (local/'steps/inspect/files/raw.log').stat().st_size==40000
    request_parameters=json.loads((local/'steps/inspect/request.json').read_text())['parameters']
    if upload_mode:
        assert request_parameters['manual_log_path'].endswith('/input.log')
        assert request_parameters['manual_log_sha256']==snapshot['context']['manual_uploads']['opaque-id']['sha256']
    else:assert request_parameters==snapshot['context']['parameters']
    advance(env,row);env.service.finish(row['id'],result)
    if not remote_retained:
        response=env.client.get(item['download_url'])
        assert response.status_code==200 and response.content==b'x'*40000
        (local/'steps/inspect/files/raw.log').write_bytes(b'tampered')
        assert env.client.get(item['download_url']).status_code==409

def test_artifact_path_traversal_is_rejected(env,tmp_path):
    result={'files':[{'path':'../../outside','sha256':'a'*64,'size_bytes':10}]}
    with pytest.raises(RuntimeError,match='路径越界'):
        asyncio.run(env.service.archive_files(None,{}, {'id':'capture'},result,'/remote',tmp_path,[]))

def test_capture_progress_is_ready_signal_without_advancing_gate(env):
    import json
    row=create(env);row=advance(env,row).json()
    class ProgressWorkflow:
        async def _remote(self,bench,command,timeout=120,**kwargs):
            if command=='capture':
                await asyncio.sleep(2.2)
                return {'exit_code':0,'stdout_tail':'','stderr_tail':''}
            return {'exit_code':0,'stdout':json.dumps({'capture_ready':True,'capture_started_at':'2026-10-08T10:00:00+08:00','hardware_active':True})}
    env.service.workflow=ProgressWorkflow()
    asyncio.run(env.service.invoke_with_progress(None,row,{'operation':'aging_capture'},'capture',30,'/remote'))
    current=refresh(env,row)
    assert current['revision']==row['revision'] and current['status']=='running'
    assert current['steps'][0]['progress']['capture_ready']
    assert current['hardware_active'] and current['bench_reserved']


def manual_verify_gate(env):
    row=create(env,'FACTORY-AGING')
    for _ in range(2):
        advance(env,row);row=finish(env,row)
    row=advance(env,row,{'capture_mode':'manual','baud_rate':115200,'duration_seconds':14400,
        'capture_timeout_seconds':15000,'max_temperature_c':70,
        'aging_reset_command':'set-ft-aging -2','confirmed':True}).json()
    advance(env,row);return finish(env,row)

def test_manual_file_upload_is_scoped_hashed_downloadable_and_used_as_opaque_id(env):
    import hashlib
    row=manual_verify_gate(env)
    body=b'raw aging log\n'*60000
    response=env.client.post('/api/qa/special-runs/'+row['id']+'/manual-log',
        params={'expected_revision':row['revision'],'operator':'tester'},content=body,
        headers={'content-type':'application/octet-stream','x-filename':'aging%20full.log'})
    assert response.status_code==201,response.text
    item=response.json()
    assert item['filename']=='aging full.log' and item['size_bytes']==len(body)
    assert item['sha256']==hashlib.sha256(body).hexdigest()
    assert env.client.get(item['download_url']).content==body
    current=refresh(env,row)
    assert current['revision']==row['revision'] and item['artifact_id'] in current['context']['manual_uploads']
    values={'confirmed':True,'manual_log_artifact_id':item['artifact_id'],
        'started_at':'2026-10-08T10:00:00+08:00','ended_at':'2026-10-08T14:00:01+08:00'}
    assert advance(env,current,{**values,'manual_log_artifact_id':'not-owned'}).status_code==422
    assert advance(env,current,{**values,'manual_log':'also pasted'}).status_code==422
    assert advance(env,current,{**values,'manual_log_path':'/etc/passwd'}).status_code==422
    assert advance(env,current,values).status_code==200
    assert env.client.post('/api/qa/special-runs/'+row['id']+'/manual-log',
        params={'expected_revision':row['revision'],'operator':'tester'},content=b'late').status_code==409

def test_manual_upload_rejects_wrong_gate_empty_and_oversize(env):
    row=create(env)
    url='/api/qa/special-runs/'+row['id']+'/manual-log'
    params={'expected_revision':row['revision'],'operator':'tester'}
    assert env.client.post(url,params=params,content=b'wrong-gate').status_code==409
    env.client.post('/api/qa/special-runs/'+row['id']+'/cancel',json={'expected_revision':row['revision'],'operator':'tester','reason':'next'})
    row=manual_verify_gate(env);url='/api/qa/special-runs/'+row['id']+'/manual-log'
    params['expected_revision']=row['revision']
    assert env.client.post(url,params=params,content=b'').status_code==422
    # Streaming a boundary overflow rejects before publishing attachment metadata.
    def chunks():
        for _ in range(65):yield b'x'*(1024*1024)
    assert env.client.post(url,params=params,content=chunks()).status_code==413
    assert not refresh(env,row)['context'].get('manual_uploads')

def test_cancel_marker_stops_cooperative_runtime_but_retains_physical_aging(env):
    row=create(env);row=advance(env,row).json()
    response=env.client.post('/api/qa/special-runs/'+row['id']+'/cancel',
        json={'expected_revision':row['revision'],'operator':'tester','reason':'stop now'})
    assert response.json()['status']=='cancelling'
    assert env.cancellations and 'CANCEL_REQUESTED' in env.cancellations[-1]
    row=finish(env,row,{'outcome':'blocked','message':'capture cancelled','assertions':[],'data':{'hardware_active':True}})
    assert row['status']=='interrupted' and row['bench_reserved']
    response=env.client.post('/api/qa/special-runs/'+row['id']+'/cancel',
        json={'expected_revision':row['revision'],'operator':'tester','reason':'fixture and remote stopped','hardware_stopped':True})
    assert response.json()['status']=='cancelled' and not response.json()['bench_reserved']

def test_cancel_delivery_failure_never_releases_bench(env):
    row=create(env);row=advance(env,row).json()
    async def offline(*args,**kwargs):raise OSError('unreachable')
    env.service.workflow=SimpleNamespace(_remote=offline)
    response=env.client.post('/api/qa/special-runs/'+row['id']+'/cancel',
        json={'expected_revision':row['revision'],'operator':'tester','reason':'stop'})
    assert response.json()['status']=='interrupted' and response.json()['bench_reserved']


def test_remote_retained_evidence_must_pass_remote_hash_check(env,tmp_path):
    async def changed(*args,**kwargs):return {'exit_code':1}
    env.service.workflow=SimpleNamespace(_remote=changed)
    item={'path':'protocol.jsonl','sha256':'a'*64,'size_bytes':123}
    snapshot={'tool_snapshot':{'evidence_policy':s.EVIDENCE_POLICY}}
    with pytest.raises(RuntimeError,match='远端原始证据校验失败'):
        asyncio.run(env.service.archive_files(None,snapshot,{'id':'capture'},{'files':[item]},'/remote',tmp_path,[]))
    assert not item.get('remote_only') and not item.get('archived')


def correction_fixture(env,tmp_path,monkeypatch):
    import json
    row=create(env,'SPECIAL-LOG-FULL')
    monkeypatch.setattr(s,'settings',SimpleNamespace(artifact_root=tmp_path))
    response={'command':['tool','--device','/dev/video0','file-put','payload','/userdata/log/qa.log'],
        'exit_code':1,'stderr':'file-put chunk failed: write failed\n'}
    original={'outcome':'passed','assertions':[{'name':'Health','passed':True}],
        'commands':[response],'data':{'successful_bytes':7800000,'target_bytes':25165824,
            'errors':[{'name':'qa.log','response':response}]},'files':[]}
    source=tmp_path/'special-runs'/row['id']/'steps'/'log_pressure'/'runtime-result.json'
    source.parent.mkdir(parents=True);source.write_text(json.dumps(original))
    with env.factory() as db:
        run=db.get(SpecialRun,row['id']);run.status='blocked';run.bench_reserved=False
        steps=deepcopy(run.steps)
        for record in steps:
            if record['step_id']=='log_pressure':record.update(status='passed',result=deepcopy(original))
            if record['step_id']=='log_verify':record.update(status='blocked',result={'outcome':'blocked','message':'Did not complete >20 MiB','data':{},'assertions':[]})
        run.steps=steps;run.current_step_index=3;db.commit()
    return refresh(env,row),source

def test_log_failure_reclassification_is_audited_idempotent_and_preserves_sources(env,tmp_path,monkeypatch):
    row,source=correction_fixture(env,tmp_path,monkeypatch)
    source_bytes=source.read_bytes();frozen=deepcopy(row['definition_snapshot'])
    url='/api/qa/special-runs/'+row['id']+'/correct-log-write-failure'
    payload={'expected_revision':row['revision'],'operator':'tester','reason':'修正写入失败错误分类'}
    response=env.client.post(url,json=payload)
    assert response.status_code==200,response.text
    fixed=response.json()
    assert fixed['status']=='failed' and fixed['revision']==row['revision']+1 and not fixed['bench_reserved']
    assert fixed['definition_snapshot']==frozen and source.read_bytes()==source_bytes
    assert all(x['status']=='failed' for x in fixed['steps'] if x['step_id'] in ('log_pressure','log_verify'))
    audit=fixed['events'][-1];assert audit['type']=='result_reclassified'
    assert audit['details']['before']['status']=='blocked' and audit['details']['source_sha256']==s.sha(source)
    assert env.client.post(url,json=payload).json()==fixed
    assert not env.starts

@pytest.mark.parametrize('fault',['source_changed','no_write_error','active','reserved','wrong_scenario'])
def test_log_failure_correction_rejects_unproven_or_active_runs(env,tmp_path,monkeypatch,fault):
    import json
    row,source=correction_fixture(env,tmp_path,monkeypatch)
    if fault=='source_changed':source.write_text('{}')
    with env.factory() as db:
        run=db.get(SpecialRun,row['id'])
        if fault=='no_write_error':
            data=json.loads(source.read_text());data['commands']=[];source.write_text(json.dumps(data))
        if fault=='active':run.status='running'
        if fault=='reserved':run.bench_reserved=True
        if fault=='wrong_scenario':run.scenario_code='SPECIAL-OTA'
        db.commit()
    response=env.client.post('/api/qa/special-runs/'+row['id']+'/correct-log-write-failure',json={
        'expected_revision':row['revision'],'operator':'tester','reason':'recheck'})
    assert response.status_code==409
    assert not any(x['type']=='result_reclassified' for x in refresh(env,row)['events'])
