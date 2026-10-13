"""Release coverage and scheduled tests reuse the controlled TestRound execution path."""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .basic_cases import ACTIVE_CODES, ROOT, definition, validate_parameters
from .case_classification import classifications
from .special_catalog import CODES as SPECIAL_CODES, definition as special_definition
from .config import settings
from .db import SessionLocal, get_db
from .models import (ReleaseCandidate, ReleasePlan, ValidationEvidence, TestSchedule, ScheduleRun,
                     TestRound, TestRoundStatus, TestRoundEvent, TestProduct, TestCase, TestBench, new_id)
from .test_workbench import now, sha256_file, TestWorkbenchError
from .test_workbench_api import get_round_workflow, round_query, product_query, next_round_number, bench_devices

router = APIRouter(prefix='/api/qa/platform')
from .prebuilt_candidates import options as prebuilt_options, create as create_prebuilt
router.get('/prebuilt-options')(prebuilt_options)
router.post('/candidates/prebuilt')(create_prebuilt)

log = logging.getLogger(__name__)
ACTIVE = ('checking_out', 'building', 'packaging', 'transferring', 'verifying', 'bench_running')

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

def require(db, model, key):
    row = db.get(model, key)
    if row is None: raise HTTPException(404, '记录不存在')
    return row

def image_identity(images):
    return sorted([{k: i[k] for k in ('filename', 'sha256', 'size_bytes')} for i in images], key=lambda i: i['filename'])

def bench_busy(db, bench_id, excluding=None):
    from .models import SpecialRun
    from .release_special import paired_preparation
    if db.scalar(select(TestRound.id).where(TestRound.bench_id == bench_id,
        TestRound.status.in_(ACTIVE), TestRound.id != (excluding or ''))) is not None:return True
    requested=db.get(TestRound,excluding) if excluding else None
    for run in db.scalars(select(SpecialRun).where(SpecialRun.bench_id==bench_id,SpecialRun.bench_reserved.is_(True))):
        if run.id==excluding:continue
        if requested is not None and paired_preparation(requested,run):continue
        return True
    return False

def spec_hash(req):
    return digest({k: v for k, v in req.items() if k != 'id'})

def ensure_open(plan):
    if plan.final_report: raise HTTPException(409, '验证报告已冻结，请创建新的验证计划')

def templates(db):
    catalog = json.loads((ROOT.parent/'web/app/scenario-catalog.json').read_text())
    registered = {c.code: c for c in db.scalars(select(TestCase).where(TestCase.enabled.is_(True)))}
    planned = {c['code']: (g['name'], c) for g in catalog['scenarios'] for c in g['cases']}
    output = []
    for code in dict.fromkeys([*ACTIVE_CODES, *planned]):
        case = registered.get(code)
        group, info = planned.get(code, ('基础功能', {}))
        implemented = code in ACTIVE_CODES and case is not None
        output.append({'id': new_id(), 'code': code, 'name': case.name if case else info.get('name', code),
            'group': group, 'mode': 'automatic' if implemented else 'gated' if code in SPECIAL_CODES and case else 'pending', 'case_id': case.id if case else None,
            'adapter': definition(code) if implemented else special_definition(code) if code in SPECIAL_CODES and case else None, 'parameters': {}, 'procedure': '',
            'environment': '产品对应标准测试台；一个锁定模组', 'required': True})
    return output

class CandidateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    source_round_id: str

@router.post('/candidates')
def create_candidate(payload: CandidateCreate, db: Session = Depends(get_db)):
    source = require(db, TestRound, payload.source_round_id)
    if 'rv1126b' not in source.board_config.lower(): raise HTTPException(422, '当前发布补测适配 RV1126B 产品')
    if not (source.build_result or {}).get('passed') or not source.image_snapshot or not source.commit_sha:
        raise HTTPException(422, '请选择已成功构建且保留镜像的常规轮次；当前固件快速测试不能创建候选包')
    if source.status.value in (*ACTIVE, 'created'): raise HTTPException(409, '源轮次尚未完成')
    if db.scalar(select(ReleaseCandidate.id).where(ReleaseCandidate.product_id == source.product_id, ReleaseCandidate.name == payload.name.strip())):
        raise HTTPException(409, '同一产品的候选版本名已存在，请使用新的 RC 名称')
    cid = new_id()
    folder = settings.artifact_root/'release-candidates'/cid
    folder.mkdir(parents=True, exist_ok=False)
    images = []
    for item in source.image_snapshot:
        path = Path(item['local_path']).resolve()
        if not path.is_relative_to(settings.artifact_root.resolve()) or not path.is_file() or sha256_file(path) != item['sha256']:
            raise HTTPException(409, '源镜像缺失或 SHA-256 不符')
        if Path(item['filename']).name != item['filename']: raise HTTPException(422, '镜像名称无效')
        target = folder/item['filename']
        shutil.copy2(path, target)
        if sha256_file(target) != item['sha256']: raise HTTPException(409, '冻结镜像时校验失败')
        images.append({**item, 'local_path': str(target)})
    snap = {'images': images, 'commit': source.commit_sha, 'subject': source.product_snapshot.get('test_point', {}).get('subject', ''),
            'board_config': source.board_config, 'manifest_commit': source.manifest_commit,
            'build': deepcopy(source.build_result), 'product': deepcopy(source.product_snapshot),
            'source': {'kind': 'gerrit', 'source_round_id': source.id},
            'identity': {'kind': 'commit', 'value': source.commit_sha}}
    row = ReleaseCandidate(id=cid, product_id=source.product_id, source_round_id=source.id,
        name=payload.name.strip(), fingerprint=digest(image_identity(images)), snapshot=snap)
    db.add(row)
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409, '候选版本名已存在')
    return {'id': row.id}

