import sys
from pathlib import Path

sys.path.insert(0, '/home/nwj/develop-hub/bench')

import pytest
import telemetry_runtime
from telemetry_runtime import CHANNELS, InfoCase, ping_samples, status_count, temperatures


def test_temperature_requires_all_six_numeric_channels():
    text = 'soc=42.1 imu=36.2 tx=41.0 ir_l=35.4 ir_r=35.5 rgb=38.0'
    assert tuple(temperatures(text)) == CHANNELS
    for bad in (text.replace('imu=36.2', 'imu=na'), text.replace('rgb=38.0', ''),
                text.replace('tx=41.0', 'tx=nan')):
        with pytest.raises(ValueError):
            temperatures(bad)


def test_ping_requires_all_samples():
    text = ''.join(f'ping {i}: {i}.25 ms\n' for i in range(1, 11))
    assert len(ping_samples(text, 10)) == 10
    with pytest.raises(ValueError):
        ping_samples(text.splitlines()[0], 10)


def test_status_counts_require_named_model():
    assert status_count('sensor: sc132gs count=3 (all present)', 'sensor', 'sc132gs') == 3
    assert status_count('imu iio: icm42607 count=1 (present)', 'imu iio', 'icm42607') == 1
    with pytest.raises(ValueError):
        status_count('sensor: other count=3', 'sensor', 'sc132gs')


def test_temperature_case_records_stream_and_rejects_missing_channel(tmp_path, monkeypatch):
    class Capture:
        def __init__(self, case, label):
            self.case = case
            self.label = label
        def snapshot(self):
            self.case.check('stream', 'advancing frames', 12, True)
        def close(self):
            pass
    monkeypatch.setattr(telemetry_runtime, 'Capture', Capture)
    monkeypatch.setattr(telemetry_runtime.time, 'sleep', lambda seconds: None)
    tool = tmp_path / 'ctl'
    tool.write_bytes(b'fake')
    answer = 'soc=42.1 imu=36.2 tx=41.0 ir_l=35.4 ir_r=35.5 rgb=38.0'
    monkeypatch.setattr(InfoCase, 'call', lambda self, step, *args: answer)
    data = {'tool': str(tool), 'tool_sha256': telemetry_runtime.digest(tool), 'parameters': {}}
    # Fake XU trace stands in for the control tool's trace output.
    (tmp_path / 'ok').mkdir()
    case = InfoCase('INFO-TEMPERATURE', data, tmp_path / 'ok')
    (case.out / 'protocol.jsonl').write_text('trace\n')
    assert case.run() == 0
    assert case.result['outcome'] == 'passed'
    assert len(case.result['files']) == 1
    missing = answer.replace('imu=36.2', 'imu=na')
    monkeypatch.setattr(InfoCase, 'call', lambda self, step, *args: missing)
    other = InfoCase('INFO-TEMPERATURE', data, tmp_path / 'bad')
    (other.out / 'protocol.jsonl').write_text('trace\n')
    assert other.run() == 1
    assert other.result['outcome'] == 'error'
