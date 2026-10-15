#!/usr/bin/env python3
"""Prepare native production packages; stable changes require explicit maintenance gates."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub


def package(root, release_id):
    release = root / 'releases' / release_id
    verified = json.loads((release / 'manifest.json').read_text())
    if verified['profile'] != 'production' or not verified['tests_passed'] or not verified['frontend_passed']:
        raise ValueError('Prepare and verify a production-profile release first')
    destination = root / 'packages' / release_id
    commit = hub.export_source(hub.REPO, verified['commit'], destination)
    shutil.copytree(release / 'source/web/dist', destination / 'web/dist')
    cache = root / 'runtime-cache' / verified['dependency_key']
    cache.mkdir(parents=True, exist_ok=True)
    if not (cache / 'node').exists() or not (cache / 'web-node_modules').exists():
        name = 'dhdev-export-' + uuid.uuid4().hex[:12]
        hub.run(['docker', 'create', '--name', name, '--network=none', '--read-only', verified['image_id'], 'true'])
        try:
            if not (cache / 'node').exists():
                hub.run(['docker', 'cp', name + ':/opt/node', cache / 'node'])
            if not (cache / 'web-node_modules').exists():
                hub.run(['docker', 'cp', name + ':/opt/web/node_modules', cache / 'web-node_modules'])
        finally:
            hub.remove(name)
    wheels = cache / 'wheels-cp310'
    wheels.mkdir(exist_ok=True)
    lock = destination / 'requirements/production-cp310-linux-amd64.lock'
    argv = hub.common() + ['--rm', '--mount', f'type=bind,src={destination}/requirements,dst=/requirements,readonly', '--mount', f'type=bind,src={wheels},dst=/wheels', '-e', 'HOME=/tmp', verified['image_id'], 'python', '-m', 'pip', 'download', '--no-deps', '--only-binary=:all:', '--require-hashes', '--python-version', '3.10', '--implementation', 'cp', '--abi', 'cp310']
    for value in ['manylinux_2_35_x86_64', 'manylinux_2_34_x86_64', 'manylinux_2_31_x86_64', 'manylinux_2_28_x86_64', 'manylinux_2_24_x86_64', 'manylinux_2_17_x86_64', 'manylinux2014_x86_64']:
        argv += ['--platform', value]
    hub.run(argv + ['-r', '/requirements/production-cp310-linux-amd64.lock', '-d', '/wheels'])
    runtime = destination / 'runtime'
    runtime.mkdir()
    for source, target in [(cache / 'node', runtime / 'node'), (cache / 'web-node_modules', runtime / 'web-node_modules'), (wheels, runtime / 'wheels')]:
        shutil.copytree(source, target, symlinks=True, copy_function=os.link)
    (destination / 'web/node_modules').symlink_to('../runtime/web-node_modules')
    checksums = {}
    for folder, directories, files in os.walk(destination, followlinks=False):
        for name in files:
            path = Path(folder) / name
            if path.is_symlink():
                continue
            checksums[path.relative_to(destination).as_posix()] = hub.sha(path)
    manifest = dict(verified, native_python='3.10', minimum_glibc='2.35', package_commit=commit, file_hashes=checksums)
    hub.write_json(destination / 'package.json', manifest)
    print('Production package:', destination, 'commit:', commit)
    return destination


def verify_package(path):
    path = path.absolute()
    if path.resolve() != path:
        raise ValueError('Package path must not be a symlink')
    manifest = json.loads((path / 'package.json').read_text())
    if manifest['profile'] != 'production' or not manifest['tests_passed'] or not manifest['frontend_passed']:
        raise ValueError('Unverified package')
    for name, expected in manifest['file_hashes'].items():
        file = path / name
        if not file.resolve().is_relative_to(path) or hub.sha(file) != expected:
            raise ValueError('Package content mismatch: ' + name)
    return manifest


def setup_native(path, python):
    version = hub.run([python, '-c', 'import sys;print(str(sys.version_info.major)+"."+str(sys.version_info.minor))'], capture=True)
    if version != '3.10' or platform.machine() != 'x86_64' or platform.system() != 'Linux':
        raise RuntimeError('Native production package requires Linux AMD64 and host CPython 3.10')
    libc, version = platform.libc_ver()
    if libc != 'glibc' or tuple(int(v) for v in version.split('.')[:2]) < (2, 35):
        raise RuntimeError('Native production package requires glibc 2.35+')
    venv = path / '.venv'
    config = venv / 'pyvenv.cfg'
    if config.exists():
        values = dict(line.split(' = ', 1) for line in config.read_text().splitlines() if ' = ' in line)
        if values.get('home') != str(Path(python).resolve().parent):
            venv.rename(path / ('.venv-previous-' + uuid.uuid4().hex[:8]))
    if not venv.exists():
        hub.run([python, '-m', 'venv', '--without-pip', venv])
    pip_wheel = next((path / 'runtime/wheels').glob('pip-*.whl'))
    env = dict(os.environ, PYTHONPATH=str(pip_wheel), PYTHONDONTWRITEBYTECODE='1')
    hub.run([venv / 'bin/python', '-m', 'pip', 'install', '--no-index', '--only-binary=:all:', '--require-hashes', '--find-links', path / 'runtime/wheels', '-r', path / 'requirements/production-cp310-linux-amd64.lock'], env=env)
    hub.run([venv / 'bin/python', '-m', 'pip', 'check'])


def pid_on_port(port):
    text = hub.run(['ss', '-lntp', 'sport', '=', ':' + str(port)], capture=True)
    values = set(re.findall(r'pid=(\d+)', text))
    if len(values) != 1:
        raise RuntimeError('Expected exactly one listener on port ' + str(port))
    return int(values.pop())


def process_record(pid):
    root = Path('/proc') / str(pid)
    if root.stat().st_uid != os.getuid():
        raise RuntimeError('Refusing to manage another user process')
    # Only application settings and ordinary executable lookup settings are retained.
    values = {}
    for item in (root / 'environ').read_bytes().split(b'\0'):
        if b'=' not in item:
            continue
        key, value = item.decode().split('=', 1)
        if (key.startswith(('DEVELOP_HUB_', 'NEXT_PUBLIC_')) or key in {'PATH', 'HOME', 'LANG', 'PYTHONPATH'}) and not any(word in key.upper() for word in ['TOKEN', 'SECRET', 'PASSWORD']):
            values[key] = value
    return {'pid': pid, 'start_ticks': (root / 'stat').read_text().split(') ', 1)[1].split()[19], 'argv': [item.decode() for item in (root / 'cmdline').read_bytes().split(b'\0') if item], 'cwd': str((root / 'cwd').resolve()), 'env': values}


def unchanged(record):
    try:
        current = process_record(record['pid'])
        return current['start_ticks'] == record['start_ticks'] and current['argv'] == record['argv'] and current['cwd'] == record['cwd']
    except (OSError, ProcessLookupError):
        return False


def stop(record):
    if not unchanged(record):
        raise RuntimeError('Process identity changed; refusing to signal it')
    os.kill(record['pid'], signal.SIGTERM)
    for _ in range(100):
        if not Path('/proc', str(record['pid'])).exists():
            return
        try:
            if Path('/proc', str(record['pid']), 'stat').read_text().split(') ', 1)[1].startswith('Z'):
                return
        except FileNotFoundError:
            return
        time.sleep(0.25)
    raise RuntimeError('Process did not stop gracefully; no forced kill was sent')


def launch(record, log):
    with log.open('ab') as output:
        process = subprocess.Popen(record['argv'], cwd=record['cwd'], env=record['env'], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(0.2)
    return process_record(process.pid)


def native_records(package_path, manifest, old, api_port, web_port):
    env = dict(old['api']['env'])
    env.update(DEVELOP_HUB_ROOT=str(package_path), PYTHONPATH=str(package_path / 'server/src'), DEVELOP_HUB_AUTO_CREATE_SCHEMA='false', DEVELOP_HUB_LEGACY_ENABLED='false', DEVELOP_HUB_ENV='production', DEVELOP_HUB_DEVICE_MODE='enabled', DEVELOP_HUB_RELEASE=manifest['commit'])
    web_env = dict(old['web']['env'])
    web_env['PATH'] = str(package_path / 'runtime/node/bin') + ':' + web_env.get('PATH', '/usr/bin:/bin')
    return {
        'api': {'argv': [str(package_path / '.venv/bin/python'), '-m', 'uvicorn', 'develop_hub.main:app', '--host', '0.0.0.0', '--port', str(api_port)], 'cwd': str(package_path), 'env': env},
        'web': {'argv': [str(package_path / 'runtime/node/bin/node'), str(package_path / 'web/node_modules/.bin/vinext'), 'start', '--host', '0.0.0.0', '--port', str(web_port)], 'cwd': str(package_path / 'web'), 'env': web_env},
    }


def healthy(api_port, web_port, commit=None):
    for _ in range(80):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{api_port}/api/health', timeout=3) as response:
                health = json.load(response)
            with urllib.request.urlopen(f'http://127.0.0.1:{web_port}/', timeout=3) as response:
                assert response.status == 200
            if commit is not None and health.get('release') != commit:
                raise ValueError('Unexpected deployed commit')
            return health
        except (OSError, ValueError, AssertionError):
            time.sleep(0.25)
    raise RuntimeError('Deployed API/web health check failed')


def create_plan(package_path, control, stable_root, api_port, web_port):
    manifest = verify_package(package_path)
    old = {'api': process_record(pid_on_port(api_port)), 'web': process_record(pid_on_port(web_port))}
    for kind in ['api', 'web']:
        cwd = Path(old[kind]['cwd'])
        if not (cwd == stable_root or cwd.is_relative_to(stable_root) or cwd.is_relative_to(package_path.parent)):
            raise RuntimeError('Listener is outside the explicitly selected stable/package directories')
    if 'develop_hub.main' not in ' '.join(old['api']['argv']) or 'vinext' not in ' '.join(old['web']['argv']):
        raise RuntimeError('Listener identity is not the expected platform')
    database_url = old['api']['env'].get('DEVELOP_HUB_DATABASE_URL', '')
    if not database_url.startswith('sqlite:///'):
        raise RuntimeError('This release procedure supports the current SQLite platform only')
    database = Path(database_url[len('sqlite:///'):]).resolve()
    if not database.is_relative_to(stable_root / 'var'):
        raise RuntimeError('Stable database is outside its registered data directory')
    hub.require_idle(database)
    if hub.db_version(database) != manifest['database_schema']:
        raise RuntimeError('Database migration required; stop and arrange a separately reviewed migration/rollback procedure')
    identifier = str(int(time.time())) + '-' + manifest['commit'][:12]
    plan = {'schema': 1, 'id': identifier, 'created_at': time.time(), 'package': str(package_path), 'package_hash': hub.sha(package_path / 'package.json'), 'commit': manifest['commit'], 'stable_root': str(stable_root), 'database': str(database), 'database_schema': manifest['database_schema'], 'old': old, 'api_port': api_port, 'web_port': web_port}
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = control / (identifier + '.plan.json')
    hub.write_json(path, plan)
    path.chmod(0o600)
    print('Read-only release plan:', path, 'commit:', manifest['commit'])
    return plan


def gate(args, rollback=False):
    approved = args.approve_stable_rollback if rollback else args.approve_stable_update
    if not approved or not args.maintenance_confirmed:
        raise ValueError('Explicit stable approval AND --maintenance-confirmed are required; no stable process was changed')


def apply(plan_path, control, python):
    plan = json.loads(plan_path.read_text())
    if time.time() - plan['created_at'] > 3600:
        raise RuntimeError('Plan expired; create a fresh read-only plan')
    path = Path(plan['package'])
    if hub.sha(path / 'package.json') != plan['package_hash']:
        raise RuntimeError('Package changed after planning')
    manifest = verify_package(path)
    if not all(unchanged(value) for value in plan['old'].values()):
        raise RuntimeError('Stable process changed after planning; create a fresh plan')
    database = Path(plan['database'])
    hub.require_idle(database)
    if hub.db_version(database) != manifest['database_schema']:
        raise RuntimeError('Stable schema changed after planning')
    setup_native(path, python)
    backup = control / (plan['id'] + '.db')
    hub.backup_db(database, backup)
    record_path = control / (plan['id'] + '.deployment.json')
    record = dict(plan, backup=str(backup), status='starting', new={})
    hub.write_json(record_path, record)
    record_path.chmod(0o600)
    desired = native_records(path, manifest, plan['old'], plan['api_port'], plan['web_port'])
    stopped = []
    try:
        for kind in ['web', 'api']:
            stop(plan['old'][kind])
            stopped.append(kind)
        for kind in ['api', 'web']:
            record['new'][kind] = launch(desired[kind], control / (plan['id'] + '-' + kind + '.log'))
        healthy(plan['api_port'], plan['web_port'], manifest['commit'])
        record['status'] = 'healthy'
    except Exception:
        for value in record['new'].values():
            if unchanged(value):
                stop(value)
        # Schema is unchanged; retain current business data on code rollback.
        for kind in ['api', 'web']:
            if kind in stopped:
                record.setdefault('recovered', {})[kind] = launch(plan['old'][kind], control / (plan['id'] + '-recovered-' + kind + '.log'))
        healthy(plan['api_port'], plan['web_port'])
        record['status'] = 'rolled_back_after_start_failure'
        hub.write_json(record_path, record)
        raise
    hub.write_json(record_path, record)
    print('Stable deployment healthy. Record:', record_path, 'commit:', manifest['commit'])


def rollback(record_path, control, restore_database=False):
    record = json.loads(record_path.read_text())
    if record['status'] != 'healthy' or not all(unchanged(value) for value in record['new'].values()):
        raise RuntimeError('Deployment identity/status changed; investigate before rollback')
    database = Path(record['database'])
    if not restore_database:
        hub.require_idle(database)
        if hub.db_version(database) != record['database_schema']:
            raise RuntimeError('Cannot roll code back across schema changes')
    for kind in ['web', 'api']:
        stop(record['new'][kind])
    if restore_database:
        failed = control / (record['id'] + '-data-before-restore')
        failed.mkdir(mode=0o700)
        for suffix in ['', '-wal', '-shm']:
            file = Path(str(database) + suffix)
            if file.exists():
                shutil.copy2(file, failed / file.name)
        hub.backup_db(Path(record['backup']), database)
    recovered = {}
    for kind in ['api', 'web']:
        recovered[kind] = launch(record['old'][kind], control / (record['id'] + '-rollback-' + kind + '.log'))
    healthy(record['api_port'], record['web_port'])
    record.update(status='manually_rolled_back', recovered=recovered, database_restored=restore_database)
    hub.write_json(record_path, record)
    print('Previous code restored; database restored:', restore_database)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['package', 'verify', 'plan', 'apply', 'rollback'])
    parser.add_argument('--root', default=str(Path.home() / 'develop-hub-dev'))
    parser.add_argument('--release')
    parser.add_argument('--package', type=Path)
    parser.add_argument('--control-root', type=Path, default=Path.home() / 'develop-hub-release-control')
    parser.add_argument('--stable-root', type=Path, default=Path('/home/nwj/develop-hub-redesign'))
    parser.add_argument('--api-port', type=int, default=8185)
    parser.add_argument('--web-port', type=int, default=3015)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--record', type=Path)
    parser.add_argument('--python', default='/usr/bin/python3')
    parser.add_argument('--approve-stable-update', action='store_true')
    parser.add_argument('--approve-stable-rollback', action='store_true')
    parser.add_argument('--maintenance-confirmed', action='store_true')
    parser.add_argument('--restore-database', action='store_true')
    parser.add_argument('--accept-data-loss', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.action == 'package':
        with hub.locked(hub.validate_root(args.root)):
            package(hub.validate_root(args.root), args.release)
    elif args.action == 'verify':
        verify_package(args.package)
        print('Package checksums verified')
    elif args.action == 'plan':
        create_plan(args.package.absolute(), args.control_root.absolute(), args.stable_root.absolute(), args.api_port, args.web_port)
    else:
        gate(args, rollback=args.action == 'rollback')
        if args.restore_database and not args.accept_data_loss:
            raise ValueError('Database restore requires explicit --accept-data-loss')
        if args.action == 'apply' and args.plan is None or args.action == 'rollback' and args.record is None:
            raise ValueError('Select an explicit --plan or --record')
        with hub.locked(hub.validate_root(args.control_root)):
            if args.action == 'apply':
                apply(args.plan, args.control_root, args.python)
            else:
                rollback(args.record, args.control_root, args.restore_database)


if __name__ == '__main__':
    main()