@router.get('/candidates')
def candidates(db: Session = Depends(get_db)):
    return [{'id': c.id, 'name': c.name, 'product_id': c.product_id, 'source_round_id': c.source_round_id,
             'fingerprint': c.fingerprint, 'commit': c.snapshot['commit'], 'subject': c.snapshot['subject'],
             'images': image_identity(c.snapshot['images']), 'created_at': c.created_at,
             'source': c.snapshot.get('source', {'kind': 'gerrit', 'source_round_id': c.source_round_id}),
             'identity': c.snapshot.get('identity', {'kind': 'commit', 'value': c.snapshot['commit']})}
            for c in db.scalars(select(ReleaseCandidate).order_by(ReleaseCandidate.created_at.desc()))]

class PlanCreate(BaseModel):
    candidate_id: str
    name: str = Field(min_length=1, max_length=160)

@router.post('/plans')
def create_plan(payload: PlanCreate, db: Session = Depends(get_db)):
    require(db, ReleaseCandidate, payload.candidate_id)
    row = ReleasePlan(candidate_id=payload.candidate_id, name=payload.name, requirements=templates(db))
    db.add(row); db.commit()
    return {'id': row.id}

def round_evidence(row, req, candidate):
    """Never let matching version text or an old PASS establish binary identity."""
    report = row.controller_result or {}
    if row.product_id != candidate.product_id or digest(image_identity(row.image_snapshot)) != candidate.fingerprint:
        return 'blocked', '固件包不匹配'
    actual = next((c for c in row.test_case_snapshot if c.get('code') == req['code']), None)
    if not actual or actual.get('adapter') != req['adapter'] or actual.get('parameters', {}) != req['parameters']:
        return 'stale', '用例、工具或参数与清单不匹配'
    result = next((c for c in report.get('cases', []) if c.get('id') == actual['id']), {})
    if row.status.value == 'created': return 'queued', '等待执行'
    if row.status.value in ACTIVE:
        # A round can be building, flashing, or running another case. Only the
        # Controller's case state establishes which requirement is executing.
        state = result.get('state')
        if state == 'running': return 'running', '用例执行中'
        if state == 'finished': return 'completed', '用例已完成，等待轮次结束后确认结果'
        if state == 'blocked': return 'blocked', result.get('reason') or '本轮未执行此用例'
        return 'queued', '等待执行此用例'
    if row.status.value == 'blocked': return 'blocked', row.last_error or '轮次阻塞'
    if not report.get('boot_verified') or report.get('flash_exit_code') != 0 or not (row.delivery_result or {}).get('passed'):
        return 'blocked', '未完成固定镜像烧录与启动版本确认'
    if result.get('state') != 'finished': return 'blocked', '本轮未完成此用例'
    values = classifications(row.test_case_snapshot, report, row.events)
    outcome = values.get(req['code'], {}).get('outcome') or result.get('outcome', 'error')
    if outcome == 'passed' and not values.get(req['code'], {}).get('manual'):
        if not result.get('assertions_total') or result.get('assertions_total') != result.get('assertions_passed') or (result.get('recovery') or {}).get('state') == 'failed':
            return 'error', '断言或恢复证据不完整'
    if report.get('state') != 'finished': return 'error', '轮次异常中断'
    return outcome if outcome in ('passed', 'failed', 'blocked', 'error') else 'error', result.get('message', '')

