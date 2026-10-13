from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from .patchset_lookup import lookup_patchsets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .basic_cases import CODES, ACTIVE_CODES, definition
from .bench_readiness import BOARD, artifact_patterns, inspect_bench
from .db import get_db
from .models import (
    ControlSequence,
    GerritRepository,
    Repository,
    SdkRepository,
    TestBench,
    TestCase,
    TestPoint,
    TestProduct,
    TestProductStatus,
    TestRound,
    TestRoundEvent,
    new_id,
)
from .schemas import (
    ManifestSnapshotView,
    SdkRepositoryCreate,
    SdkRepositoryDiscoveryRequest,
    SdkRepositoryDiscoveryView,
    SdkRepositoryView,
    TestBenchCreate,
    TestBenchView,
    TestCaseView,
    TestPointCreate,
    TestPointView,
    TestProductCreate,
    TestProductView,
    TestRoundCreate,
    TestRoundView,
)
from .test_workbench import (
    TestRoundWorkflow,
    TestWorkbenchError,
    discover_sdk_registration,
    inspect_manifest,
    inspect_repository,
    validate_sdk_paths,
    validate_sdk_manifest_checkout,
    verify_product_baseline,
    repository_source,
    board_config_choices,
    source_matches_project,
    verify_patchset,
)


router = APIRouter(prefix="/api/qa")


def get_round_workflow(request: Request) -> TestRoundWorkflow:
    return request.app.state.test_round_workflow


def product_query():  # type: ignore[no-untyped-def]
    return select(TestProduct).options(
        selectinload(TestProduct.sdk_repository),
        selectinload(TestProduct.tested_repository),
        selectinload(TestProduct.gerrit_repository),
    )


def round_query():  # type: ignore[no-untyped-def]
    return select(TestRound).options(
        selectinload(TestRound.product).selectinload(TestProduct.sdk_repository),
        selectinload(TestRound.product).selectinload(TestProduct.tested_repository),
        selectinload(TestRound.product).selectinload(TestProduct.gerrit_repository),
        selectinload(TestRound.test_point),
        selectinload(TestRound.bench),
        selectinload(TestRound.events),
    )


def next_round_number(db: Session) -> int:
    next_value = db.scalar(
        update(ControlSequence)
        .where(ControlSequence.name == "test_round_display")
        .values(next_value=ControlSequence.next_value + 1)
        .returning(ControlSequence.next_value)
    )
    if next_value is not None:
        return next_value - 1
    number = (db.scalar(select(func.max(TestRound.display_number))) or 0) + 1
    db.add(ControlSequence(name="test_round_display", next_value=number + 1))
    db.flush()
    return number


@router.post('/sdk-repositories/discover')
@router.post('/sdk-repositories')
def sdk_registration_not_public() -> None:
    raise HTTPException(status_code=403, detail='SDKs are pre-registered by the 175 administrator; operators may only select existing SDKs')


async def application_choices(sdk: SdkRepository, db: Session) -> list[dict[str, object]]:
    manifest = await inspect_manifest(sdk)
    choices = []
    registered = list(db.scalars(select(GerritRepository).where(GerritRepository.enabled.is_(True))))
    for repository in db.scalars(select(Repository).where(Repository.enabled.is_(True))):
        try:
            source = await repository_source(repository, sdk)
            project = manifest.get('project_names', {}).get(source['relative_path'])
            matching = next((item for item in registered if source_matches_project(source, item.project_path)), None)
            project = matching.project_path if matching else project
            if not project or not source_matches_project(source, project):
                continue
            choices.append({'id': repository.id, 'name': repository.name, 'root_path': repository.root_path,
                            'gerrit_project': project, 'gerrit_repository_id': matching.id if matching else None,
                            'snapshot': source})
        except TestWorkbenchError:
            continue
    return choices


