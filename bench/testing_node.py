#!/usr/bin/env python3
"""One durable, versioned testing step. No scheduler or implicit retries.

The control plane copies a registered package and immutable input to a unique
directory. Re-running the same step returns its saved result; interrupted steps
are rejected rather than repeating a device operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save(path, data):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def contained(root, name):
    path = (root / name).resolve()
    if path != root and root not in path.parents:
        raise ValueError('Path escapes step directory')
    return path


def execute(folder):
    import fcntl
    folder = folder.resolve()
    with (folder / 'step.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        job_path = folder / 'job.json'
        identity = digest(job_path)
        saved = folder / 'node-result.json'
        if saved.exists():
            previous = json.loads(saved.read_text())
            if previous.get('job_digest') != identity:
                raise ValueError('Step identity changed')
            return previous
        if (folder / 'STARTED').exists():
            raise RuntimeError('Interrupted step requires inspection; automatic retry disabled')
        job = json.loads(job_path.read_text())
        if job.get('job_schema') != 'rk-test-step/1':
            raise ValueError('Unsupported testing step protocol')
        for item in job.get('files', []):
            path = contained(folder, item['path'])
            if path.is_symlink() or not path.is_file() or digest(path) != item['sha256']:
                raise ValueError('Input integrity check failed: ' + item['path'])
        action = job['action']
        output = folder / 'output'
        output.mkdir(exist_ok=True)
        save(folder / 'input.json', job['input'])
        if action == 'case':
            entry = contained(folder, job['entrypoint'])
            if not entry.is_file():
                raise ValueError('Case entrypoint not found')
            argv = [sys.executable, str(entry), '--input', str(folder / 'input.json'), '--output-dir', str(output)]
            cwd = contained(folder, job.get('cwd', 'package'))
        else:
            argv = job.get('argv')
            if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\x00' in a for a in argv):
                raise ValueError('Registered adapter command is missing')
            argv = [a.replace('{input}', str(folder / 'input.json')).replace('{output}', str(output)) for a in argv]
            cwd = folder
        # The durable marker is written before any process may touch the device.
        (folder / 'STARTED').write_text(identity)
        result = {'job_digest': identity, 'run_id': job['input']['run_id'], 'action': action}
        try:
            with (output / 'stdout.log').open('wb') as stdout, (output / 'stderr.log').open('wb') as stderr:
                proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
                try:
                    rc = proc.wait(timeout=min(max(int(job.get('timeout_seconds', 120)), 1), 86400))
                except subprocess.TimeoutExpired:
                    import signal
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    raise RuntimeError('Step timed out; device state must be checked')
            result['exit_code'] = rc
            if rc and action != 'case':
                with (output / 'stderr.log').open('rb') as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 2000))
                    detail = stream.read().decode('utf-8', errors='replace').strip()
                if detail == 'Board and firmware must explicitly identify the supported RMSL321 RV1126B board':
                    detail = '镜像目标板型缺失或与设备不一致，烧录前检查未通过'
                stage = '烧录程序' if action == 'deploy' else '设备检查程序'
                raise RuntimeError(f'{stage}退出（退出码 {rc}）：{detail or "未返回错误说明，请查看执行日志"}')
            if action == 'inspect':
                result['data'] = json.loads((output / 'stdout.log').read_text()) if rc == 0 else {}
            else:
                filename = 'result.json' if action == 'case' else 'deployment-result.json'
                target = output / filename
                if target.is_symlink() or not target.is_file() or target.stat().st_size > 2 * 1024 * 1024:
                    raise RuntimeError('Structured step result is missing or invalid')
                result['data'] = json.loads(target.read_text())
            result['state'] = 'completed'
        except Exception as exc:
            result.update(state='error', error=str(exc))
        result['evidence'] = []
        for path in output.rglob('*'):
            if path.is_symlink():
                raise ValueError('Evidence symlink rejected')
            if path.is_file():
                result['evidence'].append({'path': path.relative_to(folder).as_posix(), 'size_bytes': path.stat().st_size, 'sha256': digest(path)})
        save(saved, result)
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--step-dir', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(execute(Path(args.step_dir)), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'state': 'error', 'error': str(error)}))
        raise SystemExit(1)
