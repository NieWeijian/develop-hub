from __future__ import annotations

from copy import deepcopy
import inspect

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from . import service, managed_devices
from .models import BuildEnvironment, CaseRevision, Device, OperatorRecord, PlanRevision, Product, TestPlan, TestRun, utcnow
from .schemas import (
    CaseCreate, DeviceCreate, DevicePatch, DeviceCheck, EnabledPatch, EnvironmentCreate, EnvironmentPatch,
    OperatorCreate, PlanCopy, PlanCreate, PlanPatch, PlanValidate, ProductCreate, ProductPatch,
    RepeatCreate, RevisionCreate, RunCreate,
)


router = APIRouter(prefix="/api/testing", tags=["RK testing"])


def commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        service.fail(409, "concurrent_conflict", "记录已被其他请求创建或修改，请刷新后重试")


async def coordinate(request: Request, method: str, run_id: str) -> None:
    runner = getattr(request.app.state, "testing_runner", None)
    if runner is None:
        service.fail(503, "runner_unavailable", "运行请求已保存，但执行服务尚未就绪")
    result = getattr(runner, method)(run_id)
    if inspect.isawaitable(result):
        await result


@router.get("/products")
def products(db: Session = Depends(get_db)):
    return [service.view(row) for row in db.scalars(select(Product).order_by(Product.created_at.desc()))]


@router.post("/products", status_code=201)
def create_product(payload: ProductCreate, db: Session = Depends(get_db)):
    row = Product(**payload.model_dump())
    db.add(row)
    commit(db)
    return service.view(row)


@router.get("/products/{identity}")
def product(identity: str, db: Session = Depends(get_db)):
    return service.view(service.require(db, Product, identity))


@router.patch("/products/{identity}")
def update_product(identity: str, payload: ProductPatch, db: Session = Depends(get_db)):
    row = service.require(db, Product, identity)
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(row, key, value)
    commit(db)
    return service.view(row)


@router.delete("/products/{identity}")
def disable_product(identity: str, db: Session = Depends(get_db)):
    row = service.require(db, Product, identity)
    row.enabled = False
    commit(db)
    return service.view(row)


@router.get("/build-environments")
def environments(db: Session = Depends(get_db)):
    return [service.view(row) for row in db.scalars(select(BuildEnvironment).order_by(BuildEnvironment.created_at.desc()))]


@router.post("/build-environments", status_code=201)
def create_environment(payload: EnvironmentCreate, db: Session = Depends(get_db)):
    service.validate_node(payload.node, build=True)
    row = BuildEnvironment(**payload.model_dump())
    db.add(row)
    commit(db)
    return service.view(row)


@router.get("/build-environments/{identity}")
def environment(identity: str, db: Session = Depends(get_db)):
    return service.view(service.require(db, BuildEnvironment, identity))


@router.patch("/build-environments/{identity}")
def update_environment(identity: str, payload: EnvironmentPatch, db: Session = Depends(get_db)):
    service.require(db, BuildEnvironment, identity)
    if payload.node is not None:
        service.validate_node(payload.node, build=True)
    values = payload.model_dump(exclude_none=True, exclude={"revision"})
    values["revision"] = payload.revision + 1
    changed = db.execute(update(BuildEnvironment).where(BuildEnvironment.id == identity, BuildEnvironment.revision == payload.revision).values(**values))
    if changed.rowcount != 1:
        service.fail(409, "stale_environment", "构建环境已被修改，请刷新后重试")
    commit(db)
    return service.view(service.require(db, BuildEnvironment, identity))


@router.delete("/build-environments/{identity}")
def disable_environment(identity: str, db: Session = Depends(get_db)):
    row = service.require(db, BuildEnvironment, identity)
    row.enabled = False
    row.revision += 1
    commit(db)
    return service.view(row)


@router.get("/devices")
def devices(db: Session = Depends(get_db)):
    return [service.view(row) for row in db.scalars(select(Device).order_by(Device.created_at.desc()))]


@router.get('/test-stations')
def test_stations():
    return managed_devices.catalog()


@router.post("/devices", status_code=201)
def create_device(payload: DeviceCreate, db: Session = Depends(get_db)):
    product = service.require(db, Product, payload.product_id)
    values = payload.model_dump(exclude={'station_id', 'serial'})
    if payload.station_id:
        if payload.model_fields_set & {'node', 'config', 'capabilities'}:
            service.fail(422, 'managed_device', '设备适配器由平台管理，无需填写执行配置')
        values.update(managed_devices.bind(payload.station_id, payload.serial, product))
    else:
        if payload.node.get('kind') != 'simulation' and managed_devices.stations():
            service.fail(422, 'managed_device', '请选择平台已部署的测试台')
        service.validate_node(payload.node)
    row = Device(**values)
    db.add(row)
    commit(db)
    return service.view(row)