@router.get('/sdk-repositories/{sdk_id}/applications')
async def list_sdk_applications(sdk_id: str, db: Session = Depends(get_db)) -> list[dict[str, object]]:
    sdk = db.get(SdkRepository, sdk_id)
    if not sdk or not sdk.enabled:
        raise HTTPException(status_code=404, detail='SDK repository not found')
    try:
        return await application_choices(sdk, db)
    except TestWorkbenchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/sdk-repositories/{sdk_id}/board-configs')
async def get_board_configs(sdk_id: str, db: Session = Depends(get_db)) -> list[dict[str, object]]:
    sdk = db.get(SdkRepository, sdk_id)
    if not sdk or not sdk.enabled:
        raise HTTPException(status_code=404, detail='SDK repository not found')
    try:
        return await board_config_choices(sdk)
    except TestWorkbenchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sdk-repositories", response_model=list[SdkRepositoryView])
def list_sdk_repositories(db: Session = Depends(get_db)) -> list[SdkRepository]:
    return list(db.scalars(select(SdkRepository).where(SdkRepository.enabled.is_(True)).order_by(SdkRepository.name)))


@router.get("/sdk-repositories/{sdk_id}/manifest", response_model=ManifestSnapshotView)
async def get_sdk_manifest(sdk_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    sdk = db.get(SdkRepository, sdk_id)
    if not sdk or not sdk.enabled:
        raise HTTPException(status_code=404, detail="SDK repository not found")
    try:
        return await inspect_manifest(sdk)
    except TestWorkbenchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/products", response_model=TestProductView, status_code=status.HTTP_201_CREATED)
async def create_product(payload: TestProductCreate, db: Session = Depends(get_db)) -> TestProduct:
    sdk = db.get(SdkRepository, payload.sdk_repository_id)
    repository = db.get(Repository, payload.tested_repository_id)
    if not sdk or not sdk.enabled:
        raise HTTPException(status_code=404, detail="SDK repository not found or disabled")
    if not repository or not repository.enabled:
        raise HTTPException(status_code=404, detail="Tested repository not found or disabled")
    if payload.board_config not in sdk.board_configs:
        raise HTTPException(status_code=422, detail="Board Config is not registered for the selected SDK")
    try:
        manifest = await inspect_manifest(sdk)
        choice = next((item for item in await application_choices(sdk, db) if item['id'] == repository.id), None)
        if not choice:
            raise TestWorkbenchError('Select an application repository whose remote matches the SDK manifest or registered Gerrit project')
        if payload.gerrit_repository_id and payload.gerrit_repository_id != choice['gerrit_repository_id']:
            raise TestWorkbenchError('Gerrit repository does not match the selected application')
        gerrit = db.scalar(select(GerritRepository).where(GerritRepository.project_path == choice['gerrit_project']))
        if gerrit and not gerrit.enabled:
            raise TestWorkbenchError('Gerrit repository is disabled')
        if not gerrit:
            gerrit = GerritRepository(name=repository.name, project_path=choice['gerrit_project'])
            db.add(gerrit)
            db.flush()
        repository_snapshot = choice['snapshot']
        product = TestProduct(
            **payload.model_dump(exclude={"gerrit_repository_id"}),
            gerrit_repository_id=gerrit.id,
            gerrit_project=gerrit.project_path,
            manifest_snapshot=manifest,
            repository_snapshot=repository_snapshot,
        )
        db.add(product)
        db.commit()
        created = db.scalar(product_query().where(TestProduct.id == product.id))
        assert created
        return created
    except TestWorkbenchError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Product code already exists") from exc


@router.get("/products", response_model=list[TestProductView])
def list_products(db: Session = Depends(get_db)) -> list[TestProduct]:
    return list(db.scalars(product_query().order_by(TestProduct.created_at.desc())).unique())


@router.get("/products/{product_id}", response_model=TestProductView)
def get_product(product_id: str, db: Session = Depends(get_db)) -> TestProduct:
    product = db.scalar(product_query().where(TestProduct.id == product_id))
    if not product:
        raise HTTPException(status_code=404, detail="Test product not found")
    return product


@router.post("/products/{product_id}/verify-baseline", response_model=TestProductView, status_code=202)
async def build_product_baseline(product_id: str, request: Request, db: Session = Depends(get_db)) -> TestProduct:
    if not db.get(TestProduct, product_id):
        raise HTTPException(status_code=404, detail="Test product not found")
    try:
        await request.app.state.test_round_workflow.start_baseline(product_id)
    except TestWorkbenchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.expire_all()
    product = db.scalar(product_query().where(TestProduct.id == product_id))
    assert product
    return product


class PatchsetLookupRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


@router.post("/products/{product_id}/patchset-lookup")
async def lookup_product_patchsets(product_id: str, payload: PatchsetLookupRequest, db: Session = Depends(get_db)) -> dict:
    product = db.scalar(product_query().where(TestProduct.id == product_id))
    if not product:
        raise HTTPException(status_code=404, detail="Test product not found")
    try:
        return await lookup_patchsets(product, payload.url)
    except TestWorkbenchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/products/{product_id}/test-points", response_model=TestPointView, status_code=status.HTTP_201_CREATED)
async def create_test_point(product_id: str, payload: TestPointCreate, db: Session = Depends(get_db)) -> TestPoint:
    product = db.get(TestProduct, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Test product not found")
    expected_ref = f"refs/changes/{payload.change_number % 100:02d}/{payload.change_number}/{payload.patchset_number}"
    if payload.ref != expected_ref:
        raise HTTPException(status_code=422, detail=f"Gerrit ref must match Change/Patchset: {expected_ref}")
    try:
        await verify_patchset(product.tested_repository, product.sdk_repository, product.gerrit_project, payload.ref, payload.commit_sha)
    except TestWorkbenchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = db.scalar(select(TestPoint).where(TestPoint.product_id == product_id,
        TestPoint.change_number == payload.change_number, TestPoint.patchset_number == payload.patchset_number))
    if existing:
        if existing.commit_sha.lower() != payload.commit_sha.lower():
            raise HTTPException(status_code=409, detail='已登记的 Patch Set 与当前 Commit 不一致，请核对 Gerrit')
        return existing
    point = TestPoint(product_id=product_id, **payload.model_dump())
    try:
        db.add(point)
        db.commit()
        db.refresh(point)
        return point
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="This product already has the Gerrit Patchset") from exc


@router.get("/products/{product_id}/test-points", response_model=list[TestPointView])
def list_test_points(product_id: str, db: Session = Depends(get_db)) -> list[TestPoint]:
    if not db.get(TestProduct, product_id):
        raise HTTPException(status_code=404, detail="Test product not found")
    return list(db.scalars(select(TestPoint).where(TestPoint.product_id == product_id).order_by(TestPoint.created_at.desc())))


class TestCaseCategoryUpdate(BaseModel):
    category: Literal["basic", "special"]


@router.patch("/test-cases/{case_id}/category", response_model=TestCaseView)
def update_case_category(case_id: str, payload: TestCaseCategoryUpdate, db: Session = Depends(get_db)) -> TestCase:
    case = db.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Test Case not found")
    # Operator grouping does not modify immutable plugin commands or old round snapshots.
    case.category = payload.category
    db.commit()
    db.refresh(case)
    return case


@router.post("/benches", response_model=TestBenchView, status_code=status.HTTP_201_CREATED)
def create_bench(payload: TestBenchCreate, db: Session = Depends(get_db)) -> TestBench:
    remote_root = PurePosixPath(payload.remote_root)
    if not remote_root.is_absolute() or str(remote_root) == "/" or ".." in remote_root.parts:
        raise HTTPException(status_code=422, detail="Bench remote root must be a scoped absolute path without traversal")
    bench = TestBench(**payload.model_dump())
    try:
        db.add(bench)
        db.commit()
        db.refresh(bench)
        return bench
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Test bench name is already registered") from exc


@router.get("/benches", response_model=list[TestBenchView])
def list_benches(db: Session = Depends(get_db)) -> list[TestBench]:
    return list(db.scalars(select(TestBench).order_by(TestBench.name)))


@router.get('/benches/{bench_id}/readiness')
async def bench_readiness(bench_id: str, execution: Literal['full','delivery'] = 'full',
    db: Session = Depends(get_db), workflow: TestRoundWorkflow = Depends(get_round_workflow)) -> dict:
    bench = db.get(TestBench, bench_id)
    if not bench or not bench.enabled:
        raise HTTPException(status_code=404, detail='测试台不存在或已停用')
    from .testing_platform import bench_busy
    if bench_busy(db, bench_id):
        raise HTTPException(409, '测试台已被运行占用，暂不执行额外硬件检查')
    return await inspect_bench(workflow, bench, execution == 'full')


@router.get("/benches/{bench_id}/devices")
async def bench_devices(bench_id: str, chip: str = "RK3572", db: Session = Depends(get_db), workflow: TestRoundWorkflow = Depends(get_round_workflow)) -> list[dict]:
    import re
    import shlex
    bench = db.get(TestBench, bench_id)
    if not bench or not bench.enabled:
        raise HTTPException(status_code=404, detail="Bench not found")
    from .testing_platform import bench_busy
    if bench_busy(db, bench_id):
        raise HTTPException(409, '测试台已被运行占用；请使用该运行冻结的设备信息')
    if chip == 'RV1126B':
        import json
        result = await workflow._remote(bench, 'python3 ' + shlex.quote(bench.remote_root + '/rv1126b.py') + ' --list', 45)
        if result['exit_code']:
            raise HTTPException(status_code=422, detail='RMSL321 检测失败，请检查测试台连接和适配器安装')
        return json.loads(result['stdout_tail'])
    if chip != 'RK3572':
        raise HTTPException(status_code=422, detail='Unsupported chip')
    result = await workflow._remote(bench, shlex.quote(bench.remote_root + '/tools/upgrade-tool') + ' LD', 30)
    if result['exit_code'] != 0:
        raise HTTPException(status_code=422, detail="无法查询测试台烧录设备，请检查 Controller 安装和 SSH")
    return [{'serial':m[2], 'mode':m[1]} for m in re.finditer(r'Mode=(\w+)\s+SerialNo=(\S+)',result['stdout_tail'])]


@router.post("/test-rounds", response_model=TestRoundView, status_code=status.HTTP_201_CREATED)
async def create_test_round(
    payload: TestRoundCreate,
    db: Session = Depends(get_db),
    workflow: TestRoundWorkflow = Depends(get_round_workflow),
) -> TestRound:
    return await create_round_internal(payload, db, workflow)


async def create_round_internal(payload, db, workflow, *, source=None, schedule_job=None):
    if payload.developer_mode:
        from .developer_tests import create
        return await create(payload,db,workflow)
    product = db.scalar(product_query().where(TestProduct.id == payload.product_id))
    if not product:
        raise HTTPException(status_code=404, detail="Test product not found")
    if product.status != TestProductStatus.READY or not (product.baseline_result or {}).get("passed"):
        raise HTTPException(status_code=409, detail="Product baseline must pass before creating a Test Round")
    point = db.get(TestPoint, payload.test_point_id) if payload.test_point_id else None
    if source:
        from types import SimpleNamespace
        point = SimpleNamespace(id=None, product_id=product.id, ready=True, **{k: source[k] for k in ('change_number','patchset_number','commit_sha','subject','owner')}, submitted_at=None)
    if not point or point.product_id != product.id or not point.ready:
        raise HTTPException(status_code=422, detail="Test point must be a ready Patchset from the locked product")
    bench = db.get(TestBench, payload.bench_id)
    if not bench or not bench.enabled:
        raise HTTPException(status_code=404, detail="Test bench not found or disabled")
    if len(payload.test_case_ids) != len(set(payload.test_case_ids)):
        raise HTTPException(status_code=422, detail="Duplicate Test Cases are not allowed")
    cases_by_id = {
        item.id: item
        for item in db.scalars(select(TestCase).where(TestCase.id.in_(payload.test_case_ids), TestCase.enabled.is_(True)))
    }
    from .special_catalog import CODES as GATED_CODES
    if any(case.code in GATED_CODES for case in cases_by_id.values()):
        raise HTTPException(422,'人工门控用例须从特殊场景或发布清单入口执行')
    if len(cases_by_id) != len(payload.test_case_ids):
        raise HTTPException(status_code=422, detail="One or more Test Cases are missing or disabled")
    if set(payload.test_case_parameters) - set(payload.test_case_ids):
        raise HTTPException(status_code=422, detail='Parameters must belong to selected Test Cases')
    if payload.execute_test_cases and not payload.flash_firmware:
        raise HTTPException(status_code=422, detail='基础测试当前要求先烧录本轮固件，保证版本对应')
    case_snapshot = []
    for case_id in payload.test_case_ids:
        case = cases_by_id[case_id]
        adapter = None
        if payload.execute_test_cases:
            if case.code not in ACTIVE_CODES:
                raise HTTPException(status_code=422, detail=f'用例尚未适配：{case.code}')
            adapter = definition(case.code)
            if adapter.get('missing_capabilities'):
                raise HTTPException(422, 'Tool unsupported: '+', '.join(adapter['missing_capabilities']))
            parameters = payload.test_case_parameters.get(case.id, {})
            if case.code.startswith('INFO-') or case.code=='STREAM-RGB-DEPTH':
                from .basic_cases import validate_parameters
                validate_parameters(case.code, parameters)
            if set(parameters) - set(adapter.get('parameters', {})):
                raise HTTPException(status_code=422, detail=f'用例参数未定义：{case.code}')
            if 'target_fov' in parameters:
                import math
                value = parameters['target_fov']
                if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or not 0 < value < 180:
                    raise HTTPException(status_code=422, detail='target_fov 必须是 0 到 180 之间的有限数值')
        case_snapshot.append({
            "adapter": adapter,
            "id": case.id,
            "code": case.code,
            "name": case.name,
            "description": case.description,
            "command": case.command,
            "expected_exit_code": case.expected_exit_code,
            "timeout_seconds": case.timeout_seconds,
            "risk_level": case.risk_level.value,
            "category": case.category,
            "parameters": payload.test_case_parameters.get(case.id, {}),
        })
    readiness = None
    if payload.flash_firmware and product.board_config == BOARD:
        readiness = await inspect_bench(workflow, bench, True)
        if not readiness['passed']:
            failed = '；'.join(c['name']+'：'+c['detail'] for c in readiness['checks'] if not c['passed'])
            raise HTTPException(status_code=422, detail='测试台尚未就绪，未创建轮次：'+failed)
    number = next_round_number(db)
    product_snapshot = {
        "id": product.id,
        "code": product.code,
        "name": product.name,
        "sdk_repository_id": product.sdk_repository_id,
        "sdk_path": product.sdk_repository.root_path,
        "sdk_configuration": {key: getattr(product.sdk_repository, key) for key in ('name', 'root_path', 'board_configs', 'artifact_root', 'artifact_patterns')},
        "tested_repository_id": product.tested_repository_id,
        "tested_repository_path": product.tested_repository.root_path,
        "gerrit_project": product.gerrit_project,
        "kernel": product.kernel_baseline,
        "uboot": product.uboot_baseline,
        "rkbin": product.rkbin_baseline,
        "manifest": product.manifest_snapshot,
        "repository": product.repository_snapshot,
        "baseline_result": product.baseline_result,
        "test_point": {"subject": point.subject, "owner": point.owner, "submitted_at": point.submitted_at.isoformat() if point.submitted_at else None},
    }
    if source:
        product_snapshot['source_ref'] = source['ref']
        product_snapshot['source_kind'] = source['kind']
    product_snapshot["execute_test_cases"] = payload.execute_test_cases
    product_snapshot["sdk_configuration"]["artifact_patterns"] = artifact_patterns(product.board_config)
    if readiness:
        product_snapshot['bench_readiness'] = readiness
    if payload.flash_firmware:
        if not payload.device_serial:
            raise HTTPException(status_code=422, detail='请选择本轮被测设备')
        if product.board_config == 'BoardConfig-SPI_NAND-RK805-RV1126B_EVB1_V10_V11-DualCamDis-v2.0-arm64.mk':
            if not payload.execute_test_cases:
                raise HTTPException(status_code=422, detail='RMSL321 烧录必须包含启动版本确认和测试用例')
            devices = await bench_devices(bench.id, 'RV1126B', db, workflow)
            matches = [d for d in devices if d['serial'] == payload.device_serial]
            if len(matches) != 1:
                raise HTTPException(status_code=422, detail='所选 RMSL321 不在线或身份不唯一，请重新检测')
            product_snapshot['sdk_configuration']['artifact_patterns'] = ['oem.img', 'system.img', 'boot.img']
            from .basic_cases import ROOT, sha
            product_snapshot['flash'] = {'worker_sha256': sha(ROOT/'worker.py'), 'adapter_sha256': sha(ROOT/'rv1126b.py'),
                'enabled': True, 'adapter': 'rv1126b-dfu-v1', 'target_chip': 'RV1126B',
                'device_serial': payload.device_serial, 'usb_path': matches[0]['usb_path']}
        elif 'rk3572' in product.board_config.lower():
            product_snapshot['flash'] = {'enabled': True, 'target_chip': 'RK3572', 'device_serial': payload.device_serial}
        else:
            raise HTTPException(status_code=422, detail='当前板级配置没有经过验证的烧录适配器')
    test_round = TestRound(
        id=new_id(),
        display_number=number,
        product_id=product.id,
        test_point_id=point.id,
        bench_id=bench.id,
        product_snapshot=product_snapshot,
        bench_snapshot={key: getattr(bench, key) for key in ('id', 'name', 'host', 'port', 'user', 'remote_root')},
        manifest_commit=str(product.manifest_snapshot["manifest_commit"]),
        board_config=product.board_config,
        gerrit_project=product.gerrit_project,
        change_number=point.change_number,
        patchset_number=point.patchset_number,
        commit_sha=point.commit_sha.lower(),
        work_branch=f"codex/test-round-{number:06d}-change-{point.change_number}-ps{point.patchset_number}",
        test_case_snapshot=case_snapshot,
    )
    test_round.events.append(TestRoundEvent(
        sequence=1,
        event_type="created",
        status="succeeded",
        message="Product baseline, Gerrit Patchset, bench, and Test Case configuration were snapshotted",
        details={"product_id": product.id, "gerrit_project": product.gerrit_project},
    ))
    if schedule_job:
        from .models import TestSchedule
        frozen = db.get(TestSchedule, schedule_job.schedule_id).config['definitions']
        if any(frozen.get(c['id']) != c['adapter'] for c in case_snapshot):
            raise HTTPException(409, '用例或 tools 在准备过程中发生变化，未派发任务')
        test_round.origin = 'scheduled'
        test_round.schedule_id = schedule_job.schedule_id
    db.add(test_round)
    db.flush()
    if schedule_job:
        schedule_job.round_id = test_round.id
        schedule_job.source_commit = test_round.commit_sha
    db.commit()
    round_id = test_round.id
    if payload.auto_start:
        try:
            await workflow.start(round_id)
        except TestWorkbenchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.expire_all()
    created = db.scalar(round_query().where(TestRound.id == round_id))
    assert created
    return created


@router.post('/basic-case-definitions/install')
def install_basic_cases(db: Session = Depends(get_db)) -> dict:
    from .seed_basic_cases import install_missing
    added = install_missing(db)
    db.commit()
    from .special_catalog import CODES as SPECIAL_CODES
    return {'added': added, 'available': [*ACTIVE_CODES, *SPECIAL_CODES]}


@router.get('/basic-case-definitions')
def basic_case_definitions() -> list[dict]:
    return [definition(code) for code in ACTIVE_CODES]


@router.get('/test-rounds/{round_id}/cases/{code}/evidence')
async def case_evidence(round_id: str, code: str, kind: str = 'result', offset: int = 0, db: Session = Depends(get_db), workflow: TestRoundWorkflow = Depends(get_round_workflow)) -> dict:
    import base64
    import json
    import shlex
    files={'result':'result.json','input':'case-input.json','protocol':'protocol.jsonl','stdout':'stdout.log','stderr':'stderr.log','input-file':'inputs/payload.bin','output-file':'artifacts/download.bin'}
    current=db.get(TestRound,round_id)
    if not current or not current.remote_path or code not in CODES or kind not in files or offset < 0:
        raise HTTPException(status_code=404,detail='Evidence not found')
    if not any(c.get('code')==code for c in current.test_case_snapshot):
        raise HTTPException(status_code=404,detail='Case is not in this round')
    from .evidence_store import directory
    local=directory(current.display_number)/'cases'/code/'attempt-001'/files[kind]
    if local.is_file() and (directory(current.display_number)/'archive-complete.json').exists():
        with local.open('rb') as f:
            f.seek(offset);data=f.read(4096);end=f.tell()
        return {'data':base64.b64encode(data).decode(),'content':data.decode('utf-8',errors='replace'),'next_offset':end,'eof':end>=local.stat().st_size}
    path=current.remote_path+'/cases/'+code+'/attempt-001/'+files[kind]
    script="import pathlib,base64,json; p=pathlib.Path("+repr(path)+"); f=p.open('rb'); f.seek("+str(offset)+"); data=f.read(4096); print(json.dumps({'data':base64.b64encode(data).decode(),'next_offset':f.tell(),'eof':f.tell()>=p.stat().st_size}))"
    response=await workflow._remote(TestBench(**current.bench_snapshot),'python3 -c '+shlex.quote(script),20)
    if response['exit_code']!=0:
        raise HTTPException(status_code=404,detail='该证据文件尚未生成或测试台暂不可达')
    body=json.loads(response['stdout_tail'])
    body['content']=base64.b64decode(body['data']).decode('utf-8',errors='replace')
    return body


class CaseClassificationChange(BaseModel):
    outcome: Literal['passed', 'failed'] | None
    reason: str = Field(min_length=1, max_length=1000)
    operator: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)


