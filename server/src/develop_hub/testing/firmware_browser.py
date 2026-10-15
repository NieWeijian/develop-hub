"""Read-only directory listings within platform-registered firmware locations."""
import hashlib
import os
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

IMAGE_SUFFIXES = {'.img', '.bin', '.itb', '.dtb'}


def browse(roots: list[dict], root_id: str = '', relative: str = '') -> dict:
    locations = []
    for item in roots:
        if not Path(item['path']).is_absolute():
            continue
        path = Path(item['path']).resolve()
        if not path.is_absolute() or any(r['path'] == str(path) for r in locations):
            continue
        locations.append({'id': hashlib.sha256(str(path).encode()).hexdigest()[:16],
                          'name': item['name'], 'path': str(path)})
    if not locations:
        return {'roots': [], 'root_id': '', 'path': '', 'parent': None, 'directories': [], 'files': [], 'truncated': False}
    selected = next((r for r in locations if r['id'] == root_id), None) if root_id else locations[0]
    if selected is None:
        raise HTTPException(404, '目录入口已变化，请重新打开目录选择器')
    parts = PurePosixPath(relative)
    if parts.is_absolute() or '\\' in relative or '\x00' in relative or any(p.startswith('.') and p != '.' for p in parts.parts):
        raise HTTPException(403, '请选择开放目录中的文件夹')
    root = Path(selected['path'])
    current = root.joinpath(*parts.parts)
    try:
        if not current.resolve().is_relative_to(root):
            raise HTTPException(403, '此文件夹位于开放目录之外')
        directories, files, scanned = [], [], 0
        with os.scandir(current) as entries:
            for entry in entries:
                scanned += 1
                if scanned > 10000:
                    raise HTTPException(422, '此目录项目过多，请通过路径输入直接指定镜像目录')
                if entry.name.startswith('.'):
                    continue
                child = current / entry.name
                try:
                    if not child.resolve().is_relative_to(root):
                        continue
                    path = child.relative_to(root).as_posix()
                    if entry.is_dir():
                        directories.append({'name': entry.name, 'path': path})
                    elif entry.is_file() and child.suffix.lower() in IMAGE_SUFFIXES:
                        files.append({'name': entry.name, 'path': str(child), 'size_bytes': entry.stat().st_size})
                except (OSError, RuntimeError):
                    continue  # A disappearing or inaccessible child must not break the directory.
        directories.sort(key=lambda d: d['name'].casefold())
        files.sort(key=lambda f: f['name'].casefold())
        path = current.relative_to(root).as_posix()
        return {'roots': locations, 'root_id': selected['id'], 'path': '' if path == '.' else path,
                'parent': None if current == root else '' if current.parent == root else current.parent.relative_to(root).as_posix(),
                'directories': directories[:1000], 'files': files[:1000],
                'truncated': len(directories) > 1000 or len(files) > 1000}
    except PermissionError as error:
        raise HTTPException(403, '平台服务没有读取此文件夹的权限') from error
    except (FileNotFoundError, NotADirectoryError) as error:
        raise HTTPException(404, '文件夹不存在或已被移动，请返回上级重新选择') from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(422, '无法读取此文件夹，请选择其他目录') from error
