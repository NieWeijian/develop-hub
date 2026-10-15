from __future__ import annotations

import asyncio
import os
import zipfile
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from . import service
from .models import BuildEnvironment, CaseRevision, Device, InputAsset, Product, TestPlan, TestRun
from .execution import checked_child, digest

router = APIRouter(prefix='/api/testing', tags=['testing-inputs'])


class FirmwareImport(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    paths: list[str] = Field(min_length=1, max_length=100)
    product_id: str | None = None
    chip: str | None = None
    board_config: str | None = None
    expected_version: str | None = None


class SourceImport(BaseModel):
    environment_id: str
    repository_overrides: list[dict] = Field(default_factory=list)


class TrustPackage(BaseModel):
    trusted: bool


def asset(db, kind, name, manifest):
    row = InputAsset(kind=kind, name=name, digest=manifest['digest'], manifest=manifest)
    db.add(row)
    db.commit()
    return service.view(row)


def input_root():
    root = settings.artifact_root / 'testing' / 'assets'
    root.mkdir(parents=True, exist_ok=True)
    return root


@router.get('/assets')
def assets(kind: str | None = None, db: Session = Depends(get_db)):
    query = select(InputAsset).order_by(InputAsset.created_at.desc())
    if kind:
        query = query.where(InputAsset.kind == kind)
    return [service.view(a) for a in db.scalars(query)]


@router.post('/build-environments/{identity}/inspect')
async def inspect_environment(identity: str, db: Session = Depends(get_db)):
    from .sources import inspect_sdk
    environment = service.require(db, BuildEnvironment, identity)
    if environment.node.get('kind', 'local') != 'local':
        raise HTTPException(422, 'SDK 检查需要控制端的本地构建环境')
    try:
        return await asyncio.to_thread(inspect_sdk, environment.config.get('sdk_path', ''))
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error


@router.post('/source-snapshots', status_code=201)
async def source_snapshot(payload: SourceImport, db: Session = Depends(get_db)):
    from .sources import capture_source
    environment = service.require(db, BuildEnvironment, payload.environment_id)
    if environment.node.get('kind', 'local') != 'local':
        raise HTTPException(422, '首批支持控制端本地 SDK 快照')
    try:
        manifest = await asyncio.to_thread(capture_source, environment.config.get('sdk_path', ''), input_root() / str(uuid4()), environment.config.get('input_rules'), payload.repository_overrides)
        return asset(db, 'source_snapshot', environment.name + ' 源码快照', manifest)
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error


@router.post('/firmware-imports', status_code=201)
async def firmware_import(payload: FirmwareImport, db: Session = Depends(get_db)):
    from .sources import import_firmware
    board = payload.board_config
    if payload.product_id:
        product = service.require(db, Product, payload.product_id)
        if not board:
            board = service.product_firmware_board(db, product)
    try:
        manifest = await asyncio.to_thread(import_firmware, payload.paths, input_root() / str(uuid4()))
        if payload.product_id:
            manifest['product_id'] = payload.product_id
            product = service.require(db, Product, payload.product_id)
            manifest['chip'] = product.chip
            manifest['board_config'] = board
            manifest['board_source'] = 'declared_product_target'
        if payload.chip:
            manifest['chip'] = payload.chip
        if payload.board_config:
            manifest['board_config'] = payload.board_config
        if payload.expected_version:
            manifest['expected_version'] = payload.expected_version
        return asset(db, 'firmware_bundle', payload.name, manifest)
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error


@router.get('/firmware-directories')
async def firmware_directories(root_id: str = '', path: str = '', db: Session = Depends(get_db)):
    from .firmware_browser import browse
    configured = os.environ.get('DEVELOP_HUB_FIRMWARE_BROWSE_ROOTS', '')
    if configured:
        roots = [{'name': Path(p).name or p, 'path': p} for p in configured.split(os.pathsep) if p and Path(p).is_absolute()]
    else:
        roots = [{'name': e.name, 'path': e.config['sdk_path']} for e in db.scalars(select(BuildEnvironment).where(BuildEnvironment.enabled.is_(True)))
                 if e.node.get('kind', 'local') == 'local' and e.config.get('sdk_path') and Path(e.config['sdk_path']).is_absolute()]
    return await asyncio.to_thread(browse, roots, root_id, path)


async def bounded_upload(request, path, limit):
    count = 0
    with path.open('wb') as stream:
        async for chunk in request.stream():
            count += len(chunk)
            if count > limit:
                raise HTTPException(413, '文件超过上传大小上限')
            stream.write(chunk)
    return count


@router.post('/case-imports', status_code=201)
async def import_case(request: Request, db: Session = Depends(get_db)):
    from .packages import import_case_package
    path = input_root() / (str(uuid4()) + '.zip')
    try:
        await bounded_upload(request, path, 64 * 1024 * 1024)
        manifest = await asyncio.to_thread(import_case_package, path, input_root() / 'packages')
        return asset(db, 'case_package', manifest['manifest'].get('name', manifest['package_id']), manifest)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        raise HTTPException(422, str(error)) from error
    finally:
        path.unlink(missing_ok=True)


@router.post('/case-imports/{identity}/publish', status_code=201)
def publish_package(identity: str, payload: TrustPackage, db: Session = Depends(get_db)):
    row = service.require(db, InputAsset, identity)
    if row.kind != 'case_package' or payload.trusted is not True:
        raise HTTPException(422, '请确认用例代码来自获准执行的来源')
    manifest = deepcopy(row.manifest)
    root = Path(manifest['root_path'])
    for item in manifest['files']:
        path = checked_child(root, item['path'])
        if not path.is_file() or digest(path) != item['sha256']:
            raise HTTPException(409, '用例包内容已变化，请重新导入')
    rows = []
    for case in manifest['cases']:
        definition = deepcopy(case)
        definition['implementation'] = {**definition.get('implementation', {}), 'package_asset_id': identity}
        created = service.create_case(db, {'case_id': case['case_id'], 'version': case['case_version'], 'name': case['name'], 'mode': case.get('mode', 'automated'), 'definition': definition}, allow_package=True)
        rows.append(created)
    manifest['trusted'] = True
    row.manifest = manifest
    db.commit()
    return {'cases': [service.view(c) for c in rows], 'asset': service.view(row)}


@router.post('/test-runs/{identity}/evidence', status_code=201)
async def upload_evidence(identity: str, request: Request, name: str = '附件', db: Session = Depends(get_db)):
    run = service.require(db, TestRun, identity)
    if run.state != 'awaiting_operator':
        raise HTTPException(409, '当前运行不在人工记录阶段')
    evidence_id = str(uuid4())
    folder = settings.artifact_root / 'testing' / 'runs' / identity / 'attachments'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / evidence_id
    size = await bounded_upload(request, path, 20 * 1024 * 1024)
    record = {'evidence_id': evidence_id, 'name': name[:160], 'path': str(path), 'size_bytes': size, 'sha256': digest(path)}
    db.refresh(run)
    run.result = {**run.result, 'evidence': [*run.result.get('evidence', []), record]}
    db.commit()
    return {k: v for k, v in record.items() if k != 'path'}


@router.get('/test-runs/{identity}/evidence/{evidence_id}')
def download_evidence(identity: str, evidence_id: str, db: Session = Depends(get_db)):
    run = service.require(db, TestRun, identity)
    record = next((e for e in run.result.get('evidence', []) if e['evidence_id'] == evidence_id), None)
    if not record:
        raise HTTPException(404, '附件不存在')
    root = settings.artifact_root / 'testing' / 'runs' / identity
    path = Path(record['path']).resolve()
    if root.resolve() not in path.parents or not path.is_file() or digest(path) != record['sha256']:
        raise HTTPException(409, '附件完整性校验未通过')
    return FileResponse(path, filename=record['name'], media_type='application/octet-stream')


@router.post('/demo', status_code=201)
def demo(db: Session = Depends(get_db)):
    existing = db.scalar(select(CaseRevision).where(CaseRevision.case_id == 'demo.temperature'))
    if existing:
        return {'created': False, 'message': '模拟示例已存在'}
    product = Product(name='示例 RK 产品（模拟）', chip='SIMULATED', board='演示板', capabilities=['demo.temperature'])
    db.add(product)
    db.flush()
    device = Device(name='模拟测试台', product_id=product.id, node={'kind': 'simulation'}, config={'serial': 'SIM-001'}, capabilities=['demo.temperature'])
    db.add(device)
    case = service.create_case(db, {'case_id': 'demo.temperature', 'version': '1.0.0', 'name': '温度阈值检查（模拟）', 'mode': 'automated', 'definition': {'implementation': {'runtime_profile': 'simulation'}, 'requirements': {'capabilities': ['demo.temperature']}, 'parameters_schema': {'type': 'object', 'additionalProperties': False, 'properties': {'limit_c': {'type': 'number', 'minimum': 0, 'maximum': 100, 'default': 75}}, 'required': ['limit_c']}, 'parameter_ui': {'limit_c': {'label': '温度上限', 'unit': '°C'}}, 'required_assertions': ['demo-measurement'], 'effects': 'read_only'}})
    manual = service.create_case(db, {'case_id': 'example.appearance', 'version': '1.0.0', 'name': '外观与连接检查', 'mode': 'manual', 'definition': {'steps': [{'id': 'check', 'title': '检查设备外观和连接', 'instruction': '确认外观完整、线缆连接可靠，并填写检查说明。', 'required_fields': ['说明']}]}})
    db.flush()
    plan = TestPlan(name='体验完整测试流程（模拟）', draft={'product_id': product.id, 'firmware': {'kind': 'current_device'}, 'device_id': device.id, 'cases': [{'binding_id': 'temperature', 'case_revision_id': case.id, 'required': True, 'parameter_overrides': {}}, {'binding_id': 'appearance', 'case_revision_id': manual.id, 'required': True, 'parameter_overrides': {}}], 'acceptance': {'mode': 'all_required_cases'}, 'policy': {'independent_case_failure': 'continue'}})
    db.add(plan)
    db.commit()
    return {'created': True, 'plan_id': plan.id, 'message': '已创建明确标记的模拟示例，不操作真实设备'}