@router.post('/test-rounds/{round_id}/cases/{code}/classification')
def change_case_classification(round_id: str, code: str, payload: CaseClassificationChange,
                               db: Session = Depends(get_db)):
    from .case_classification import validate_change
    # Serialize concurrent edits before reading the revision (also works on SQLite).
    db.execute(update(TestRound).where(TestRound.id == round_id).values(status=TestRound.status))
    row = db.scalar(round_query().where(TestRound.id == round_id))
    if not row:
        raise HTTPException(status_code=404, detail='Test Round not found')
    if not payload.reason.strip() or not payload.operator.strip():
        raise HTTPException(status_code=422, detail='请填写修改人及原因')
    try:
        current = validate_change(row, code, payload.revision)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sequence = (db.scalar(select(func.max(TestRoundEvent.sequence)).where(TestRoundEvent.test_round_id == round_id)) or 0) + 1
    db.add(TestRoundEvent(test_round_id=round_id, sequence=sequence,
        event_type='case_classification', status='recorded', message=code+' 人工修正结果分类',
        details={'code':code, 'outcome':payload.outcome, 'previous_outcome':current['outcome'],
                 'automatic_outcome':current['automatic_outcome'], 'reason':payload.reason.strip(),
                 'operator':payload.operator.strip(), 'identity_source':'operator_entered'}))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail='记录已更新，请刷新后重试') from exc
    db.expire_all()
    return TestRoundView.model_validate(db.scalar(round_query().where(TestRound.id == round_id))).case_classifications[code]


