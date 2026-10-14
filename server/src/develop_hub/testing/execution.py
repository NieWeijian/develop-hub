"""Transport and strict result validation for registered execution nodes."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sys


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def checked_child(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if root.resolve() not in path.parents:
        raise ValueError('文件路径不在登记目录内')
    return path


async def command(argv: list[str], timeout: int = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # This stops the transport, not an assumed remote hardware operation.
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        raise
    return proc.returncode or 0, stdout.decode(errors='replace'), stderr.decode(errors='replace')


def ssh_args(node: dict, scp: bool = False) -> list[str]:
    host, user = node.get('host', ''), node.get('user', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', user):
        raise ValueError('SSH 节点地址或用户名无效')
    argv = ['scp' if scp else 'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-P' if scp else '-p', str(int(node.get('port', 22)))]
    if node.get('key'):
        argv += ['-i', node['key']]
    return argv


async def execute_step(node: dict, folder: Path, job: dict, package: dict | None = None, firmware: dict | None = None) -> dict:
    from .safety import require_simulation
    require_simulation(node)
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / 'job.json').exists():
        raise RuntimeError('执行步骤已经创建，禁止自动重复下发')
    if package:
        source = Path(package['root_path'])
        # The archive importer already checked all paths. Recheck when copying.
        for path in source.rglob('*'):
            if path.is_symlink():
                raise ValueError('用例包不能包含符号链接')
        expected = {item['path']: item['sha256'] for item in package['files']}
        actual = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob('*') if p.is_file()}
        if actual != expected:
            raise ValueError('用例包存储内容已改变，请重新导入')
        shutil.copytree(source, folder / 'package')
    if firmware:
        target = folder / 'firmware'
        target.mkdir()
        for item in firmware['files']:
            source = checked_child(Path(firmware['root_path']), item['path'])
            if digest(source) != item['sha256']:
                raise ValueError('固件内容已改变')
            destination = checked_child(target, item['path'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        job['input']['firmware'] = {**firmware, 'root_path': 'firmware'}
    worker = Path(__file__).resolve().parents[4] / 'bench' / 'testing_node.py'
    shutil.copy2(worker, folder / 'node.py')
    job['job_schema'] = 'rk-test-step/1'
    job['files'] = [{'path': p.relative_to(folder).as_posix(), 'sha256': digest(p)} for p in sorted(folder.rglob('*')) if p.is_file()]
    (folder / 'job.json').write_text(json.dumps(job, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    kind = node.get('kind', 'local')
    timeout = int(job.get('timeout_seconds', 120)) + 60
    if kind == 'local':
        code, stdout, stderr = await command([node.get('python', sys.executable), str(folder / 'node.py'), '--step-dir', str(folder)], timeout)
    elif kind == 'ssh':
        root = node.get('root', '')
        if not root.startswith('/') or '..' in PurePosixPath(root).parts or root == '/':
            raise ValueError('SSH 节点需要专用绝对工作目录')
        destination = f"{node['user']}@{node['host']}"
        remote = str(PurePosixPath(root) / folder.parent.name / folder.name)
        code, _, error = await command(ssh_args(node) + [destination, 'mkdir -p -- ' + shlex.quote(str(PurePosixPath(remote).parent)) + ' && test ! -e ' + shlex.quote(remote)])
        if code:
            raise RuntimeError('远端步骤目录已存在或不可用，禁止重复执行: ' + error[-300:])
        code, _, error = await command(ssh_args(node, True) + ['-r', str(folder), destination + ':' + remote], 600)
        if code:
            raise RuntimeError('任务传输失败: ' + error[-300:])
        argv = [node.get('python', 'python3'), remote + '/node.py', '--step-dir', remote]
        code, stdout, stderr = await command(ssh_args(node) + [destination, shlex.join(argv)], timeout)
        # Read the durable result before transferring attachments.
        if stdout.strip():
            preliminary = json.loads(stdout)
            for item in preliminary.get('evidence', []):
                path = checked_child(folder, item['path'])
                if item['size_bytes'] > 64 * 1024 * 1024:
                    raise RuntimeError('证据超过单文件归档上限')
                path.parent.mkdir(parents=True, exist_ok=True)
                rc, _, err = await command(ssh_args(node, True) + [destination + ':' + remote + '/' + item['path'], str(path)], 300)
                if rc or digest(path) != item['sha256']:
                    raise RuntimeError('证据归档或校验失败: ' + err[-200:])
    else:
        raise ValueError('该节点类型不能执行真实命令')
    if code or not stdout.strip():
        raise RuntimeError('执行节点异常: ' + (stderr or stdout)[-1000:])
    result = json.loads(stdout)
    if result.get('job_digest') != digest(folder / 'job.json') or result.get('run_id') != job['input']['run_id']:
        raise RuntimeError('执行结果与任务身份不匹配')
    (folder / 'node-result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    if result.get('state') != 'completed':
        raise RuntimeError(result.get('error', '执行节点未完成'))
    return result


def validate_case_result(result: dict, case: dict, run_id: str) -> dict:
    data = result.get('data', {})
    expected_identity = (run_id, case['binding_id'], case['case_revision_id'])
    if data.get('result_schema') != 'rk-case-result/1' or tuple(data.get(k) for k in ('run_id', 'binding_id', 'case_revision_id')) != expected_identity:
        raise ValueError('用例结果身份或格式不匹配')
    if data.get('state') != 'completed':
        raise ValueError('用例未正常完成')
    assertions = data.get('assertions', [])
    if not assertions or any(not isinstance(a, dict) or not isinstance(a.get('passed'), bool) or not a.get('id') or 'expected' not in a or 'actual' not in a for a in assertions):
        raise ValueError('用例缺少完整检查项')
    ids = [a['id'] for a in assertions]
    if len(ids) != len(set(ids)) or not set(case['definition'].get('required_assertions', [])).issubset(ids):
        raise ValueError('必需检查项缺失或重复')
    recovery = data.get('recovery', {}).get('state')
    required = case['definition'].get('recovery', {}).get('required', False)
    if recovery not in {'not_required', 'restored', 'failed'} or (required and recovery == 'not_required'):
        raise ValueError('恢复记录不完整')
    verdict = 'passed' if all(a['passed'] for a in assertions) else 'failed'
    if result.get('exit_code') != 0:
        verdict = 'error'
    if recovery == 'failed':
        verdict = 'error'
    if data.get('verdict') != verdict:
        raise ValueError('用例结论与检查项、退出码或恢复状态不一致')
    evidence_paths = {i['path'] for i in result.get('evidence', [])}
    for item in data.get('attachments', []):
        path = item.get('path', '')
        if not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or 'output/' + path not in evidence_paths:
            raise ValueError('用例附件没有对应的已校验证据')
    return {**data, 'evidence': result.get('evidence', [])}
