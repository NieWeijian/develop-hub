import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'bench'))
import telemetry_runtime as rt
from develop_hub.basic_cases import ACTIVE_CODES, validate_parameters
from fastapi import HTTPException


@pytest.fixture
def factory(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(rt.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(rt.time, 'sleep', lambda duration: clock.__setitem__(0, clock[0] + duration))
    monkeypatch.setattr(rt.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='get-temperature', stderr=''))
    class Capture:
        def __init__(self, case, label): self.case = case
        def snapshot(self, generation=0): self.case.check('stream', 'advancing frames', 12, True)
        def close(self): pass
    monkeypatch.setattr(rt, 'Capture', Capture)
    tool = tmp_path / 'tool'; tool.write_bytes(b'fake')
    def create(code, answer, parameters=None):
        case = rt.InfoCase(code, {'tools_package': {'capabilities': ['temperature.read.v1']}, 'tool': str(tool), 'tool_sha256': rt.digest(tool), 'parameters': parameters or {}}, tmp_path / code)
        (case.out / 'protocol.jsonl').write_text('trace\n')
        case.call = answer
        return case
    return create


@pytest.fixture
def historical():
    return json.loads((Path(__file__).parent / 'fixtures/info/sensor-status.json').read_text())


def test_sensor_role_mapping(historical):
    assert set(rt.sensor_mapping(historical['sensor'])) == {'ir_l', 'ir_r', 'rgb'}
    for text in (historical['sensor'].replace('3-0032', '3-0030'), historical['sensor'].replace('m01_b', 'm00_b'), historical['sensor'].replace('count=3', 'count=2')):
        with pytest.raises(ValueError): rt.sensor_mapping(text)


def test_proc_contract_rejects_missing_nodes_and_fields(historical):
    assert len(rt.vicap_stats(historical['vicap'])) == 3
    assert len(rt.isp_stats(historical['isp'])) == 3
    for key, parser, field in [('vicap', rt.vicap_stats, 'frame amount:'), ('isp', rt.isp_stats, 'ErrCnt:')]:
        with pytest.raises(ValueError): parser(historical[key].split('### /proc/', 2)[0])
        with pytest.raises(ValueError): parser(historical[key].replace(field, 'missing:'))


def sensor_answer(historical, fault=None):
    def answer(step, *args):
        if step == 'sensor-parameters': return '[algo]\nfps=10\n'
        if step == 'sensor-status': return historical['sensor']
        key = 'vicap' if step.startswith('vicap') else 'isp'
        text = historical[key]
        if step.endswith('-1'):
            pattern = r'(frame amount:)(\d+)' if key == 'vicap' else r'(\(frame:)(\d+)'
            delta = 0 if fault == 'stalled' else (-1 if fault == 'reset' else 50)
            text = re.sub(pattern, lambda m: m[1] + str(int(m[2]) + delta), text)
            if fault == 'cif_error': text = text.replace('all err count:0', 'all err count:1')
            if fault == 'isp_error': text = text.replace('ErrCnt:0', 'ErrCnt:1')
            if fault == 'loss': text = text.replace('frameloss:0', 'frameloss:1')
            if fault == 'fps': text = re.sub(r'fps:[\d.]+', 'fps:1.000', text)
        return text
    return answer


@pytest.mark.parametrize('fault', [None, 'stalled', 'reset', 'cif_error', 'isp_error', 'loss', 'fps'])
def test_sensor_window_judgement(factory, historical, fault):
    case = factory('INFO-SENSORS', sensor_answer(historical, fault))
    assert case.run() == (0 if fault is None else 1)
    assert case.result['outcome'] == ('passed' if fault is None else 'failed')


@pytest.mark.parametrize('rate,limit,passed', [(50, None, True), (50.01, None, False), (60, 60, True)])
def test_ping_threshold_and_visible_values(factory, rate, limit, passed):
    answer = lambda *args: '\n'.join(f'ping {i}: {rate} ms' for i in range(1, 11))
    case = factory('INFO-PING', answer, {} if limit is None else {'max_rtt_ms': limit})
    assert case.run() == (0 if passed else 1)
    rtt = [a for a in case.result['assertions'] if 'RTT' in a['name']]
    assert len(rtt) == 2
    assert rtt[0]['actual']['max_ms'] == rate


def test_temperature_all_channels_30_seconds_and_summary(factory):
    readings = iter(range(31))
    def answer(*args):
        value = next(readings)
        return ' '.join(f'{channel}={value}' for channel in rt.CHANNELS)
    case = factory('INFO-TEMPERATURE', answer)
    assert case.run() == 0
    data = json.loads((case.out / 'artifacts/temperatures.json').read_text())
    assert data['duration_seconds'] == 30
    assert len(data['summary']) == 6
    for stats in data['summary'].values():
        assert stats == {'min_c': 0, 'max_c': 30, 'median_c': 15, 'samples': 31}


def test_temperature_missing_tool_is_blocked_before_capture(factory, monkeypatch):
    case = factory('INFO-TEMPERATURE', lambda *a: pytest.fail('Must not call module'))
    case.data['tools_package'] = {'capabilities': []}
    monkeypatch.setattr(rt, 'Capture', lambda *a: pytest.fail('Must not open stream'))
    assert case.run() == 1
    assert case.result['outcome'] == 'blocked'


def test_temperature_invalid_channel_fails(factory):
    case = factory('INFO-TEMPERATURE', lambda *a: 'soc=40 imu=na tx=40 ir_l=40 ir_r=40 rgb=40')
    assert case.run() == 1
    assert case.result['outcome'] == 'failed'


def test_parameter_validation_and_imu_retirement():
    assert 'INFO-IMU' not in ACTIVE_CODES
    validate_parameters('INFO-PING', {'max_rtt_ms': 50})
    for value in [0, -1, True, float('nan'), '50']:
        with pytest.raises(HTTPException): validate_parameters('INFO-PING', {'max_rtt_ms': value})