@router.get("/test-rounds", response_model=list[TestRoundView])
def list_test_rounds(product_id: str | None = None, db: Session = Depends(get_db)) -> list[TestRound]:
    query = round_query()
    if product_id:
        query = query.where(TestRound.product_id == product_id)
    return list(db.scalars(query.order_by(TestRound.created_at.desc())).unique())


@router.get("/test-rounds/{round_id}", response_model=TestRoundView)
def get_test_round(round_id: str, db: Session = Depends(get_db)) -> TestRound:
    test_round = db.scalar(round_query().where(TestRound.id == round_id))
    if not test_round:
        raise HTTPException(status_code=404, detail="Test Round not found")
    test_round.events.sort(key=lambda item: item.sequence)
    return test_round


@router.post("/test-rounds/{round_id}/start", response_model=TestRoundView)
async def start_test_round(
    round_id: str,
    db: Session = Depends(get_db),
    workflow: TestRoundWorkflow = Depends(get_round_workflow),
) -> TestRound:
    if not db.get(TestRound, round_id):
        raise HTTPException(status_code=404, detail="Test Round not found")
    try:
        await workflow.start(round_id)
    except TestWorkbenchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.expire_all()
    test_round = db.scalar(round_query().where(TestRound.id == round_id))
    assert test_round
    return test_round


