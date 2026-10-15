#!/usr/bin/env python3
"""Rebuild and deploy the isolated development platform from a Git commit."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FORBIDDEN_PORTS = {3000, 3015, 8175, 8185}
FORBIDDEN_ROOTS = [Path('/home/nwj/develop-hub-redesign'), Path('/home/nwj/develop-hub'), Path('/home/nwj/develop-hub-stable-protection'), Path('/home/nwj/.nvm')]


def run(argv, *, capture=False, env=None, cwd=None):
    result = subprocess.run([str(item) for item in argv], cwd=cwd, env=env, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ''


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def validate_root(value):
    raw = Path(value).expanduser().absolute()
    resolved = raw.resolve()
    if resolved != raw or resolved in [Path('/'), Path.home()] or any(resolved == item or resolved.is_relative_to(item) for item in FORBIDDEN_ROOTS):
        raise ValueError('Refusing symlink, home, filesystem root or protected stable path: ' + str(raw))
    return resolved


def validate_config(config):
    if any(type(config[key]) is not int or config[key] in FORBIDDEN_PORTS or not 1024 <= config[key] <= 65535 for key in ['web_port', 'api_port']):
        raise ValueError('Development ports must be non-reserved high ports; 3000/3015/8175/8185 are protected')
    if config['web_port'] == config['api_port']:
        raise ValueError('Web and API ports must differ')
    if not re.fullmatch(r'(?:dhdev-[a-z0-9-]+|develop-hub-dev)', config['name']):
        raise ValueError('Invalid development container name')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', config['public_host']):
        raise ValueError('Use a hostname or IPv4 address without a URL or shell syntax')


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def config_for(root, args):
    path = root / 'deployment.json'
    if path.exists():
        config = json.loads(path.read_text())
    else:
        name = args.name or ('develop-hub-dev' if root == Path('/home/nwj/develop-hub-dev') else 'dhdev-' + hashlib.sha256(str(root).encode()).hexdigest()[:12])
        config = {'schema': 1, 'name': name, 'public_host': args.public_host, 'web_port': args.web_port, 'api_port': args.api_port}
    validate_config(config)
    return config


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'operation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def common():
    return ['docker', 'run', '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--cpus=2', '--memory=4g', '--memory-swap=4g', '--pids-limit=256', '--log-opt=max-size=10m', '--log-opt=max-file=3']


def export_source(repo, ref, destination):
    commit = run(['git', '-C', repo, 'rev-parse', '--verify', ref + '^{commit}'], capture=True)
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Expected a full Git commit')
    destination.mkdir(parents=True)
    process = subprocess.Popen(['git', '-C', str(repo), 'archive', '--format=tar', commit], stdout=subprocess.PIPE)
    with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
        for member in archive:
            target = destination / member.name
            if not target.resolve().is_relative_to(destination.resolve()) or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe source archive entry: ' + member.name)
            archive.extract(member, destination)
    if process.wait():
        raise RuntimeError('Git source export failed')
    return commit


def image_for(source, log):
    inputs = ['deploy/isolated-development/Dockerfile', 'deploy/isolated-development/toolchain.lock.json', 'deploy/isolated-development/fetch-node.py', 'deploy/isolated-development/install-os.py', 'deploy/isolated-development/third_party/repo', 'requirements/development-linux-amd64.lock', 'requirements/production-cp310-linux-amd64.lock', 'web/package.json', 'web/package-lock.json']
    key = hashlib.sha256(''.join(sha(source / name) for name in inputs).encode()).hexdigest()
    image = 'develop-hub-toolchain:' + key[:20]
    exists = subprocess.run(['docker', 'image', 'inspect', image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not exists:
        env = dict(os.environ, DOCKER_BUILDKIT='0')
        with log.open('w') as output:
            subprocess.run(['docker', 'build', '--platform=linux/amd64', '--memory=4g', '--cpu-period=100000', '--cpu-quota=200000', '-t', image, '-f', str(source / 'deploy/isolated-development/Dockerfile'), str(source)], env=env, stdout=output, stderr=subprocess.STDOUT, check=True)
    return image, key


def environment(config, *, profile='development', marker=''):
    return {
        'DEVELOP_HUB_ROOT': '/app', 'DEVELOP_HUB_ENV': 'isolated-development',
        'DEVELOP_HUB_DEVICE_MODE': 'simulation-only',
        'DEVELOP_HUB_DATABASE_URL': 'sqlite:////var/dev/testing.db',
        'DEVELOP_HUB_ARTIFACT_ROOT': '/var/dev/artifacts',
        'DEVELOP_HUB_WORKTREE_ROOT': '/var/dev/worktrees',
        'DEVELOP_HUB_QA_PLUGIN_ROOT': '/var/dev/qa-plugins',
        'DEVELOP_HUB_AUTO_CREATE_SCHEMA': 'false', 'DEVELOP_HUB_LEGACY_ENABLED': 'false',
        'DEVELOP_HUB_TEST_STATIONS': '/var/dev/test-stations.json',
        'DEVELOP_HUB_CORS_ORIGINS': f"http://{config['public_host']}:{config['web_port']},http://127.0.0.1:{config['web_port']}",
        'NEXT_PUBLIC_DEVELOP_HUB_API_URL': f"http://{config['public_host']}:{config['api_port']}",
        'NEXT_PUBLIC_DEVELOP_HUB_ENV': profile,
        'DEVELOP_HUB_RELEASE': marker,
        'PYTHONPATH': '/app/server/src', 'PYTHONDONTWRITEBYTECODE': '1', 'HOME': '/tmp',
    }


def env_args(values):
    result = []
    for key, value in values.items():
        result += ['-e', key + '=' + value]
    return result


def tool(source, data, image, values, command, log):
    argv = common() + ['--rm', '--network=none', '--mount', f'type=bind,src={source},dst=/app', '--mount', f'type=bind,src={data},dst=/var/dev'] + env_args(values) + [image, 'sh', '-c', command]
    with log.open('w') as output:
        subprocess.run(argv, check=True, stdout=output, stderr=subprocess.STDOUT)


def backup_db(source, target):
    if not source.exists():
        return
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst, pages=128)
        assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def db_version(path):
    if not path.exists():
        return []
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        if not db.execute("SELECT count(*) FROM sqlite_master WHERE name='alembic_version'").fetchone()[0]:
            return []
        return sorted(row[0] for row in db.execute('SELECT version_num FROM alembic_version'))


def require_idle(path):
    if not path.exists():
        return
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        if db.execute("SELECT count(*) FROM sqlite_master WHERE name='testing_runs'").fetchone()[0]:
            active = db.execute("SELECT count(*) FROM testing_runs WHERE state IN ('preparing','running','queued','awaiting_operator')").fetchone()[0]
            if active:
                raise RuntimeError('Finish/cancel active development runs before activation or rollback')


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(str(self.path))


def get_socket(path, route):
    connection = UnixHTTP(path)
    try:
        connection.request('GET', route)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f'HTTP {response.status} for {route}')
        return body
    finally:
        connection.close()


def container_exists(name):
    return subprocess.run(['docker', 'inspect', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def remove(name):
    if container_exists(name):
        run(['docker', 'rm', '-f', name])


def start_pair(source, data, image, values, prefix):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry = data / 'test-stations.json'
    if not registry.exists():
        write_json(registry, {'schema': 'rk-test-stations/1', 'stations': {}})
    names = [prefix + '-api', prefix + '-web']
    for kind, name in zip(['api', 'web'], names):
        argv = common() + ['-d', '--name', name, '--restart=unless-stopped', '--network=none', '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777', '--mount', f'type=bind,src={source},dst=/app,readonly', '--mount', f'type=bind,src={data},dst=/var/dev'] + env_args(values) + [image, 'sh', f'/app/deploy/isolated-development/{kind}-entry.sh']
        run(argv)
    for _ in range(120):
        try:
            health = json.loads(get_socket(data / 'api.sock', '/api/health'))
            html = get_socket(data / 'web.sock', '/').decode()
            assert health['device_mode'] == 'simulation-only' and '<html' in html.lower()
            for kind in ['api', 'web']:
                (data / (kind + '.sock')).chmod(0o600)
            return health
        except (OSError, ValueError, AssertionError, http.client.HTTPException):
            time.sleep(0.25)
    raise RuntimeError('Candidate did not become healthy; inspect its container logs')


def build_release(root, config, args):
    release_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    release = root / 'releases' / release_id
    source = release / 'source'
    commit = export_source(REPO, args.ref, source)
    image, lock_key = image_for(source, release / 'image-build.log')
    # Dependencies come exclusively from the immutable toolchain image.
    (source / 'web/node_modules').symlink_to('/opt/web/node_modules')
    (source / '.venv').symlink_to('/opt/venv')
    data = release / 'probe-data'
    data.mkdir(mode=0o700)
    backup_db(root / 'var/testing.db', data / 'testing.db')
    values = environment(config, profile=args.profile, marker=commit)
    if args.profile == 'production':
        url = urlsplit(args.production_api_url or '')
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
            raise ValueError('A credential-free --production-api-url is required')
        values['NEXT_PUBLIC_DEVELOP_HUB_API_URL'] = args.production_api_url.rstrip('/')
    tool(source, data, image, values, 'cd /app/web && npm run typecheck && npm run lint && npm run build', release / 'frontend.log')
    tool(source, data, image, values, 'DEVELOP_HUB_ENV=test DEVELOP_HUB_DEVICE_MODE=enabled python -m pytest server/tests', release / 'backend.log')
    prefix = config['name'] + '-probe-' + release_id.lower()
    try:
        health = start_pair(source, data, image, values, prefix)
        target_schema = db_version(data / 'testing.db')
    finally:
        for kind in ['api', 'web']:
            remove(prefix + '-' + kind)
    manifest = {
        'schema': 1, 'id': release_id, 'commit': commit, 'profile': args.profile,
        'image': image, 'image_id': run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image], capture=True),
        'dependency_key': lock_key, 'database_schema': target_schema,
        'tests_passed': True, 'frontend_passed': True, 'probe_health': health,
        'api_url': values['NEXT_PUBLIC_DEVELOP_HUB_API_URL'],
        'source': str(source), 'created_at': datetime.now(timezone.utc).isoformat(),
    }
    write_json(release / 'manifest.json', manifest)
    print('Verified release:', release_id, commit, flush=True)
    return release, manifest


def load_release(root, release_id):
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}', release_id):
        raise ValueError('Invalid release ID')
    release = root / 'releases' / release_id
    manifest = json.loads((release / 'manifest.json').read_text())
    if not manifest['tests_passed'] or not manifest['frontend_passed'] or manifest['profile'] != 'development':
        raise ValueError('Only verified development releases may be activated here')
    if Path(manifest['source']).resolve() != release / 'source':
        raise ValueError('Release source escaped its deployment root')
    return release, manifest


def activate(root, config, release, manifest):
    if manifest['profile'] != 'development':
        raise ValueError('Production candidates use the explicit stable release procedure')
    data = root / 'var'
    data.mkdir(exist_ok=True, mode=0o700)
    require_idle(data / 'testing.db')
    current_schema = db_version(data / 'testing.db')
    if current_schema and current_schema != manifest['database_schema']:
        raise RuntimeError('Schema change detected: automatic activation/rollback is blocked; arrange a data migration')
    previous = json.loads((root / 'active.json').read_text()) if (root / 'active.json').exists() else None
    backups = root / 'backups'
    backups.mkdir(exist_ok=True, mode=0o700)
    backup_db(data / 'testing.db', backups / (manifest['id'] + '.db'))
    retained = []
    for kind in ['api', 'web']:
        name = config['name'] + '-' + kind
        if container_exists(name):
            old = name + '-retained-' + manifest['id'].lower()
            run(['docker', 'stop', '--time', '25', name])
            run(['docker', 'rename', name, old])
            retained.append((name, old))
    try:
        start_pair(release / 'source', data, manifest['image'], environment(config, marker=manifest['commit']), config['name'])
    except Exception:
        for kind in ['api', 'web']:
            remove(config['name'] + '-' + kind)
        for name, old in retained:
            run(['docker', 'rename', old, name])
            run(['docker', 'start', name])
        raise
    if previous:
        write_json(root / 'previous.json', previous)
    write_json(root / 'active.json', manifest)
    temporary = root / 'current.tmp'
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(release / 'source')
    temporary.replace(root / 'current')
    write_json(root / 'deployment.json', config)
    print('Activated development commit', manifest['commit'], flush=True)


def install_gateways(root, config, source):
    units = Path.home() / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    services = []
    for kind in ['api', 'web']:
        name = config['name'] + '-' + kind + '-proxy.service'
        services.append(name)
        # Paths with whitespace are rejected until explicit systemd escaping is implemented.
        if any(char.isspace() for char in str(root) + str(source)):
            raise ValueError('Use deployment/source paths without whitespace')
        (units / name).write_text(f'''[Unit]
Description=Isolated Develop Hub {kind} gateway
After=network.target
[Service]
ExecStart=/usr/bin/python3 {source}/deploy/isolated-development/proxy.py {kind} --root {root} --port {config[kind + '_port']}
WorkingDirectory={root}
Restart=on-failure
RestartSec=3
UMask=0077
CPUQuota=50%
MemoryMax=256M
TasksMax=64
NoNewPrivileges=true
[Install]
WantedBy=default.target
''')
    env = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}', DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
    run(['systemctl', '--user', 'daemon-reload'], env=env)
    run(['systemctl', '--user', 'enable', *services], env=env)
    run(['systemctl', '--user', 'restart', *services], env=env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['bootstrap', 'deploy', 'prepare', 'activate', 'rollback', 'status', 'stop'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--ref', default='HEAD', help='Committed source revision; working-tree edits are never silently deployed')
    parser.add_argument('--profile', choices=['development', 'production'], default='development')
    parser.add_argument('--production-api-url')
    parser.add_argument('--release')
    parser.add_argument('--public-host', default='127.0.0.1')
    parser.add_argument('--web-port', type=int, default=3025)
    parser.add_argument('--api-port', type=int, default=8195)
    parser.add_argument('--name')
    parser.add_argument('--no-gateways', action='store_true', help='For CI/isolated verification without a user systemd session')
    args = parser.parse_args(argv)
    if args.action in ['bootstrap', 'deploy', 'prepare'] and args.ref == 'HEAD' and run(['git', '-C', REPO, 'status', '--porcelain'], capture=True):
        raise ValueError('Commit or stash working-tree changes first, or explicitly select a different committed --ref')
    root = validate_root(args.root)
    config = config_for(root, args)
    with locked(root):
        if args.action in ['bootstrap', 'deploy', 'prepare']:
            release, manifest = build_release(root, config, args)
            if args.action != 'prepare':
                activate(root, config, release, manifest)
                if not args.no_gateways:
                    install_gateways(root, config, release / 'source')
        elif args.action in ['activate', 'rollback']:
            release_id = args.release
            if args.action == 'rollback' and not release_id:
                release_id = json.loads((root / 'previous.json').read_text())['id']
            release, manifest = load_release(root, release_id or '')
            activate(root, config, release, manifest)
            if not args.no_gateways:
                install_gateways(root, config, release / 'source')
        elif args.action == 'stop':
            for kind in ['api', 'web']:
                remove(config['name'] + '-' + kind)
        else:
            print((root / 'active.json').read_text() if (root / 'active.json').exists() else 'No managed release activated')


if __name__ == '__main__':
    main()