def coverage(db, plan):
    from .release_special import special_evidence, aggregate_ota
    candidate = require(db, ReleaseCandidate, plan.candidate_id)
    evidence = list(db.scalars(select(ValidationEvidence).where(ValidationEvidence.plan_id == plan.id).order_by(ValidationEvidence.created_at, ValidationEvidence.id)))
    rows = []
    for req in plan.requirements:
        history = []
        for entry in evidence:
            if entry.requirement_id != req['id']: continue
            status, message = entry.payload.get('outcome', 'unmeasured'), entry.payload.get('reason', '')
            number = None
            audit = None
            special_run_id=entry.payload.get('special_run_id')
            if req['mode']=='gated':
                status,message=special_evidence(db,plan,req,candidate,entry)
            elif entry.round_id:
                rnd = db.scalar(round_query().where(TestRound.id == entry.round_id))
                status, message = round_evidence(rnd, req, candidate)
                number = rnd.display_number
                audit = classifications(rnd.test_case_snapshot, rnd.controller_result, rnd.events).get(req['code'])
            if entry.requirement_hash != spec_hash(req): status, message = 'stale', '清单要求已更新'
            history.append({'id': entry.id, 'status': status, 'message': message, 'round_id': entry.round_id,
                'round_number': number, 'special_run_id': special_run_id, 'classification': audit, 'payload': entry.payload, 'created_at': entry.created_at.isoformat()})
        latest = history[-1] if history else None
        status = latest['status'] if latest else ('pending_definition' if req['mode'] == 'pending' else 'unmeasured')
        extra={}
        if req['mode']=='gated' and req['code']=='SPECIAL-OTA':
            status,variants=aggregate_ota(db,history,status)
            extra['variant_statuses']=variants
        rows.append({**req, 'status': status, 'history': history, 'latest': latest, **extra})
    counts = dict(Counter(r['status'] for r in rows))
    return {'id': plan.id, 'name': plan.name, 'candidate_id': plan.candidate_id, 'candidate_name': candidate.name,
        'revision': plan.revision, 'requirements': rows, 'counts': counts, 'total': len(rows),
        'ready': bool(rows) and all(r['status'] == 'passed' for r in rows), 'final_report': plan.final_report}

@router.get('/plans')
def plans(db: Session = Depends(get_db)):
    return [coverage(db, p) for p in db.scalars(select(ReleasePlan).order_by(ReleasePlan.created_at.desc()))]

@router.get('/plans/{pid}')
def plan_detail(pid: str, db: Session = Depends(get_db)):
    return coverage(db, require(db, ReleasePlan, pid))

class RequirementEdit(BaseModel):
    revision: int
    parameters: dict = Field(default_factory=dict)
    operator: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)

@router.patch('/plans/{pid}/requirements/{rid}')
def edit_requirement(pid: str, rid: str, payload: RequirementEdit, db: Session = Depends(get_db)):
    plan = require(db, ReleasePlan, pid); ensure_open(plan)
    if plan.revision != payload.revision: raise HTTPException(409, '清单已变化，请刷新')
    reqs = deepcopy(plan.requirements)
    req = next((r for r in reqs if r['id'] == rid), None)
    if req is None: raise HTTPException(404, '清单项不存在')
    if req['mode'] != 'automatic': raise HTTPException(409, '特殊场景尚待定义，当前只占位')
    validate_parameters(req['code'], payload.parameters)
    before = deepcopy(req)
    req['parameters'] = payload.parameters
    # Updating the definition is explicit and audited, not a silent execution-time change.
    req['adapter'] = definition(req['code'])
    updated = db.execute(update(ReleasePlan).where(ReleasePlan.id == pid, ReleasePlan.revision == payload.revision,
        ReleasePlan.final_report.is_(None)).values(requirements=reqs, revision=payload.revision + 1))
    if updated.rowcount != 1: raise HTTPException(409, '清单已变化，请刷新')
    db.add(ValidationEvidence(plan_id=pid, requirement_id=rid, requirement_hash=spec_hash(req), payload={
        'outcome': 'stale', 'operator': payload.operator, 'reason': payload.reason, 'before': before, 'after': req, 'kind': 'requirement_revision'}))
    db.commit(); return {'revision': payload.revision + 1}

class SyncSpecial(BaseModel):
    revision:int
    operator:str=Field(min_length=1,max_length=120)
    reason:str=Field(min_length=1,max_length=2000)

@router.post('/plans/{pid}/sync-special')
def sync_special(pid:str,payload:SyncSpecial,db:Session=Depends(get_db)):
    from .bench_reservation import bench_guard
    with bench_guard('plan:'+pid):
        return sync_special_internal(pid,payload,db)

@router.post('/plans/{pid}/sync-definitions')
def sync_definitions(pid:str,payload:SyncSpecial,db:Session=Depends(get_db)):
    from .bench_reservation import bench_guard
    with bench_guard('plan:'+pid):
        return sync_special_internal(pid,payload,db,include_automatic=True)