@router.get("/devices/{identity}")
def device(identity: str, db: Session = Depends(get_db)):
    return service.view(service.require(db, Device, identity))


@router.post('/devices/{identity}/check')
async def check_device(identity: str, payload: DeviceCheck, request: Request):
    runner = getattr(request.app.state, 'testing_runner', None)
    if runner is None:
        service.fail(503, 'runner_unavailable', '执行服务尚未就绪')
    return await runner.check_device(identity, payload.operator, payload.note, payload.recovery_confirmed)


@router.patch("/devices/{identity}")
def update_device(identity: str, payload: DevicePatch, db: Session = Depends(get_db)):
    row = service.require(db, Device, identity)
    if row.config.get('managed_station_id') and payload.model_fields_set & {'node', 'config', 'capabilities'}:
        service.fail(422, 'managed_device', '设备适配器由平台管理，只需编辑设备名称')
    if payload.node is not None:
        service.validate_node(payload.node)
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(row, key, value)
    commit(db)
    return service.view(row)


@router.delete("/devices/{identity}")
def disable_device(identity: str, db: Session = Depends(get_db)):
    row = service.require(db, Device, identity)
    row.enabled = False
    commit(db)
    return service.view(row)


@router.get("/case-revisions")
def cases(db: Session = Depends(get_db)):
    return [service.view(row) for row in db.scalars(select(CaseRevision).order_by(CaseRevision.created_at.desc()))]


@router.post("/case-revisions", status_code=201)
def create_case(payload: CaseCreate, db: Session = Depends(get_db)):
    try:
        row = service.create_case(db, payload)
        commit(db)
    except IntegrityError:
        db.rollback()
        row = service.create_case(db, payload)
        commit(db)
    return service.view(row)


@router.get("/case-revisions/{identity}")
def case(identity: str, db: Session = Depends(get_db)):
    return service.view(service.require(db, CaseRevision, identity))


@router.patch("/case-revisions/{identity}")
def enable_case(identity: str, payload: EnabledPatch, db: Session = Depends(get_db)):
    row = service.require(db, CaseRevision, identity)
    row.enabled = payload.enabled
    commit(db)
    return service.view(row)


@router.delete("/case-revisions/{identity}")
def disable_case(identity: str, db: Session = Depends(get_db)):
    return enable_case(identity, EnabledPatch(enabled=False), db)


@router.get("/test-plans")
def plans(db: Session = Depends(get_db)):
    output = []
    for row in db.scalars(select(TestPlan).order_by(TestPlan.updated_at.desc())):
        item = service.view(row)
        latest = db.scalar(select(PlanRevision).where(PlanRevision.plan_id == row.id).order_by(PlanRevision.version.desc()).limit(1))
        item["latest_revision"] = service.view(latest) if latest else None
        output.append(item)
    return output


@router.post("/test-plans", status_code=201)
def create_plan(payload: PlanCreate, db: Session = Depends(get_db)):
    row = TestPlan(**payload.model_dump())
    db.add(row)
    commit(db)
    return service.view(row)


@router.get("/test-plans/{identity}")
def plan(identity: str, db: Session = Depends(get_db)):
    row = service.require(db, TestPlan, identity)
    result = service.view(row)
    result["revisions"] = [service.view(revision) for revision in db.scalars(select(PlanRevision).where(PlanRevision.plan_id == identity).order_by(PlanRevision.version.desc()))]
    return result


@router.patch("/test-plans/{identity}/draft")
def update_plan(identity: str, payload: PlanPatch, db: Session = Depends(get_db)):
    service.require(db, TestPlan, identity)
    values = {"draft": deepcopy(payload.draft), "revision": payload.revision + 1, "updated_at": utcnow()}
    if payload.name is not None:
        values["name"] = payload.name
    changed = db.execute(update(TestPlan).where(TestPlan.id == identity, TestPlan.revision == payload.revision, TestPlan.archived.is_(False)).values(**values))
    if changed.rowcount != 1:
        service.fail(409, "stale_draft", "草稿已修改或方案已归档，请刷新后重试")
    commit(db)
    return service.view(service.require(db, TestPlan, identity))


@router.delete("/test-plans/{identity}")
def archive_plan(identity: str, db: Session = Depends(get_db)):
    row = service.require(db, TestPlan, identity)
    row.archived = True
    commit(db)
    return service.view(row)


@router.post("/test-plans/{identity}/copy", status_code=201)
def copy_plan(identity: str, payload: PlanCopy, db: Session = Depends(get_db)):
    original = service.require(db, TestPlan, identity)
    row = TestPlan(name=payload.name or original.name + " 副本", draft=deepcopy(original.draft))
    db.add(row)
    commit(db)
    return service.view(row)


@router.post("/test-plans/{identity}/validate")
def validate(identity: str, payload: PlanValidate, db: Session = Depends(get_db)):
    row = service.require(db, TestPlan, identity)
    return service.validate_plan(db, payload.draft if payload.draft is not None else row.draft, payload.parameter_overrides)


