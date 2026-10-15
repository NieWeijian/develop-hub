"""Runs INSIDE a no-network recovery container, never on the live host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

package = Path('/candidate')
baseline = Path('/drill/baseline')
sys.path.insert(0, str(package / 'deploy/isolated-development'))
import hub
import stable_release as release

if Path('/var/run/docker.sock').exists() or Path('/dev/bus/usb').exists():
    raise RuntimeError('Recovery drill must not have host management/hardware mounts')
if any(name != 'lo' for name in os.listdir('/sys/class/net')):
    raise RuntimeError('Recovery drill requires --network=none')

data = baseline / 'var/preview'
control = Path('/drill/control')
control.mkdir(exist_ok=True)
hub.require_idle(data / 'testing.db')

# Refresh only the copied virtualenv launcher for this container's CPython 3.10.
for name in ['python', 'python3', 'python3.10']:
    alias = baseline / '.venv/bin' / name
    if alias.is_symlink():
        alias.unlink()
base_python = str(Path(sys.executable).resolve())
subprocess.run([base_python, '-m', 'venv', '--without-pip', str(baseline / '.venv')], check=True)
env = dict(os.environ, DEVELOP_HUB_ROOT=str(baseline), DEVELOP_HUB_DATABASE_URL='sqlite:///' + str(data / 'testing.db'), DEVELOP_HUB_ARTIFACT_ROOT=str(data / 'artifacts'), DEVELOP_HUB_WORKTREE_ROOT=str(data / 'worktrees'), DEVELOP_HUB_QA_PLUGIN_ROOT=str(data / 'plugins'), DEVELOP_HUB_TEST_STATIONS=str(data / 'test-stations.json'), DEVELOP_HUB_LEGACY_ENABLED='false', DEVELOP_HUB_AUTO_CREATE_SCHEMA='false', DEVELOP_HUB_ENV='recovery-drill', DEVELOP_HUB_DEVICE_MODE='simulation-only', DEVELOP_HUB_RELEASE='recovered-baseline', PYTHONPATH=str(baseline / 'server/src'), HOME='/tmp', PYTHONDONTWRITEBYTECODE='1')
old = {
    'api': {'argv': [str(baseline / '.venv/bin/python'), '-m', 'uvicorn', 'develop_hub.main:app', '--host', '127.0.0.1', '--port', '8185'], 'cwd': str(baseline), 'env': env},
    'web': {'argv': [str(baseline / 'runtime/node/bin/node'), str(baseline / 'web/node_modules/.bin/vinext'), 'start', '--host', '127.0.0.1', '--port', '3015'], 'cwd': str(baseline / 'web'), 'env': dict(env, PATH=str(baseline / 'runtime/node/bin') + ':' + env.get('PATH', ''))},
}
running = {kind: release.launch(old[kind], control / ('baseline-' + kind + '.log')) for kind in ['api', 'web']}
release.healthy(8185, 3015)
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8185/api/testing/test-runs') as response:
    recovered_runs = len(json.load(response))
with urllib.request.urlopen('http://127.0.0.1:8185/api/testing/test-plans') as response:
    recovered_plans = len(json.load(response))

manifest = release.verify_package(package)
plan = release.create_plan(package, control, baseline, 8185, 3015)
plan_path = control / (plan['id'] + '.plan.json')

# Keep device calls forbidden even during the production-package cutover drill.
original_records = release.native_records
def safe_records(*args):
    records = original_records(*args)
    records['api']['env']['DEVELOP_HUB_DEVICE_MODE'] = 'simulation-only'
    return records
release.native_records = safe_records
release.apply(plan_path, control, base_python)
record_path = control / (plan['id'] + '.deployment.json')
record = json.loads(record_path.read_text())
assert record['status'] == 'healthy'
assert release.healthy(8185, 3015, manifest['commit'])['device_mode'] == 'simulation-only'
release.rollback(record_path, control)
record = json.loads(record_path.read_text())
running = record['recovered']
assert record['status'] == 'manually_rolled_back'

# Now exercise a real candidate startup failure and automatic recovery.
plan = release.create_plan(package, control, baseline, 8185, 3015)
plan_path = control / (plan['id'] + '.plan.json')
def bad_records(*args):
    records = safe_records(*args)
    records['web']['argv'] = ['/bin/sh', '-c', 'sleep 1; exit 17']
    return records
release.native_records = bad_records
try:
    release.apply(plan_path, control, base_python)
except (RuntimeError, FileNotFoundError, ProcessLookupError):
    pass
else:
    raise AssertionError('Injected broken candidate unexpectedly succeeded')
failed = json.loads((control / (plan['id'] + '.deployment.json')).read_text())
assert failed['status'] == 'rolled_back_after_start_failure'
release.healthy(8185, 3015)
result = {'restored_baseline_boot': True, 'historical_runs_readable': recovered_runs, 'historical_plans_readable': recovered_plans, 'production_package_boot': True, 'manual_code_rollback': True, 'failed_candidate_auto_rollback': True, 'network': 'none', 'physical_devices': False, 'package_commit': manifest['commit']}
Path('/drill/result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
for value in failed['recovered'].values():
    if release.unchanged(value):
        release.stop(value)
