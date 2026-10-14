"""Only manages named development containers; stable services are out of scope."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import socket
import time

root = Path('/home/nwj/develop-hub-dev')
assert root.resolve() == root
parser = argparse.ArgumentParser()
parser.add_argument('action', choices=['start', 'stop', 'restart', 'build', 'test', 'status', 'shell'])
args = parser.parse_args()
image = 'develop-hub-dev-tools:20261014'
names = ['develop-hub-dev-api', 'develop-hub-dev-web']

def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)

def stop():
    for name in names:
        result = subprocess.run(['docker', 'inspect', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            run(['docker', 'rm', '-f', name])

def common():
    return ['docker', 'run', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user', str(os.getuid()) + ':' + str(os.getgid()), '--cpus=2', '--memory=4g', '--memory-swap=4g', '--pids-limit=256', '--log-opt=max-size=10m', '--log-opt=max-file=3']

def tooling(command):
    assert (root / 'app').resolve() == root / 'app'
    run(common() + ['--rm', '--network=none', '--mount', f'type=bind,src={root}/app,dst=/app', '--mount', f'type=bind,src={root}/var,dst=/var/dev', '--env-file', str(root / 'ops/development.env'), image, 'sh', '-c', command])

def start():
    for kind in ['api', 'web']:
        run(common() + ['-d', '--name', 'develop-hub-dev-' + kind, '--restart=unless-stopped', '--network=none', '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777', '--mount', f'type=bind,src={root}/app,dst=/app,readonly', '--mount', f'type=bind,src={root}/var,dst=/var/dev', '--mount', f'type=bind,src={root}/ops,dst=/isolation,readonly', '--env-file', str(root / 'ops/development.env'), image, 'sh', '/isolation/' + kind + '-entry.sh'])

    for kind in ['api', 'web']:
        path = root / 'var' / (kind + '.sock')
        for attempt in range(120):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.connect(str(path))
                path.chmod(0o600)
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError('Development Unix socket did not become ready: ' + kind)

if args.action == 'start':
    start()
elif args.action == 'stop':
    stop()
elif args.action == 'restart':
    stop()
    start()
elif args.action == 'build':
    tooling('cd /app/web && npm run typecheck && npm run lint && npm run build')
elif args.action == 'test':
    # Mocked unit tests need local subprocesses; container network/devices remain blocked.
    tooling('DEVELOP_HUB_ENV=test DEVELOP_HUB_DEVICE_MODE=enabled /app/.venv/bin/python -m pytest server/tests')
elif args.action == 'shell':
    run(common() + ['--rm', '-it', '--network=none', '--mount', f'type=bind,src={root}/app,dst=/app', '--env-file', str(root / 'ops/development.env'), image, 'sh'])
else:
    for name in names:
        run(['docker', 'inspect', '--format', '{{.Name}} {{.State.Status}} network={{.HostConfig.NetworkMode}} read_only={{.HostConfig.ReadonlyRootfs}}', name])
