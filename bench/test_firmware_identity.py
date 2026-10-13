import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from firmware_identity import matches_firmware_identity, normalize_identity, source_identity, validate_identity
import worker
from test_runtime import Case

VERSION = 'factory-V2.0.7-1-gabcdef0'
FULL = 'RK struct_light Multi UVC APP version: ' + VERSION + ' (compile 17:51:23 Sep 28 2026)'


@pytest.mark.parametrize('identity', [
    None, {}, {'kind': 'commit', 'value': ''}, {'kind': 'commit', 'value': 'abcdef'},
    {'kind': 'commit', 'value': 'gabcdef0'}, {'kind': 'commit', 'value': 'a' * 41},
    {'kind': 'version', 'value': ''}, {'kind': 'version', 'value': ' _b '},
    {'kind': 'version', 'value': 'first\nsecond'}, {'kind': 'version', 'value': 'x' * 1025},
    {'kind': 'version', 'value': None}, {'kind': 'unknown', 'value': VERSION},
    {'kind': 'commit', 'value': 'abcdef0', 'extra': True},
])
def test_reject_invalid_identity(identity):
    with pytest.raises(ValueError):
        normalize_identity(identity)


def test_normalization_and_legacy_source():
    assert validate_identity is normalize_identity
    assert source_identity({'commit': ' ABCDEF012345 '}) == {'kind': 'commit', 'value': 'abcdef012345'}
    assert normalize_identity({'kind': 'version', 'value': FULL + ' _b\n'}) == {'kind': 'version', 'value': FULL}
    assert normalize_identity({'kind': 'version', 'value': VERSION + ' a'})['value'] == VERSION
    with pytest.raises(ValueError):
        source_identity({'identity': None, 'commit': 'abcdef0'})
    with pytest.raises(ValueError):
        source_identity({'commit': ''})


@pytest.mark.parametrize('observed', [FULL + ' _b', 'firmware abcdef0', 'FW_abcdef0 b', 'v1-gabcdef0123456789 _a'])
def test_legacy_commit_token_matches(observed):
    assert matches_firmware_identity({'kind': 'commit', 'value': 'abcdef0123456789'}, observed)


@pytest.mark.parametrize('observed', ['firmware badbad0', 'prefixabcdef0suffix', 'aabcdef0', 'abcdef0z', '', None])
def test_commit_does_not_match_non_token(observed):
    assert not matches_firmware_identity({'kind': 'commit', 'value': 'abcdef0'}, observed)


@pytest.mark.parametrize('suffix', [' _a', ' _b', ' a', ' b', ''])
def test_full_version_and_token_ignore_only_slot(suffix):
    assert matches_firmware_identity({'kind': 'version', 'value': FULL + ' _a'}, FULL + suffix)
    assert matches_firmware_identity({'kind': 'version', 'value': VERSION}, FULL + suffix)


def test_version_keeps_compile_time_for_full_output():
    changed = FULL.replace('17:51:23', '18:51:23') + ' _b'
    assert not matches_firmware_identity({'kind': 'version', 'value': FULL}, changed)
    assert matches_firmware_identity({'kind': 'version', 'value': VERSION}, changed)


@pytest.mark.parametrize('expected', ['factory-V2.0.7', 'abcdef0', 'V2.0.7-1', VERSION + '-extra', FULL.replace('factory-', '')])
def test_version_rejects_substrings_and_wrong_versions(expected):
    assert not matches_firmware_identity({'kind': 'version', 'value': expected}, FULL + ' _b')


@pytest.mark.parametrize('expected,outcome', [(VERSION, 'passed'), ('factory-V2.0.7', 'failed'), ('', 'error')])
def test_basic_version_uses_shared_identity(tmp_path, expected, outcome):
    tool = tmp_path / 'tool'
    tool.write_bytes(b'fixture')
    data = {'tool': str(tool), 'tool_sha256': worker.digest(tool), 'video': '/dev/fixture',
            'commit': 'badbad0', 'firmware_identity': {'kind': 'version', 'value': expected}}
    case = Case('BASIC-VERSION', data, tmp_path / 'result')
    calls = []
    def call(*args):
        calls.append(args)
        (case.out / 'protocol.jsonl').write_text('{"fixture":true}\n')
        return FULL + ' _b'
    case.call = call
    assert case.run() == (0 if outcome == 'passed' else 1)
    assert case.result['outcome'] == outcome
    if not expected:
        assert calls == []