def sync_special_internal(pid,payload,db,*,include_automatic=False):
    db.expire_all()
    plan=require(db,ReleasePlan,pid);ensure_open(plan)
    if not payload.operator.strip() or not payload.reason.strip():raise HTTPException(422,'请记录操作员及同步原因')
    if plan.revision!=payload.revision:raise HTTPException(409,'清单已变化，请刷新')
    from .special_catalog import install_missing
    from .models import SpecialRun
    if db.scalar(select(TestRound.id).where(TestRound.validation_plan_id==pid,
        TestRound.status.in_((*ACTIVE, 'created')))) is not None:
        raise HTTPException(409,'本计划有等待或正在执行的轮次，请结束后同步')
    # Keep a running gate tied to its original immutable requirements.
    for run in db.scalars(select(SpecialRun).where(SpecialRun.bench_reserved.is_(True))):
        if run.context.get('release_binding',{}).get('validation_plan_id')==pid:
            raise HTTPException(409,'本计划有未结束的人工门控，请结束后同步')
    install_missing(db)
    modes=('automatic','gated') if include_automatic else ('gated',)
    current={r['code']:r for r in templates(db) if r['mode'] in modes}
    reqs=deepcopy(plan.requirements);updated=[]
    for req in reqs:
        if req['code'] not in current:continue
        fresh=current[req['code']]
        after={**req,**{key:fresh[key] for key in ('mode','case_id','adapter','name')}}
        if after==req:continue
        before=deepcopy(req);req.update(after);updated.append(req['code'])
        db.add(ValidationEvidence(plan_id=pid,requirement_id=req['id'],requirement_hash=spec_hash(req),
            payload={'kind':'requirement_revision','outcome':'unmeasured','operator':payload.operator.strip(),
                'reason':payload.reason.strip(),'before':before,'after':deepcopy(req),
                'sync_scope':'all_implemented' if include_automatic else 'special'}))
    if updated:
        changed=db.execute(update(ReleasePlan).where(ReleasePlan.id==pid,ReleasePlan.revision==payload.revision,
            ReleasePlan.final_report.is_(None)).values(requirements=reqs,revision=payload.revision+1))
        if changed.rowcount!=1:db.rollback();raise HTTPException(409,'清单已变化，请刷新')
    db.commit();db.refresh(plan)
    return {'revision':plan.revision,'updated':updated}

class StartSpecial(BaseModel):
    revision:int
    bench_id:str
    device_serial:str=Field(min_length=1,max_length=128)
    operator:str=Field(min_length=1,max_length=120)

@router.post('/plans/{pid}/requirements/{rid}/special-runs',status_code=201)
async def start_special(pid:str,rid:str,payload:StartSpecial,db:Session=Depends(get_db),workflow=Depends(get_round_workflow)):
    plan=require(db,ReleasePlan,pid);ensure_open(plan)
    if plan.revision!=payload.revision:raise HTTPException(409,'清单已变化，请刷新')
    if not payload.operator.strip():raise HTTPException(422,'请填写操作员')
    req=next((r for r in plan.requirements if r['id']==rid),None)
    if not req or req['mode']!='gated':raise HTTPException(422,'清单项尚未接入人工门控')
    if special_definition(req['code'])!=req['adapter']:raise HTTPException(409,'场景或运行包已更新，请显式同步清单')
    basic=next((r for r in plan.requirements if r['code']=='BASIC-VERSION' and r['mode']=='automatic'),None)
    if not basic:raise HTTPException(409,'清单缺少候选启动版本验证用例 BASIC-VERSION')
    return await execute_plan_internal(pid,ExecutePlan(requirement_ids=[basic['id']],revision=payload.revision,
        bench_id=payload.bench_id,device_serial=payload.device_serial),db,workflow,gated=(deepcopy(req),payload.operator))

class ExecutePlan(BaseModel):
    requirement_ids: list[str] = Field(min_length=1)
    revision: int
    bench_id: str
    device_serial: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')

@router.post('/plans/{pid}/execute')
async def execute_plan(pid: str, payload: ExecutePlan, db: Session = Depends(get_db), workflow=Depends(get_round_workflow)):
    return await execute_plan_internal(pid,payload,db,workflow)

