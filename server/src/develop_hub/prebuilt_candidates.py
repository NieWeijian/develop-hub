"""Import already built SDK images without fabricating a source build or Git point."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import stat

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .bench_readiness import BOARD
from .config import settings
from .db import get_db
from .firmware_identity import validate_identity
from .models import ReleaseCandidate, TestProduct, TestRound, new_id
from .test_workbench import now
from .test_workbench_api import product_query

FILENAMES = ('oem.img', 'system.img', 'boot.img')


class PrebuiltCreate(BaseModel):
    product_id: str
    name: str = Field(min_length=1, max_length=160)
    image_directory: str = Field(min_length=1, max_length=4096)
    operator: str = Field(min_length=1, max_length=120)
    identity: dict[str, str]


def product_root(db, product_id):
    product = db.scalar(product_query().where(TestProduct.id == product_id))
    if not product:
        raise HTTPException(404, '产品不存在')
    if product.board_config != BOARD:
        raise HTTPException(422, '现成镜像导入目前支持 RMSL321 RV1126B 产品的 boot/system/oem 镜像')
    if not product.sdk_repository or not product.sdk_repository.enabled:
        raise HTTPException(409, '产品关联的 SDK 已停用')
    root = Path(product.sdk_repository.root_path).resolve()
    if not root.is_dir():
        raise HTTPException(409, '产品 SDK 目录不可用')
    return product, root


def options(product_id: str, db: Session = Depends(get_db)):
    product, root = product_root(db, product_id)
    configured = Path(product.sdk_repository.artifact_root)
    recommended = (configured if configured.is_absolute() else root/configured).resolve()
    if not recommended.is_relative_to(root):
        recommended = root/'output/image'
    return {'product_id': product.id, 'allowed_root': str(root),
            'recommended_image_directory': str(recommended), 'image_directory': str(recommended),
            'required_filenames': list(FILENAMES), 'board_config': product.board_config}


def stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def checked_source(path, root):
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise HTTPException(422, '镜像实际路径必须位于产品 SDK 目录内：'+path.name)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise HTTPException(422, '镜像必须是非空普通文件：'+path.name)
    return resolved, stamp(info)


def freeze_image(path, expected, target):
    # Open the already resolved path without following a replaced final symlink.
    source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(source_fd, 'rb') as source:
        if Path('/proc/self/fd/'+str(source.fileno())).resolve() != path:
            raise HTTPException(409, '镜像路径在打开时发生变化，请重新导入')
        if stamp(os.fstat(source.fileno())) != expected:
            raise HTTPException(409, '镜像正在变化，请等待本地构建完成后重新导入')
        checksum = hashlib.sha256()
        with target.open('xb') as destination:
            for chunk in iter(lambda: source.read(1024*1024), b''):
                checksum.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if stamp(os.fstat(source.fileno())) != expected:
            raise HTTPException(409, '复制期间源镜像发生变化，未创建候选固件')
    from .test_workbench import sha256_file
    digest = checksum.hexdigest()
    if target.stat().st_size != expected[2] or sha256_file(target) != digest:
        raise HTTPException(409, '冻结镜像大小或 SHA-256 校验失败')
    return {'filename': target.name, 'local_path': str(target), 'size_bytes': expected[2], 'sha256': digest}


def create(payload: PrebuiltCreate, db: Session = Depends(get_db)):
    from .testing_platform import ACTIVE, digest, image_identity
    from .test_workbench import sha256_file
    name, operator = payload.name.strip(), payload.operator.strip()
    if not name or not operator:
        raise HTTPException(422, '候选版本名与操作者不能为空')
    try:
        identity = validate_identity(payload.identity)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    product, root = product_root(db, payload.product_id)
    for active in db.scalars(select(TestRound).where(TestRound.status.in_(ACTIVE))):
        if active.status.value in ('checking_out', 'building', 'packaging') and active.origin != 'release' and not active.product_snapshot.get('developer_mode'):
            if active.product_snapshot.get('sdk_repository_id') == product.sdk_repository_id:
                raise HTTPException(409, '该 SDK 正在构建，请等待构建完成再导入现成镜像')
    raw_directory = Path(payload.image_directory.strip())
    if not raw_directory.is_absolute():
        raise HTTPException(422, '请输入 175 上的绝对目录路径')
    if db.scalar(select(ReleaseCandidate.id).where(ReleaseCandidate.product_id == product.id, ReleaseCandidate.name == name)):
        raise HTTPException(409, '同一产品的候选版本名已存在，请使用新的名称')
    folder = None
    folder_created = False
    try:
        directory = raw_directory.resolve(strict=True)
        if not directory.is_dir() or not directory.is_relative_to(root):
            raise HTTPException(422, '镜像目录必须位于产品 SDK 目录内')
        sources = {name: checked_source(directory/name, root) for name in FILENAMES}
        cid = new_id()
        folder = settings.artifact_root/'release-candidates'/cid
        folder.mkdir(parents=True, exist_ok=False)
        folder_created = True
        images = [freeze_image(path, before, folder/name) for name, (path, before) in sources.items()]
        # Check every source again after all copies: never freeze mixed build generations.
        for image in images:
            name = image['filename']
            path, before = sources[name]
            current, after = checked_source(directory/name, root)
            if current != path or before != after or sha256_file(current) != image['sha256'] or stamp(current.stat()) != before:
                raise HTTPException(409, '导入期间源镜像发生变化，请等待构建完成后重新导入')
        imported_at = now().isoformat()
        source = {'kind': 'prebuilt', 'image_directory': str(directory), 'operator': operator,
                  'imported_at': imported_at, 'validation': {'passed': True, 'checked_at': imported_at,
                                                           'files': image_identity(images)}}
        product_snapshot = {'id': product.id, 'code': product.code, 'name': product.name,
            'sdk_repository_id': product.sdk_repository_id, 'sdk_path': str(root),
            'sdk_configuration': {key: deepcopy(getattr(product.sdk_repository, key)) for key in ('name','root_path','board_configs','artifact_root','artifact_patterns')},
            'firmware_source': source, 'firmware_identity': identity,
            'test_point': {'subject': '现成镜像 · '+payload.name.strip()}}
        snapshot = {'images': images, 'commit': identity['value'] if identity['kind'] == 'commit' else '',
            'subject': product_snapshot['test_point']['subject'], 'board_config': product.board_config,
            'manifest_commit': '', 'build': None, 'product': product_snapshot, 'source': source, 'identity': identity}
        row = ReleaseCandidate(id=cid, product_id=product.id, source_round_id=None, name=payload.name.strip(),
                               fingerprint=digest(image_identity(images)), snapshot=snapshot)
        db.add(row)
        db.commit()
        return {'id': row.id}
    except Exception as exc:
        db.rollback()
        if folder_created and folder is not None and folder.is_dir():
            shutil.rmtree(folder)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, IntegrityError):
            raise HTTPException(409, '同一产品的候选版本名已存在') from exc
        if isinstance(exc, (OSError, RuntimeError)):
            raise HTTPException(422, '镜像导入失败：'+str(exc)) from exc
        raise