def boot_fixture(tmp_path, monkeypatch, *, observed=FULL + ' _b', cpu='locked-cpu', with_case=False):
    root = tmp_path / 'package'
    root.mkdir()
    tool = root / 'tool'
    tool.write_bytes(b'fixture')
    package = {'binary_path': 'tool', 'binary_sha256': worker.digest(tool)}
    (root / 'tools.lock.json').write_text(json.dumps(package))
    for filename in ('info-codes.json', 'factory-codes.json'):
        (root / filename).write_text('[]')
    video_root = tmp_path / 'video4linux'
    node = video_root / 'video71'
    node.mkdir(parents=True)
    (node / 'name').write_text('RMSL321_RGB')
    (node / 'index').write_text('0')
    usb = tmp_path / 'usb1' / '1-2'
    (usb / 'child').mkdir(parents=True)
    (usb / 'idVendor').write_text('2207')
    (node / 'device').symlink_to(usb / 'child')
    monkeypatch.setattr(worker, '__file__', str(root / 'worker.py'))
    monkeypatch.setattr(worker, 'Path', lambda value: video_root if str(value) == '/sys/class/video4linux' else Path(value))
    ticks = iter([0, 0, 181])
    monkeypatch.setattr(worker.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(worker.time, 'sleep', lambda _: None)
    job_path = tmp_path / 'job'
    job_path.mkdir()
    job = worker.Job(job_path)
    job.result.update(target_slot='b', firmware_sha256='fixture-sha')
    job.save = lambda phase=None, **values: job.result.update(values, **({'phase': phase} if phase else {}))
    job.usb_location = lambda value: 'locked-port'
    config = {'source': {'identity': {'kind': 'version', 'value': VERSION}},
              'flash': {'target_chip': 'RV1126B', 'device_serial': 'locked-cpu'}, 'test_cases': [], 'round_id': 'TEST-fixture'}
    if with_case:
        entry = root / 'test-cases' / 'BASIC-VERSION' / '1.0.0'
        entry.mkdir(parents=True)
        for path in (root / 'test_runtime.py', entry / 'run.py', entry / 'case.json'):
            path.write_text('{}')
        adapter = {'id': 'BASIC-VERSION', 'tool_sha256': worker.digest(tool),
                   'runtime_sha256': worker.digest(root / 'test_runtime.py'),
                   'entrypoint_sha256': worker.digest(entry / 'run.py'),
                   'definition_sha256': worker.digest(entry / 'case.json')}
        config['test_cases'] = [{'id': 'case-id', 'name': 'Version', 'adapter': adapter}]
        job.result['cases'] = [{}]
        monkeypatch.setattr(worker, 'validate_adapter_registration', lambda *args: None)
    calls, inputs = [], []
    def run(args, name, timeout):
        calls.append(name)
        if name == 'boot-version.log':
            return 0, observed
        if name == 'boot-cpu.log':
            return 0, cpu
        if name == 'BASIC-VERSION.log':
            path = Path(args[args.index('--input') + 1])
            inputs.append(json.loads(path.read_text()))
            (path.parent / 'result.json').write_text(json.dumps({'state': 'finished', 'outcome': 'passed', 'assertions': [{'passed': True}]}))
            return 0, ''
        raise AssertionError(name)
    job.run = run
    return job, config, calls, inputs


def test_boot_passes_version_identity_to_case_input(tmp_path, monkeypatch):
    job, config, calls, inputs = boot_fixture(tmp_path, monkeypatch, with_case=True)
    job.run_tests(config, 'locked-port')
    assert job.result['boot_verified'] is True
    assert job.result['outcome'] == 'passed'
    assert calls == ['boot-version.log', 'boot-cpu.log', 'BASIC-VERSION.log']
    assert inputs[0]['firmware_identity'] == config['source']['identity']
    assert inputs[0]['commit'] == ''


@pytest.mark.parametrize('observed,cpu', [(FULL + ' _a', 'locked-cpu'), (FULL + ' _b', 'wrong-cpu'), ('wrong-version _b', 'locked-cpu')])
def test_version_identity_does_not_bypass_slot_cpu_or_boot_version(tmp_path, monkeypatch, observed, cpu):
    job, config, calls, inputs = boot_fixture(tmp_path, monkeypatch, observed=observed, cpu=cpu)
    with pytest.raises(RuntimeError, match='Boot identity/version not verified'):
        job.run_tests(config, 'locked-port')
    assert not job.result.get('boot_verified')
    assert inputs == []


def test_invalid_boot_identity_rejects_before_any_device_call(tmp_path, monkeypatch):
    job, config, calls, inputs = boot_fixture(tmp_path, monkeypatch)
    config['source'] = {'commit': ''}
    with pytest.raises(ValueError):
        job.run_tests(config, 'locked-port')
    assert calls == []