async def execute_plan_internal(pid,payload,db,workflow,*,gated=None):
    plan = require(db, ReleasePlan, pid); ensure_open(plan)
    if payload.revision != plan.revision: raise HTTPException(409, '清单已更新，请刷新')
    candidate = require(db, ReleaseCandidate, plan.candidate_id)
    bench = require(db, TestBench, payload.bench_id)
    if not bench.enabled or bench_busy(db, bench.id): raise HTTPException(409, '测试台停用或正在执行其他轮次')
    selected = [r for r in plan.requirements if r['id'] in payload.requirement_ids]
    if len(selected) != len(payload.requirement_ids): raise HTTPException(422, '清单项无效或重复')
    cases = []
    for req in selected:
        if req['mode'] != 'automatic': raise HTTPException(422, '人工门控请通过清单中的特殊场景入口执行')
        current = definition(req['code'])
        if current != req['adapter']: raise HTTPException(409, '用例或 tools 已变化，请显式更新清单要求后重测')
        if current.get('missing_capabilities'): raise HTTPException(409, '工具不支持：'+', '.join(current['missing_capabilities']))
        case = require(db, TestCase, req['case_id'])
        if not case.enabled: raise HTTPException(409, '用例已停用')
        validate_parameters(req['code'], req['parameters'])
        cases.append({'id': case.id, 'code': case.code, 'name': case.name, 'adapter': current,
            'parameters': req['parameters'], 'category': case.category, 'timeout_seconds': case.timeout_seconds,
            'command': case.command, 'expected_exit_code': case.expected_exit_code, 'risk_level': case.risk_level.value})
    if len({c['id'] for c in cases}) != len(cases): raise HTTPException(422, '同一用例的不同配置请分轮执行')
    devices = await bench_devices(bench.id, 'RV1126B', db, workflow)
    matches = [d for d in devices if d['serial'] == payload.device_serial]
    if len(matches) != 1: raise HTTPException(422, '请选择唯一且在线的模组')
    from .bench_readiness import inspect_bench
    readiness = await inspect_bench(workflow, bench, True)
    if not readiness['passed']: raise HTTPException(409, '测试台环境未通过检查')
    source = require(db, TestRound, candidate.source_round_id) if candidate.source_round_id else None
    if source is None and candidate.snapshot.get('source', {}).get('kind') != 'prebuilt':
        raise HTTPException(409, '候选固件来源缺失')
    snap = deepcopy(candidate.snapshot['product'])
    snap['firmware_source'] = deepcopy(candidate.snapshot.get('source', {'kind': 'gerrit', 'source_round_id': candidate.source_round_id}))
    snap['firmware_identity'] = deepcopy(candidate.snapshot.get('identity', {'kind': 'commit', 'value': candidate.snapshot['commit']}))
    from .basic_cases import sha
    snap.pop('developer_mode', None)
    snap['execute_test_cases'] = True
    snap['flash'] = {'worker_sha256': sha(ROOT/'worker.py'), 'adapter_sha256': sha(ROOT/'rv1126b.py'),
        'enabled': True, 'adapter': 'rv1126b-dfu-v1', 'target_chip': 'RV1126B',
        'device_serial': payload.device_serial, 'usb_path': matches[0]['usb_path']}
    snap['release_fingerprint'] = candidate.fingerprint
    snap['release_plan_revision'] = plan.revision
    number = next_round_number(db)
    row = TestRound(id=new_id(), display_number=number, product_id=candidate.product_id, test_point_id=source.test_point_id if source else None,
        bench_id=bench.id, origin='release', candidate_id=candidate.id, validation_plan_id=pid,
        product_snapshot=snap, bench_snapshot={k:getattr(bench,k) for k in ('id','name','host','port','user','remote_root')},
        manifest_commit=source.manifest_commit if source else candidate.snapshot.get('manifest_commit', ''),
        board_config=source.board_config if source else candidate.snapshot['board_config'], gerrit_project=source.gerrit_project if source else '',
        change_number=source.change_number if source else 0, patchset_number=source.patchset_number if source else 0, commit_sha=candidate.snapshot['commit'],
        work_branch='', test_case_snapshot=cases, image_snapshot=deepcopy(candidate.snapshot['images']), build_result=candidate.snapshot['build'])
    row.events.append(TestRoundEvent(sequence=1,event_type='created',status='succeeded',message='发布补测：复用固定候选镜像',details={'candidate_id':candidate.id,'plan_id':pid,'revision':plan.revision}))
    from .bench_reservation import bench_guard
    from .release_special import attach_preparation, refresh_preparation
    special=None
    with bench_guard('plan:'+pid),bench_guard(bench.id):
        db.refresh(plan)
        ensure_open(plan)
        if plan.revision!=payload.revision:raise HTTPException(409,'清单已变化，请刷新')
        if bench_busy(db,bench.id):raise HTTPException(409,'测试台已被其他运行占用')
        for req in selected:
            current=next((item for item in plan.requirements if item['id']==req['id']),None)
            selected_case=require(db,TestCase,req['case_id']);db.refresh(selected_case)
            if current!=req or not selected_case.enabled or definition(req['code'])!=req['adapter']:
                raise HTTPException(409,'用例定义已变化，请刷新并同步清单')
        db.add(row);db.flush()
        for req in selected:
            db.add(ValidationEvidence(plan_id=pid,requirement_id=req['id'],requirement_hash=spec_hash(req),round_id=row.id,payload={'kind':'execution'}))
        if gated:
            special=attach_preparation(db,plan,candidate,row,bench,matches[0],gated)
        db.commit()
    try: await workflow.start(row.id)
    except TestWorkbenchError as exc:
        if special is not None:
            from .special_scenarios import event,view
            with bench_guard(bench.id):
                db.refresh(row);db.refresh(special)
                if row.status==TestRoundStatus.CREATED:
                    row.status=TestRoundStatus.BLOCKED;row.last_error='候选准备未派发：'+str(exc);row.completed_at=now()
                if special.bench_reserved and special.status=='preparing_firmware':
                    special.status='interrupted';special.last_error='候选准备无法启动：'+str(exc)
                    event(special,'preparation_interrupted',special.last_error)
                db.commit()
            return view(special)
        raise HTTPException(409, str(exc)) from exc
    if special is not None:
        from .special_scenarios import view
        return view(special)
    return {'round_id': row.id}

