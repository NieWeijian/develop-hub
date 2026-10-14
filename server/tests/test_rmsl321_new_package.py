import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

from fastapi.testclient import TestClient
import pytest

from develop_hub.testing.packages import import_case_package, validate_parameters
from test_testing_execution import application, post, wait

ROOT = Path(__file__).resolve().parents[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_complete_source_package_and_manual_contracts(tmp_path, application):
    builder = load(ROOT / 'scripts/build-rmsl321-case-package.py', 'builder')
    archive = tmp_path / 'suite.zip'
    manifest = builder.build(archive)
    assert len(manifest['cases']) == 25
    assert all(c['source_case'] not in builder.RETIRED for c in manifest['cases'])
    imported = import_case_package(archive, tmp_path / 'packages')
    assert len(imported['cases']) == 25
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name.endswith('.py'):
                compile(z.read(name), name, 'exec')
        assert b'checked(' in z.read('switch_runtime.py')
        assert b'tools.lock.json' not in z.read('rv1126b.py')
    for case in manifest['cases']:
        parameters = validate_parameters(case['parameters_schema'], {})
        if case['source_case'] == 'STREAM-RGB-DEPTH':
            assert parameters['duration_seconds'] == 18000
            with pytest.raises(ValueError):
                validate_parameters(case['parameters_schema'], {'duration_seconds': 86400})
    with TestClient(application) as client:
        for case in builder.manual_cases():
            published = post(client, '/case-revisions', case)
            assert published['definition']['recovery']['required']
            assert published['definition']['steps'][-1]['attachments_required']


@pytest.mark.parametrize('outcome,recovery,expected', [('passed', 'passed', 'passed'), ('failed', 'passed', 'failed'), ('blocked', 'passed', 'error'), ('passed', 'failed', 'error')])
def test_bridge_keeps_failures_and_recovery(monkeypatch, outcome, recovery, expected):
    monkeypatch.setitem(sys.modules, 'bridge_support', SimpleNamespace())
    bridge = load(ROOT / 'bench/rmsl321_package_bridge.py', 'bridge_test')
    identity = {'serial': 'abcd', 'usb_path': '/usb/device', 'firmware_version': 'v1'}
    legacy = {'state': 'finished', 'outcome': outcome, 'assertions': [{'name': 'readback', 'expected': 1, 'actual': 1, 'passed': outcome == 'passed'}], 'recovery': {'state': recovery}}
    data = {'run_id': 'r', 'binding_id': 'b', 'case_revision_id': 'c'}
    converted = bridge.normalized_result(data, legacy, identity, identity, True)
    assert converted['verdict'] == expected
    assert bridge.normalized_result(data, legacy, identity, {}, True)['recovery']['state'] == 'failed'


@pytest.mark.parametrize('restored', [True, False])
@pytest.mark.parametrize('decision', ['failed', 'passed'])
def test_manual_failure_skips_risky_steps_and_requires_recovery(application, restored, decision):
    with TestClient(application) as client:
        post(client, '/demo')
        demo = client.get('/api/testing/test-plans').json()[0]
        case = post(client, '/case-revisions', {'case_id': 'manual.recovery', 'version': '1.0.0', 'name': '人工恢复测试', 'mode': 'manual', 'definition': {
            'effects': 'state_change', 'recovery': {'required': True, 'mode': 'operator_confirmation'},
            'steps': [{'id': 'first', 'title': '第一步', 'instruction': '执行并记录', 'pass_criteria': '记录完整', 'required_confirmations': ['confirmed'], 'record_schema': {'type': 'object', 'additionalProperties': False, 'properties': {'confirmed': {'type': 'boolean'}}, 'required': ['confirmed']}}, {'id': 'must-skip', 'title': '后续写入', 'instruction': '不应执行', 'pass_criteria': '不应执行'},
                {'id': 'restore-confirmation', 'title': '恢复确认', 'instruction': '确认恢复', 'pass_criteria': '恢复可用', 'attachments_required': True,
                 'record_schema': {'type': 'object', 'additionalProperties': False, 'properties': {'recovery_confirmed': {'type': 'boolean'}}, 'required': ['recovery_confirmed']}}]}})
        draft = demo['draft']; draft['cases'] = [{'binding_id': 'manual', 'case_revision_id': case['id'], 'required': True, 'parameter_overrides': {}}]
        plan = post(client, '/test-plans', {'name': '人工流程', 'draft': draft})
        run = post(client, '/test-runs', {'plan_id': plan['id'], 'draft_revision': plan['revision'], 'idempotency_key': 'manual-recovery'})
        current = wait(client, run['id'], 'awaiting_operator')
        post(client, '/test-runs/' + run['id'] + '/operator-records', {'step_id': 'manual:first', 'revision': current['operator_revision'], 'operator': 'tester', 'decision': decision, 'values': {'confirmed': False}, 'idempotency_key': 'first'})
        import time
        for _ in range(50):
            current = client.get('/api/testing/test-runs/' + run['id']).json()
            if current['result']['awaiting_operator']['step_id'] == 'manual:restore-confirmation': break
            time.sleep(.05)
        assert current['result']['awaiting_operator']['step_id'] == 'manual:restore-confirmation'
        payload = {'step_id': 'manual:restore-confirmation', 'revision': current['operator_revision'], 'operator': 'tester', 'decision': 'passed' if restored else 'failed', 'values': {'recovery_confirmed': restored}, 'idempotency_key': 'restore'}
        assert client.post('/api/testing/test-runs/' + run['id'] + '/operator-records', json=payload).status_code == 422
        evidence = client.post('/api/testing/test-runs/' + run['id'] + '/evidence?name=restore.txt', content=b'recovery fixture').json()
        payload['attachments'] = [{'evidence_id': evidence['evidence_id']}]
        post(client, '/test-runs/' + run['id'] + '/operator-records', payload)
        final = wait(client, run['id'], 'completed')
        assert final['verdict'] == ('failed' if restored else 'error')
        assert final['recovery'] == ('restored' if restored else 'failed')
        device = client.get('/api/testing/devices/' + draft['device_id']).json()
        assert device['health'] == ('ready' if restored else 'needs_attention')