@router.get('/test-rounds/{round_id}/report')
async def download_round_report(round_id: str, db: Session = Depends(get_db), workflow: TestRoundWorkflow = Depends(get_round_workflow)):
    from fastapi.responses import JSONResponse
    row = db.scalar(round_query().where(TestRound.id == round_id))
    if not row:
        raise HTTPException(status_code=404, detail='Test Round not found')
    report = TestRoundView.model_validate(row).model_dump(mode='json')
    import json
    report['case_results'] = []
    for case in row.test_case_snapshot:
        if case['code'] not in CODES or not row.remote_path or not row.product_snapshot.get('execute_test_cases'):
            continue
        try:
            offset = 0
            content = ''
            while offset < 1024 * 1024:
                page = await case_evidence(round_id, case['code'], 'result', offset, db, workflow)
                content += page['content']
                offset = page['next_offset']
                if page['eof']:
                    report['case_results'].append(json.loads(content))
                    break
            else:
                raise ValueError('Case report exceeds 1 MiB')
        except (HTTPException, ValueError, KeyError) as exc:
            report['case_results'].append({'id': case['code'], 'evidence_unavailable': str(getattr(exc, 'detail', exc))})
    report['report_schema'] = 'develop-hub-test-round/2'
    report['classification_note'] = 'case_classifications 为当前结果分类；verdict/controller_result/case_results 保留自动执行结论。'
    report['verdict'] = {
        'test_complete':'PASS', 'test_failed':'FAIL', 'bench_failed':'ERROR',
        'build_failed':'ERROR', 'blocked':'ERROR', 'remote_ready':'NOT_TESTED',
        'flash_complete':'NOT_TESTED',
    }.get(row.status.value, 'IN_PROGRESS')
    return JSONResponse(report, headers={'Content-Disposition': f'attachment; filename="TEST-{row.display_number:06d}.json"'})


@router.get('/test-rounds/{round_id}/cases/{code}/artifact')
async def case_artifact(round_id: str,code: str,name: str,db: Session=Depends(get_db),workflow: TestRoundWorkflow=Depends(get_round_workflow)):
    from fastapi.responses import FileResponse
    from .evidence_store import archive,artifact,directory
    row=db.get(TestRound,round_id)
    if not row or not row.remote_path or not any(c['code']==code for c in row.test_case_snapshot):raise HTTPException(404,'Unknown case')
    if (row.controller_result or {}).get('state') not in ('finished','interrupted','rejected'):raise HTTPException(409,'图片将在本轮完成后同步到 Hub')
    try:
        await archive(workflow,TestBench(**row.bench_snapshot),row.remote_path,row.display_number)
        path=artifact(directory(row.display_number),code,name)
    except (ValueError,OSError,RuntimeError) as exc:raise HTTPException(404,str(exc)) from exc
    return FileResponse(path,media_type='image/png' if path.suffix=='.png' else 'application/octet-stream',headers={'X-Content-Type-Options':'nosniff'})
