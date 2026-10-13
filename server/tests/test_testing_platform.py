"""Release identity, coverage, delivery and restart-safe scheduling contracts."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from develop_hub.db import Base
from develop_hub.models import (Repository, SdkRepository, GerritRepository, TestProduct as Product,
    TestBench as Bench, TestRound as Round, TestRoundStatus as Status, ReleaseCandidate,
    ReleasePlan, ValidationEvidence, TestSchedule as Schedule, ScheduleRun, new_id)
from develop_hub import testing_platform as p


@pytest.fixture
def context(tmp_path, monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'platform.db'))
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(p,'SessionLocal',factory)
    with factory() as db:
        repo=Repository(name='r',root_path=str(tmp_path/'repo'))
        sdk=SdkRepository(name='s',root_path=str(tmp_path/'sdk'),artifact_root=str(tmp_path/'images'))
        gerrit=GerritRepository(name='g',project_path='linux/app/struct_light')
        db.add_all([repo,sdk,gerrit]);db.flush()
        product=Product(name='product',code='P',sdk_repository_id=sdk.id,tested_repository_id=repo.id,
            gerrit_repository_id=gerrit.id,gerrit_project=gerrit.project_path,board_config='RV1126B')
        bench=Bench(name='bench',host='invalid.test',user='test',remote_root='/qa')
        db.add_all([product,bench]);db.commit()
        yield SimpleNamespace(db=db,factory=factory,product=product,bench=bench,tmp=tmp_path)
    engine.dispose()

def fixture_round(ctx, outcome='passed', images=None):
    number=len(list(ctx.db.scalars(select(Round))))+1
    req={'id':new_id(),'code':'INFO-PING','case_id':'case','name':'Ping','group':'telemetry','mode':'automatic',
         'adapter':{'version':'1','tools':'abc'},'parameters':{},'environment':'standard','procedure':'','required':True}
    row=Round(display_number=number,product_id=ctx.product.id,bench_id=ctx.bench.id,manifest_commit='manifest',
        board_config='RV1126B',gerrit_project=ctx.product.gerrit_project,change_number=1,patchset_number=1,
        commit_sha='a'*40,work_branch='',product_snapshot={},bench_snapshot={},status=Status.TEST_COMPLETE,
        test_case_snapshot=[{'id':'case','code':req['code'],'adapter':req['adapter'],'parameters':{}}],
        image_snapshot=images or [{'filename':'oem.img','sha256':'a'*64,'size_bytes':10}],
        build_result={'passed':True},delivery_result={'passed':True},controller_result={'state':'finished','boot_verified':True,'flash_exit_code':0,
            'cases':[{'id':'case','state':'finished','outcome':outcome,'assertions_total':1,'assertions_passed':int(outcome=='passed'),'recovery':{'state':'passed'}}]})
    ctx.db.add(row);ctx.db.commit()
    candidate=ReleaseCandidate(product_id=ctx.product.id,source_round_id=row.id,name='RC'+str(number),
        fingerprint=p.digest(p.image_identity(row.image_snapshot)),snapshot={'images':row.image_snapshot,'commit':row.commit_sha,'subject':'test'})
    ctx.db.add(candidate);ctx.db.commit()
    plan=ReleasePlan(candidate_id=candidate.id,name='full',requirements=[req])
    ctx.db.add(plan);ctx.db.commit()
    return row,candidate,plan,req

def test_binary_identity_required_even_same_commit(context):
    row,candidate,plan,req=fixture_round(context)
    assert p.round_evidence(row,req,candidate)[0]=='passed'
    row.image_snapshot=[{**row.image_snapshot[0],'sha256':'b'*64}]
    assert p.round_evidence(row,req,candidate)[0]=='blocked'

def test_quick_version_text_without_flash_does_not_prove_release(context):
    row,candidate,plan,req=fixture_round(context)
    row.controller_result={**row.controller_result,'boot_verified':False,'identity_verified':True,'version':row.commit_sha}
    assert p.round_evidence(row,req,candidate)[0]=='blocked'

def test_new_candidate_does_not_inherit_old_pass(context):
    row,candidate,plan,req=fixture_round(context)
    context.db.add(ValidationEvidence(plan_id=plan.id,requirement_id=req['id'],requirement_hash=p.spec_hash(req),round_id=row.id,payload={}))
    context.db.commit()
    assert p.coverage(context.db,plan)['ready']
    _,_,other,_=fixture_round(context)
    assert p.coverage(context.db,other)['counts']=={'unmeasured':1}

def test_latest_attempt_blocks_old_pass_and_retains_history(context):
    row,candidate,plan,req=fixture_round(context)
    p.link_evidence(plan.id,p.LinkEvidence(round_id=row.id,requirement_ids=[req['id']]),context.db)
    failed,_,_,_=fixture_round(context,'failed')
    p.link_evidence(plan.id,p.LinkEvidence(round_id=failed.id,requirement_ids=[req['id']]),context.db)
    current=p.coverage(context.db,plan)
    assert current['counts']=={'failed':1} and len(current['requirements'][0]['history'])==2
    # A previously unlinked older result cannot overwrite a newer failure.
    old,_,_,_=fixture_round(context)
    old.created_at=row.created_at-timedelta(seconds=10);context.db.commit()
    with pytest.raises(HTTPException) as exc:
        p.link_evidence(plan.id,p.LinkEvidence(round_id=old.id,requirement_ids=[req['id']]),context.db)
    assert exc.value.status_code==409

def test_pending_placeholder_prevents_finalize(context):
    row,candidate,plan,req=fixture_round(context)
    plan.requirements=[{**req,'mode':'pending','adapter':None}];context.db.commit()
    assert p.coverage(context.db,plan)['counts']=={'pending_definition':1}
    with pytest.raises(HTTPException):p.finalize(plan.id,p.Finalize(revision=1,operator='tester'),context.db)

def test_definition_or_parameters_changed_invalidate_evidence(context):
    row,candidate,plan,req=fixture_round(context)
    altered={**req,'parameters':{'max_rtt_ms':10}}
    assert p.round_evidence(row,altered,candidate)[0]=='stale'
    assert p.spec_hash(altered)!=p.spec_hash(req)

def test_completed_report_is_frozen(context):
    row,candidate,plan,req=fixture_round(context)
    p.link_evidence(plan.id,p.LinkEvidence(round_id=row.id,requirement_ids=[req['id']]),context.db)
    report=p.finalize(plan.id,p.Finalize(revision=1,operator='tester'),context.db)
    assert report['candidate_fingerprint']==candidate.fingerprint
    row.controller_result={**row.controller_result,'state':'interrupted'};context.db.commit()
    assert plan.final_report['counts']=={'passed':1}
    with pytest.raises(HTTPException):p.link_evidence(plan.id,p.LinkEvidence(round_id=row.id,requirement_ids=[req['id']]),context.db)

def test_invalid_assertion_counts_never_pass(context):
    row,candidate,plan,req=fixture_round(context)
    report=deepcopy(row.controller_result);report['cases'][0]['assertions_total']=0;row.controller_result=report
    assert p.round_evidence(row,req,candidate)[0]=='error'

def test_bench_busy_protects_other_rounds(context):
    row,_,_,_=fixture_round(context)
    row.status=Status.BENCH_RUNNING;context.db.commit()
    assert p.bench_busy(context.db,context.bench.id)
    assert not p.bench_busy(context.db,context.bench.id,row.id)

def test_schedule_timezone_next_daily():
    stamp=p.datetime(2026,9,29,17,0,tzinfo=p.timezone.utc)
    assert p.next_daily({'timezone':'Asia/Shanghai','daily_time':'02:00'},stamp).isoformat()=='2026-09-29T18:00:00+00:00'

def test_persistent_trigger_deduplicates_across_ticks(context,monkeypatch):
    stamp=p.now();schedule=Schedule(name='nightly',enabled=True,config={'timezone':'UTC','daily_time':'02:00'},next_fire_at=stamp-timedelta(seconds=30))
    context.db.add(schedule);context.db.commit()
    engine=p.ScheduleEngine(None)
    async def noop(jid): pass
    monkeypatch.setattr(engine,'dispatch',noop)
    asyncio.run(engine.tick());asyncio.run(engine.tick())
    rows=list(context.db.scalars(select(ScheduleRun)))
    assert len(rows)==1 and rows[0].state=='queued'
    context.db.expire_all()
    assert p.utc(context.db.get(Schedule,schedule.id).next_fire_at)>stamp

def test_missed_schedule_skips_without_backlog(context,monkeypatch):
    schedule=Schedule(name='nightly',enabled=True,config={'timezone':'UTC','daily_time':'02:00'},next_fire_at=p.now()-timedelta(days=2))
    context.db.add(schedule);context.db.commit()
    engine=p.ScheduleEngine(None)
    async def forbidden(jid): pytest.fail('must not execute missed schedule')
    monkeypatch.setattr(engine,'dispatch',forbidden)
    asyncio.run(engine.tick())
    rows=list(context.db.scalars(select(ScheduleRun)))
    assert len(rows)==1 and rows[0].state=='skipped'

def test_restart_with_linked_created_round_starts_same_round(context):
    row,_,_,_=fixture_round(context);row.status=Status.CREATED
    schedule=Schedule(name='nightly',enabled=True,config={'bench_id':context.bench.id},next_fire_at=p.now()+timedelta(days=1))
    context.db.add(schedule);context.db.flush()
    job=ScheduleRun(schedule_id=schedule.id,scheduled_at=p.now(),state='dispatching',round_id=row.id)
    context.db.add(job);context.db.commit()
    started=[]
    async def start(rid):started.append(rid)
    asyncio.run(p.ScheduleEngine(SimpleNamespace(start=start)).dispatch(job.id))
    assert started==[row.id]
    assert len(list(context.db.scalars(select(Round))))==1

def test_candidate_delivery_rejects_tampered_image_before_transfer(context):
    row,candidate,plan,req=fixture_round(context)
    image=context.tmp/'oem.img';image.write_bytes(b'tampered')
    row.origin='release';row.candidate_id=candidate.id;row.image_snapshot=[{**row.image_snapshot[0],'local_path':str(image)}]
    context.db.commit()
    workflow=SimpleNamespace()
    asyncio.run(p.dispatch_candidate(workflow,row.id))
    context.db.expire_all()
    assert context.db.get(Round,row.id).status==Status.BLOCKED
    assert 'checksum' in context.db.get(Round,row.id).last_error

def test_full_catalog_keeps_six_special_placeholders(context):
    reqs=p.templates(context.db)
    special=[r for r in reqs if r['group']=='特殊场景']
    assert len(special)==6 and all(r['mode']=='pending' for r in special)


def test_schedule_round_creation_atomically_links_job_and_freezes_branch(context):
    from develop_hub.models import TestCase as Case, TestProductStatus
    from develop_hub.schemas import TestRoundCreate
    from develop_hub.test_workbench_api import create_round_internal
    context.product.status=TestProductStatus.READY
    context.product.baseline_result={'passed':True}
    context.product.manifest_snapshot={'manifest_commit':'fixed-manifest'}
    case=Case(code='INFO-PING',name='Ping',description='fixture',command='fixture')
    context.db.add(case);context.db.flush()
    schedule=Schedule(name='nightly',enabled=True,config={'definitions':{case.id:None}},next_fire_at=p.now()+timedelta(days=1))
    context.db.add(schedule);context.db.flush()
    job=ScheduleRun(schedule_id=schedule.id,scheduled_at=p.now(),state='dispatching')
    context.db.add(job);context.db.commit()
    source={'kind':'branch','ref':'refs/heads/master','commit_sha':'b'*40,'change_number':0,'patchset_number':0,'subject':'master','owner':''}
    payload=TestRoundCreate(product_id=context.product.id,bench_id=context.bench.id,test_case_ids=[case.id],auto_start=False)
    row=asyncio.run(create_round_internal(payload,context.db,None,source=source,schedule_job=job))
    assert row.origin=='scheduled' and row.schedule_id==schedule.id
    assert row.commit_sha=='b'*40 and row.product_snapshot['source_ref']=='refs/heads/master'
    assert row.test_point_id is None and job.round_id==row.id and job.source_commit==row.commit_sha


def test_release_execution_and_delivery_reuse_frozen_images(context,monkeypatch):
    from develop_hub.models import TestCase as Case
    from develop_hub.test_workbench import TestRoundWorkflow
    row,candidate,plan,req=fixture_round(context)
    case=Case(id='case',code='INFO-PING',name='Ping',description='fixture',command='fixture')
    context.db.add(case)
    file=context.tmp/'oem.img';file.write_bytes(b'image')
    images=[{'filename':'oem.img','local_path':str(file),'size_bytes':5,'sha256':p.sha256_file(file)}]
    row.image_snapshot=images
    candidate.fingerprint=p.digest(p.image_identity(images))
    candidate.snapshot={**candidate.snapshot,'images':images,'product':{},'build':{'passed':True}}
    context.db.commit()
    monkeypatch.setattr(p,'definition',lambda code:req['adapter'])
    monkeypatch.setattr(p,'validate_parameters',lambda *args:None)
    async def devices(*args):return [{'serial':'CPU123','usb_path':'1-1'}]
    async def ready(*args):return {'passed':True}
    monkeypatch.setattr(p,'bench_devices',devices)
    import develop_hub.bench_readiness as readiness
    monkeypatch.setattr(readiness,'inspect_bench',ready)
    events=[]
    async def start(rid):events.append(('start',rid))
    result=asyncio.run(p.execute_plan(plan.id,p.ExecutePlan(requirement_ids=[req['id']],revision=1,bench_id=context.bench.id,device_serial='CPU123'),context.db,SimpleNamespace(start=start)))
    created=context.db.get(Round,result['round_id'])
    assert created.origin=='release' and created.candidate_id==candidate.id
    assert created.image_snapshot==images and created.commit_sha==candidate.snapshot['commit']
    assert not created.product_snapshot.get('developer_mode')
    assert p.coverage(context.db,plan)['counts']=={'queued':1}
    async def transfer(rnd,bench,path,artifacts):
        assert artifacts==images
        assert 'firmware_flash' in path.read_text()
        events.append(('transfer',rnd.id));return '/qa/job'
    async def verify(*args):events.append(('verify',created.id));return {'passed':True,'files':[]}
    async def dispatch(rid):events.append(('dispatch',rid))
    workflow=SimpleNamespace(settings=SimpleNamespace(artifact_root=context.tmp),_config=TestRoundWorkflow._config,
        _transfer=transfer,_verify_remote=verify,_dispatch_bench=dispatch)
    asyncio.run(p.dispatch_candidate(workflow,created.id))
    assert [e[0] for e in events]==['start','transfer','verify','dispatch']
    assert len(list(context.db.scalars(select(ValidationEvidence).where(ValidationEvidence.plan_id==plan.id))))==1


def test_paused_schedule_does_not_run_queued_job(context):
    schedule=Schedule(name='paused',enabled=False,config={},next_fire_at=p.now()+timedelta(days=1))
    context.db.add(schedule);context.db.flush()
    job=ScheduleRun(schedule_id=schedule.id,scheduled_at=p.now(),state='queued')
    context.db.add(job);context.db.commit()
    asyncio.run(p.ScheduleEngine(None).dispatch(job.id))
    context.db.expire_all()
    assert context.db.get(ScheduleRun,job.id).state=='skipped'


def test_live_coverage_tracks_each_case_without_finalizing_early(context):
    row,candidate,plan,req=fixture_round(context)
    specs=[('complete','finished'),('current','running'),('waiting','queued'),('blocked','blocked')]
    requirements=[{**req,'id':new_id(),'code':name,'case_id':name} for name,_ in specs]
    plan.requirements=requirements
    row.test_case_snapshot=[{'id':r['case_id'],'code':r['code'],'adapter':r['adapter'],'parameters':{}} for r in requirements]
    row.status=Status.BENCH_RUNNING
    row.controller_result={**row.controller_result,'state':'running','cases':[
        {'id':name,'state':state,'outcome':'passed' if state=='finished' else None,
         'assertions_total':1,'assertions_passed':1,'recovery':{'state':'passed'}} for name,state in specs]}
    for requirement in requirements:
        context.db.add(ValidationEvidence(plan_id=plan.id,requirement_id=requirement['id'],
            requirement_hash=p.spec_hash(requirement),round_id=row.id,payload={}))
    context.db.commit()
    report=p.coverage(context.db,plan)
    assert report['counts']=={'completed':1,'running':1,'queued':1,'blocked':1}
    assert not report['ready']
    with pytest.raises(HTTPException):
        p.finalize(plan.id,p.Finalize(revision=plan.revision,operator='tester'),context.db)
    with pytest.raises(HTTPException):
        p.link_evidence(plan.id,p.LinkEvidence(round_id=row.id,requirement_ids=[requirements[0]['id']]),context.db)


@pytest.mark.parametrize('status', p.ACTIVE)
def test_active_round_without_controller_case_is_queued(context,status):
    row,candidate,_,req=fixture_round(context)
    row.status=Status(status)
    row.controller_result={}
    assert p.round_evidence(row,req,candidate)[0]=='queued'


@pytest.mark.parametrize('missing', ['boot','flash','delivery','assertions','recovery','finished_report'])
def test_completed_case_still_requires_terminal_evidence(context,missing):
    row,candidate,_,req=fixture_round(context)
    row.status=Status.BENCH_RUNNING
    report=deepcopy(row.controller_result)
    report['state']='running'
    row.controller_result=report
    assert p.round_evidence(row,req,candidate)[0]=='completed'
    row.status=Status.TEST_COMPLETE
    report['state']='finished'
    if missing=='boot': report['boot_verified']=False
    elif missing=='flash': report['flash_exit_code']=1
    elif missing=='delivery': row.delivery_result={'passed':False}
    elif missing=='assertions': report['cases'][0]['assertions_passed']=0
    elif missing=='recovery': report['cases'][0]['recovery']={'state':'failed'}
    elif missing=='finished_report': report['state']='interrupted'
    assert p.round_evidence(row,req,candidate)[0] in ('blocked','error')


def test_completed_case_only_passes_after_round_finishes(context):
    row,candidate,_,req=fixture_round(context)
    row.status=Status.BENCH_RUNNING
    assert p.round_evidence(row,req,candidate)[0]=='completed'
    row.status=Status.TEST_COMPLETE
    assert p.round_evidence(row,req,candidate)[0]=='passed'


def test_delivery_round_without_cases_creates_valid_configuration(context):
    from develop_hub.models import TestProductStatus
    from develop_hub.schemas import TestRoundCreate
    from develop_hub.test_workbench import TestRoundWorkflow
    from develop_hub.test_workbench_api import create_round_internal
    context.product.status=TestProductStatus.READY
    context.product.baseline_result={'passed':True}
    context.product.manifest_snapshot={'manifest_commit':'fixed-manifest'}
    source={'kind':'branch','ref':'refs/heads/master','commit_sha':'b'*40,'change_number':0,
            'patchset_number':0,'subject':'master','owner':''}
    payload=TestRoundCreate(product_id=context.product.id,bench_id=context.bench.id,test_case_ids=[],auto_start=False)
    row=asyncio.run(create_round_internal(payload,context.db,None,source=source))
    assert row.test_case_snapshot==[]
    config=TestRoundWorkflow._config(row,[],context.bench)
    assert config['test_cases']==[]
    assert config['execute_test_cases'] is False
    assert config['execution_boundary']=='remote_file_verification'


@pytest.mark.parametrize('developer_mode', [False,True])
@pytest.mark.parametrize('case_fields', [{},{'test_case_ids':[]}])
def test_test_execution_requires_cases_in_both_modes(developer_mode,case_fields):
    from pydantic import ValidationError
    from develop_hub.schemas import TestRoundCreate
    with pytest.raises(ValidationError,match='Select at least one Test Case'):
        TestRoundCreate(product_id='product',bench_id='bench',developer_mode=developer_mode,
            execute_test_cases=True,flash_firmware=not developer_mode,**case_fields)
