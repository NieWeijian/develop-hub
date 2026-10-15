from copy import deepcopy
import hashlib
import json

import pytest

from develop_hub.testing.report_analysis import annotate_report, assertion_role, failure_reasons
from develop_hub.testing.reporting import report_html


def test_failed_auxiliary_is_not_hidden_and_summary_is_not_duplicated():
    case = {'verdict': 'error', 'message': 'generic', 'assertions': [
        {'id': 'case-completed', 'passed': False, 'actual': 'generic'},
        {'id': 'device-identity', 'name': '测试前后设备身份一致', 'passed': False,
         'expected': 'A', 'actual': 'B'},
        {'name': 'Tool SHA-256', 'passed': True},
    ]}
    original = deepcopy(case)
    report = {'result': {'cases': [deepcopy(case)]}}
    annotate_report(report)
    result = report['result']['cases'][0]
    assert result['verdict'] == 'error'
    assert len(result['failure_reasons']) == 1
    assert result['failure_reasons'][0]['actual'] == 'B'
    assert result['assertions'][1]['role'] == 'auxiliary'
    assert case == original


def test_node_mismatch_explains_missing_and_extra_not_sensor_damage():
    case = {'verdict': 'failed', 'assertions': [{
        'name': 'vicap nodes and required fields', 'passed': False,
        'expected': ['rkcif-mipi-lvds', 'rkcif-mipi-lvds1', 'rkcif-mipi-lvds2'],
        'actual': "Missing, duplicate or unexpected proc nodes: ['rkcif-mipi-lvds', 'rkcif-mipi-lvvds1', 'rkcif-mipi-lvds2']"}]}
    reason = failure_reasons(case)[0]
    assert '缺少节点：rkcif-mipi-lvds1' in reason['detail']
    assert '异常节点：rkcif-mipi-lvvds1' in reason['detail']
    assert '尚未完成' in reason['suggestion']


@pytest.mark.parametrize('mode', ['verified', 'changed', 'outside', 'wrong_command', 'no_error_flag', 'other_step'])
def test_temperature_requires_verified_matching_protocol(tmp_path, mode):
    root = tmp_path / 'run'
    root.mkdir()
    payload = b'unsupported command'
    word = len(payload) | (0 if mode == 'no_error_flag' else 0x8000)
    row = {'step': 'another-step' if mode == 'other_step' else 'temperature-0',
           'command_id': 0x1134 if mode == 'wrong_command' else 0x1135,
           'direction': 'RX', 'return_code': 0, 'hex': (word.to_bytes(2, 'little') + payload).hex()}
    raw = json.dumps(row).encode()
    path = (tmp_path if mode == 'outside' else root) / 'protocol.jsonl'
    path.write_bytes(raw + (b' ' if mode == 'changed' else b''))
    digest = hashlib.sha256(raw).hexdigest()
    case = {'verdict': 'error', 'message': 'temperature-0 tool exit 1', 'assertions': [],
            'evidence': [{'path': 'output/execution/protocol.jsonl', 'sha256': digest}]}
    report = {'result': {'cases': [case], 'evidence': [{'path': str(path), 'sha256': digest, 'evidence_id': 'trace'}]}}
    annotate_report(report, root)
    reason = case['failure_reasons'][0]
    if mode == 'verified':
        assert reason['title'] == '固件不支持六路温度读取命令'
        assert reason['evidence_id'] == 'trace'
    else:
        assert reason['title'] == '温度读取工具执行失败'
        assert 'unsupported' not in str(reason)
    assert case['verdict'] == 'error'


def test_passed_and_pending_do_not_acquire_failure():
    for verdict in ['passed', 'pending', 'running']:
        assert failure_reasons({'verdict': verdict, 'message': 'note'}) == []


def test_business_file_integrity_is_not_a_tool_precondition():
    assert assertion_role({'name': 'Calibration SHA-256'}) == 'business'
    assert assertion_role({'name': 'Tool SHA-256'}) == 'auxiliary'


def test_export_failure_before_folded_checks_and_escape():
    case = {'binding_id': 'c', 'verdict': 'failed', 'assertions': [
        {'name': '<unsafe>', 'passed': False, 'expected': 'a', 'actual': '<script>'},
        {'name': 'Tool SHA-256', 'passed': True}]}
    report = {'id': 'run', 'verdict': 'failed', 'created_at': '2026-10-14T07:00:00Z',
              'summary': {'total': 1, 'passed': 0}, 'result': {'cases': [case]},
              'request_snapshot': {'cases': [{'binding_id': 'c', 'name': 'Example', 'required': True}]}}
    annotate_report(report)
    html = report_html(report)
    assert html.index('class="failure"') < html.index('<summary>检查详情')
    assert '<details><summary>检查详情' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '辅助检查通过不代表整个用例通过' in html