async def dispatch_candidate(workflow, round_id):
    """Identical delivery/flash path, no SDK checkout or build."""
    try:
        with SessionLocal() as db:
            row = db.get(TestRound, round_id)
            candidate = db.get(ReleaseCandidate, row.candidate_id)
            if digest(image_identity(row.image_snapshot)) != candidate.fingerprint: raise TestWorkbenchError('Candidate image identity changed')
            for item in row.image_snapshot:
                if sha256_file(Path(item['local_path'])) != item['sha256']: raise TestWorkbenchError('Candidate image checksum mismatch')
            for case in row.test_case_snapshot:
                if definition(case['code']) != case['adapter']: raise TestWorkbenchError('Frozen case/tool definition changed')
            bench = TestBench(**row.bench_snapshot)
            artifacts = row.image_snapshot
            config = workflow._config(row, artifacts, bench)
            folder = workflow.settings.artifact_root/'test-rounds'/row.id
            folder.mkdir(parents=True,exist_ok=True)
            path = folder/'test-run.json'
            path.write_text(json.dumps(config,ensure_ascii=False,indent=2))
            row.generated_config=config; row.status=TestRoundStatus.TRANSFERRING; db.commit()
        remote = await workflow._transfer(row,bench,path,artifacts)
        delivery = await workflow._verify_remote(bench,remote,artifacts,path)
        with SessionLocal() as db:
            current=db.get(TestRound,round_id); current.remote_path=remote; current.delivery_result=delivery; db.commit()
        await workflow._dispatch_bench(round_id)
    except Exception as exc:
        with SessionLocal() as db:
            row=db.get(TestRound,round_id); row.status=TestRoundStatus.BLOCKED; row.last_error=str(exc); row.completed_at=now(); db.commit()

class LinkEvidence(BaseModel):
    round_id: str
    requirement_ids: list[str] = Field(min_length=1)

@router.post('/plans/{pid}/link')
def link_evidence(pid: str, payload: LinkEvidence, db: Session=Depends(get_db)):
    plan=require(db,ReleasePlan,pid); ensure_open(plan)
    candidate=require(db,ReleaseCandidate,plan.candidate_id)
    row=db.scalar(round_query().where(TestRound.id==payload.round_id))
    if row is None: raise HTTPException(404,'轮次不存在')
    for rid in payload.requirement_ids:
        req=next((r for r in plan.requirements if r['id']==rid),None)
        if req is None: raise HTTPException(422,'清单项不存在')
        if req['mode']!='automatic':raise HTTPException(409,'人工门控证据必须通过本计划专属场景运行生成')
        state,reason=round_evidence(row,req,candidate)
        if state not in ('passed','failed','error'): raise HTTPException(409,'结果不可引用：'+reason)
        previous=list(db.scalars(select(ValidationEvidence).where(ValidationEvidence.plan_id==pid,ValidationEvidence.requirement_id==rid)))
        if any(e.round_id==row.id and e.requirement_hash==spec_hash(req) for e in previous): continue
        # Older results may not overwrite newer attempted execution, including failures.
        for e in previous:
            if e.round_id and utc(require(db,TestRound,e.round_id).created_at)>utc(row.created_at):
                raise HTTPException(409,'不能以旧轮次覆盖较新的测试结果')
        db.add(ValidationEvidence(plan_id=pid,requirement_id=rid,requirement_hash=spec_hash(req),round_id=row.id,payload={'kind':'linked'}))
    db.commit(); return {'linked':True}

class Finalize(BaseModel):
    revision: int
    operator: str = Field(min_length=1,max_length=120)

@router.post('/plans/{pid}/finalize')
def finalize(pid: str,payload: Finalize,db: Session=Depends(get_db)):
    from .bench_reservation import bench_guard
    with bench_guard('plan:'+pid):
        db.expire_all()
        return finalize_internal(pid,payload,db)

def finalize_internal(pid,payload,db):
    plan=require(db,ReleasePlan,pid); ensure_open(plan)
    from .models import SpecialRun
    if any(run.context.get('release_binding',{}).get('validation_plan_id')==pid
           for run in db.scalars(select(SpecialRun).where(SpecialRun.bench_reserved.is_(True)))):
        raise HTTPException(409,'本计划仍有占用测试台的人工门控，不能冻结报告')
    report=coverage(db,plan)
    if plan.revision!=payload.revision or not report['ready']: raise HTTPException(409,'所有清单项通过后才能冻结验证报告')
    report.pop('final_report')
    report.update(operator=payload.operator,completed_at=now().isoformat(),candidate_fingerprint=require(db,ReleaseCandidate,plan.candidate_id).fingerprint)
    updated=db.execute(update(ReleasePlan).where(ReleasePlan.id==pid,ReleasePlan.revision==payload.revision,ReleasePlan.final_report.is_(None)).values(final_report=report))
    if updated.rowcount!=1: raise HTTPException(409,'清单已变更，请刷新后再确认')
    db.commit(); db.refresh(plan); return report

