import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from develop_hub import test_workbench as workbench
from develop_hub.models import TestRoundStatus as Status


def test_structured_stdout_keeps_complete_unicode_json_and_bounded_log(tmp_path):
    report = {'round_id': 'TEST-000020', 'message': '标定通过' * 5000, 'state': 'finished'}
    payload = json.dumps(report, ensure_ascii=False)
    script = 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'
    result = asyncio.run(workbench.run_command(
        [sys.executable, '-c', script], tmp_path, 10, payload, capture_stdout=True))
    assert result['exit_code'] == 0
    assert json.loads(result['stdout']) == report
    assert len(result['stdout_tail']) == 12000
    assert result['stdout_tail'] == payload[-12000:]
    with pytest.raises(json.JSONDecodeError):
        json.loads(result['stdout_tail'])
    default = asyncio.run(workbench.run_command(
        [sys.executable, '-c', script], tmp_path, 10, payload))
    assert 'stdout' not in default
    assert default['stdout_tail'] == payload[-12000:]


def test_oversized_structured_stdout_fails_explicitly(tmp_path):
    with pytest.raises(workbench.TestWorkbenchError, match='exceeds 8388608 bytes'):
        asyncio.run(workbench.run_command(
            [sys.executable, '-c', "print('x' * (8 * 1024 * 1024))"],
            tmp_path, 10, capture_stdout=True))


@pytest.mark.parametrize('failure', ['json', 'ssh', 'identity', 'revision', None])
def test_monitor_preserves_snapshot_on_error_then_recovers(monkeypatch, tmp_path, failure):
    previous = {'revision': 763, 'phase': 'test_cases', 'state': 'running',
                'received_at': '2026-10-08T03:43:55+00:00',
                'cases': [{'id': 'calibration', 'state': 'running'}]}
    current = SimpleNamespace(
        status=Status.BENCH_RUNNING, display_number=20,
        bench_snapshot={'host': 'fixture', 'port': 22, 'user': 'fixture'},
        remote_path=str(tmp_path), delivery_result={'files': [{'filename': 'test-run.json', 'sha256': 'digest'}]},
        controller_result=previous, product_snapshot={'execute_test_cases': True},
        test_case_snapshot=[{'id': 'calibration', 'code': 'SWITCH-FIXTURE'}, {'id': 'stream', 'code': 'STREAM-RGB-DEPTH'}],
        completed_at=None, last_error=None)
    report = {'round_id': 'TEST-000020', 'config_sha256': 'digest', 'revision': 1000,
              'state': 'finished', 'phase': 'tests_complete', 'outcome': 'error',
              'message': 'stream interrupted', 'log_tail': 'x' * 13000,
              'cases': [{'id': 'calibration', 'code': 'SWITCH-FIXTURE', 'state': 'finished', 'outcome': 'passed'},
                        {'id': 'stream', 'code': 'STREAM-RGB-DEPTH', 'state': 'finished', 'outcome': 'error'}]}
    commits = []
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args): return current
        def commit(self): commits.append(current.status)
    monkeypatch.setattr(workbench, 'SessionLocal', Session)
    workflow = workbench.TestRoundWorkflow(SimpleNamespace())
    events = []
    monkeypatch.setattr(workflow, '_event', lambda *args: events.append(args))
    from develop_hub import evidence_store
    async def archive(*args):
        assert commits[-1] == Status.TEST_FAILED
        assert current.controller_result['sync_status'] == 'ok'
        raise RuntimeError('fixture: evidence copy unavailable')
    monkeypatch.setattr(evidence_store, 'archive', archive)
    attempts = 0
    async def remote(bench, command, timeout=120, *, capture_stdout=False):
        nonlocal attempts
        assert capture_stdout is True
        attempts += 1
        value = dict(report)
        if attempts == 1:
            if failure == 'json': return {'exit_code': 0, 'stdout': '{broken'}
            if failure == 'ssh': return {'exit_code': 255, 'stdout': ''}
            if failure == 'identity': value['config_sha256'] = 'other'
            if failure == 'revision': value['revision'] = 762
        payload = json.dumps(value)
        return {'exit_code': 0, 'stdout': payload, 'stdout_tail': payload[-12000:]}
    monkeypatch.setattr(workflow, '_remote', remote)
    async def pause(seconds):
        if failure and attempts == 1:
            assert current.status == Status.BENCH_RUNNING
            assert current.completed_at is None
            assert current.controller_result['sync_status'] == 'error'
            assert current.controller_result['transport_error']
            assert current.controller_result['checked_at']
            assert current.controller_result['cases'] == previous['cases']
            assert current.controller_result['received_at'] == previous['received_at']
        assert attempts <= 2
    monkeypatch.setattr(workbench.asyncio, 'sleep', pause)
    asyncio.run(workflow._monitor_bench('fixture-round'))
    assert current.status == Status.TEST_FAILED
    assert current.completed_at is not None
    assert current.controller_result['sync_status'] == 'ok'
    assert 'transport_error' not in current.controller_result
    assert 'checked_at' not in current.controller_result
    assert current.controller_result['cases'][0]['outcome'] == 'passed'
    assert current.controller_result['cases'][1]['outcome'] == 'error'
    assert current.controller_result['evidence_archived'] is False
    assert 'evidence copy unavailable' in current.controller_result['evidence_archive_error']
    assert len(events) == 1