@router.post("/test-plans/{identity}/revisions", status_code=201)
def publish(identity: str, payload: RevisionCreate, db: Session = Depends(get_db)):
    revision = service.publish_plan(db, identity, payload.revision)
    commit(db)
    data = service.view(revision)
    data["draft_revision"] = service.require(db, TestPlan, identity).revision
    return data


@router.get("/test-plans/{identity}/revisions/{revision_id}")
def revision(identity: str, revision_id: str, db: Session = Depends(get_db)):
    row = service.require(db, PlanRevision, revision_id)
    if row.plan_id != identity:
        service.fail(404, "not_found", "指定版本不属于当前方案")
    return service.view(row)


@router.get("/test-runs")
def runs(db: Session = Depends(get_db)):
    return [service.report(db, row) for row in db.scalars(select(TestRun).order_by(TestRun.created_at.desc()).limit(100))]


@router.get('/test-runs/{identity}/report.html')
def export_report(identity: str, db: Session = Depends(get_db)):
    from .reporting import report_html
    run = service.require(db, TestRun, identity)
    if run.state not in service.TERMINAL_STATES:
        service.fail(409, 'run_in_progress', '运行结束后可以导出报告')
    return Response(report_html(service.report(db, run, diagnostics=True)), media_type='text/html', headers={
        'Content-Disposition': f'attachment; filename="testing-report-{run.id}.html"',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'",
        'X-Content-Type-Options': 'nosniff'})


@router.post("/test-runs", status_code=201)
async def create_run(payload: RunCreate, request: Request, db: Session = Depends(get_db)):
    try:
        row, created = service.create_run(db, payload)
        db.commit()
    except IntegrityError:
        db.rollback()
        # A simultaneous retry may have won the unique request-key insert.
        existing = db.scalar(select(TestRun).where(TestRun.idempotency_key == payload.idempotency_key))
        if existing is None:
            service.fail(409, "concurrent_conflict", "方案同时发生修改，请刷新重试")
        row, created = service.create_run(db, payload)
    except HTTPException as exc:
        db.rollback()
        # Save-and-run retries can lose the draft CAS after another identical
        # request has committed. The request key remains the authority.
        existing = db.scalar(select(TestRun).where(TestRun.idempotency_key == payload.idempotency_key))
        if exc.status_code != 409 or existing is None:
            raise
        row, created = service.create_run(db, payload)
    if created or row.state == "queued":
        await coordinate(request, "start", row.id)
    db.expire_all()
    return service.report(db, service.require(db, TestRun, row.id))


@router.get("/test-runs/{identity}")
def run(identity: str, db: Session = Depends(get_db)):
    return service.report(db, service.require(db, TestRun, identity))


@router.post("/test-runs/{identity}/repeat", status_code=201)
async def repeat(identity: str, payload: RepeatCreate, request: Request, db: Session = Depends(get_db)):
    try:
        row, created = service.repeat_run(db, identity, payload.idempotency_key)
        db.commit()
    except IntegrityError:
        db.rollback()
        row, created = service.repeat_run(db, identity, payload.idempotency_key)
        db.commit()
    if created or row.state == "queued":
        await coordinate(request, "start", row.id)
    db.expire_all()
    return service.report(db, service.require(db, TestRun, row.id))


@router.get("/test-runs/{identity}/report")
def report(identity: str, db: Session = Depends(get_db)):
    return service.report(db, service.require(db, TestRun, identity), diagnostics=True)


@router.post("/test-runs/{identity}/cancel")
async def cancel(identity: str, request: Request, db: Session = Depends(get_db)):
    row = service.require(db, TestRun, identity)
    if row.state not in service.TERMINAL_STATES:
        await coordinate(request, "cancel", row.id)
    db.expire_all()
    return service.report(db, service.require(db, TestRun, identity))


@router.post("/test-runs/{identity}/operator-records", status_code=201)
async def operator_record(identity: str, payload: OperatorCreate, request: Request, db: Session = Depends(get_db)):
    try:
        row, created = service.record_operator(db, identity, payload)
        db.commit()
    except IntegrityError:
        db.rollback()
        row, created = service.record_operator(db, identity, payload)
        db.commit()
    if created:
        await coordinate(request, "resume", identity)
    return service.view(row)


@router.get("/test-runs/{identity}/operator-records")
def operator_records(identity: str, db: Session = Depends(get_db)):
    service.require(db, TestRun, identity)
    return [service.view(row) for row in db.scalars(select(OperatorRecord).where(OperatorRecord.run_id == identity).order_by(OperatorRecord.revision))]


@router.get("/triggers")
def triggers():
    return {"enabled": False, "message": "自定义定时与 Gerrit 触发将在下一阶段开放", "items": []}