@router.get('/plans/{pid}/report')
def export_report(pid: str,db: Session=Depends(get_db)):
    from fastapi.responses import Response
    plan=require(db,ReleasePlan,pid)
    data=plan.final_report or {**coverage(db,plan),'draft':True}
    return Response(json.dumps(data,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="validation-{pid}.json"'})

class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1,max_length=160)
    product_id: str
    bench_id: str
    device_serial: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    source_kind: str = Field(pattern=r'^(branch|change)$')
    source: str = Field(min_length=1,max_length=500)
    timezone: str = 'Asia/Shanghai'
    daily_time: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    skip_unchanged: bool = True
    test_case_ids: list[str] = Field(min_length=1)
    test_case_parameters: dict[str,dict] = Field(default_factory=dict)
    enabled: bool = False

def next_daily(config, after):
    zone=ZoneInfo(config['timezone'])
    local=utc(after).astimezone(zone)
    hour,minute=map(int,config['daily_time'].split(':'))
    target=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if target<=local: target+=timedelta(days=1)
    return target.astimezone(timezone.utc)

@router.post('/schedules')
def create_schedule(payload: ScheduleCreate,db: Session=Depends(get_db)):
    product=require(db,TestProduct,payload.product_id); require(db,TestBench,payload.bench_id)
    if 'rv1126b' not in product.board_config.lower(): raise HTTPException(422,'当前定时计划适配 RV1126B 产品')
    try: ZoneInfo(payload.timezone)
    except Exception: raise HTTPException(422,'时区无效')
    if payload.source_kind=='branch' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*',payload.source): raise HTTPException(422,'分支名称无效')
    if payload.source_kind=='change':
        from .patchset_lookup import parse_change_link
        from urllib.parse import urlsplit
        try: parse_change_link(payload.source, urlsplit(product.repository_snapshot.get('remote_url','')).hostname or '', product.gerrit_project)
        except TestWorkbenchError as exc: raise HTTPException(422,str(exc)) from exc
    if len(set(payload.test_case_ids))!=len(payload.test_case_ids): raise HTTPException(422,'用例重复')
    if set(payload.test_case_parameters)-set(payload.test_case_ids): raise HTTPException(422,'参数必须属于选中的用例')
    frozen={}
    for cid in payload.test_case_ids:
        case=require(db,TestCase,cid)
        if not case.enabled or case.code not in ACTIVE_CODES: raise HTTPException(422,'用例不可执行')
        validate_parameters(case.code,payload.test_case_parameters.get(cid,{}))
        adapter=definition(case.code)
        if adapter.get('missing_capabilities'): raise HTTPException(422,'用例缺少工具能力')
        frozen[cid]=adapter
    config=payload.model_dump(exclude={'name','enabled'}); config['definitions']=frozen
    row=TestSchedule(name=payload.name,enabled=payload.enabled,config=config,next_fire_at=next_daily(config,now()))
    db.add(row); db.commit(); return {'id':row.id}

class ScheduleToggle(BaseModel):
    enabled: bool

@router.patch('/schedules/{sid}')
def toggle_schedule(sid:str,payload:ScheduleToggle,db:Session=Depends(get_db)):
    row=require(db,TestSchedule,sid); row.enabled=payload.enabled
    if payload.enabled: row.next_fire_at=next_daily(row.config,now())
    db.commit(); return {'enabled':row.enabled}

@router.get('/schedules')
def schedules(db:Session=Depends(get_db)):
    output=[]
    for s in db.scalars(select(TestSchedule).order_by(TestSchedule.created_at.desc())):
        runs=[]
        for r in db.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id==s.id).order_by(ScheduleRun.scheduled_at.desc()).limit(30)):
            row=db.get(TestRound,r.round_id) if r.round_id else None
            runs.append({'id':r.id,'state':row.status.value if row else r.state,'message':r.message,'scheduled_at':r.scheduled_at,
                'round_id':r.round_id,'round_number':row.display_number if row else None,'commit':r.source_commit})
        output.append({'id':s.id,'name':s.name,'enabled':s.enabled,'config':{k:v for k,v in s.config.items() if k!='definitions'},'next_fire_at':s.next_fire_at,'runs':runs})
    return output

async def resolve_schedule_source(config, product):
    from .test_workbench import repository_source, source_matches_project, run_command
    if config['source_kind']=='change':
        from .patchset_lookup import lookup_patchsets
        result=await lookup_patchsets(product,config['source'])
        ps=next(p for p in result['patchsets'] if p['patchset_number']==result['selected_patchset'])
        return {**ps,'change_number':result['change_number'],'subject':result['subject'],'branch':result['branch'],'owner':result['owner'],'kind':'change'}
    source=await repository_source(product.tested_repository,product.sdk_repository)
    if not source_matches_project(source,product.gerrit_project): raise TestWorkbenchError('Gerrit project mismatch')
    ref='refs/heads/'+config['source']
    result=await run_command(['git','ls-remote',source['remote_url'],ref],Path(source['path']),60)
    lines=[line.split() for line in result['stdout_tail'].splitlines() if line.strip()]
    entries=[v for v in lines if len(v)==2 and v[1]==ref and re.fullmatch(r'[0-9a-f]{40,64}',v[0])]
    if result['exit_code'] or len(entries)!=1: raise TestWorkbenchError('找不到指定 Gerrit 分支')
    return {'kind':'branch','commit_sha':entries[0][0],'ref':ref,'change_number':0,'patchset_number':0,'branch':config['source'],
        'subject':'Gerrit 分支 '+config['source'],'owner':'','submitted_at':None}

class ScheduleEngine:
    def __init__(self,workflow): self.workflow=workflow; self.task=None
    def start(self): self.task=asyncio.create_task(self.run(),name='hub-test-scheduler')
    async def stop(self):
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
    async def run(self):
        while True:
            try: await self.tick()
            except Exception: log.exception('Test scheduler tick failed')
            await asyncio.sleep(15)
    async def tick(self):
        stamp=now()
        with SessionLocal() as db:
            for s in db.scalars(select(TestSchedule).where(TestSchedule.enabled.is_(True),TestSchedule.next_fire_at<=stamp)):
                scheduled=utc(s.next_fire_at)
                pending=list(db.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id==s.id,ScheduleRun.state.in_(('queued','dispatching','started')))))
                busy=any(not r.round_id or require(db,TestRound,r.round_id).status.value in (*ACTIVE,'created') for r in pending)
                state='skipped' if busy or stamp-scheduled>timedelta(minutes=5) else 'queued'
                db.add(ScheduleRun(schedule_id=s.id,scheduled_at=scheduled,state=state,message='上一轮尚未结束或错过触发窗口' if state=='skipped' else '等待测试台'))
                s.next_fire_at=next_daily(s.config,stamp)
            try: db.commit()
            except IntegrityError: db.rollback()
            jobs=list(db.scalars(select(ScheduleRun).where(ScheduleRun.state.in_(('queued','dispatching'))).order_by(ScheduleRun.scheduled_at)))
            ids=[j.id for j in jobs]
        for jid in ids: await self.dispatch(jid)
    async def dispatch(self,jid):
        from .schemas import TestRoundCreate
        from .test_workbench_api import create_round_internal
        with SessionLocal() as db:
            job=require(db,ScheduleRun,jid); schedule=require(db,TestSchedule,job.schedule_id); config=schedule.config
            if not schedule.enabled:
                job.state='skipped'; job.message='计划已暂停'; db.commit(); return
            if job.round_id:
                rnd=require(db,TestRound,job.round_id)
                if rnd.status.value=='created' and not bench_busy(db,rnd.bench_id):
                    await self.workflow.start(rnd.id)
                if rnd.status.value!='created': job.state='started'; db.commit()
                return
            if bench_busy(db,config['bench_id']): return
            product=db.scalar(product_query().where(TestProduct.id==config['product_id']))
            if not product: return
            if db.scalar(select(TestRound.id).join(TestProduct,TestRound.product_id==TestProduct.id).where(
                TestRound.status.in_(ACTIVE),TestProduct.sdk_repository_id==product.sdk_repository_id)): return
            if db.scalar(select(TestProduct.id).where(TestProduct.sdk_repository_id==product.sdk_repository_id,TestProduct.status=='verifying')): return
            try:
                for cid, frozen in config['definitions'].items():
                    case=require(db,TestCase,cid)
                    if definition(case.code)!=frozen: raise TestWorkbenchError('用例或 tools 已变化，请重新建立定时计划')
                source=await resolve_schedule_source(config,product)
                previous=db.scalar(select(ScheduleRun).where(ScheduleRun.schedule_id==schedule.id,ScheduleRun.id!=job.id,
                    ScheduleRun.round_id.is_not(None)).order_by(ScheduleRun.scheduled_at.desc()))
                if config['skip_unchanged'] and previous and previous.source_commit==source['commit_sha'] and require(db,TestRound,previous.round_id).status==TestRoundStatus.TEST_COMPLETE:
                    job.state='skipped';job.message='代码未变化，上一轮已通过';job.source_commit=source['commit_sha'];db.commit();return
                job.state='dispatching';db.commit()
                payload=TestRoundCreate(product_id=product.id,bench_id=config['bench_id'],device_serial=config['device_serial'],
                    test_case_ids=config['test_case_ids'],test_case_parameters=config['test_case_parameters'],flash_firmware=True,execute_test_cases=True,auto_start=False)
                row=await create_round_internal(payload,db,self.workflow,source=source,schedule_job=job)
                await self.workflow.start(row.id)
                job.state='started';job.message='已创建并启动轮次';db.commit()
            except Exception as exc:
                db.rollback();job=require(db,ScheduleRun,jid);job.state='blocked';job.message=str(getattr(exc,'detail',str(exc)));db.commit()
