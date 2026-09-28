"""Read-only information reporting cases for the locked RMSL321 module."""
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import time

from test_runtime import Case
from switch_runtime import Capture
from worker import atomic, digest, timestamp

CHANNELS = ('soc', 'imu', 'tx', 'ir_l', 'ir_r', 'rgb')


def temperatures(text):
    fields = dict(re.findall(r'([a-z_]+)=([^\s]+)', text))
    if set(fields) != set(CHANNELS):
        raise ValueError('Temperature channels missing or unexpected: ' + text)
    values = {}
    for name in CHANNELS:
        if fields[name] == 'na':
            raise ValueError(name + ' temperature unavailable')
        value = float(fields[name])
        if not math.isfinite(value):
            raise ValueError(name + ' temperature invalid')
        values[name] = value
    return values


def status_count(text, label, model):
    match = re.search(r'^' + re.escape(label) + r': ' + re.escape(model) + r' count=(\d+)', text, re.M)
    if not match:
        raise ValueError('Malformed ' + label + ' status')
    return int(match[1])


def ping_samples(text, count):
    samples = [float(x) for x in re.findall(r'^ping \d+: ([\d.]+) ms$', text, re.M)]
    if len(samples) != count or not all(math.isfinite(x) and x >= 0 for x in samples):
        raise ValueError('Incomplete or invalid ping RTT samples')
    return samples


class InfoCase(Case):
    def record(self, name, value):
        path = self.out / 'artifacts' / (name + '.json')
        path.parent.mkdir(exist_ok=True)
        atomic(path, value)
        self.result['files'].append({'path': str(path.relative_to(self.out)), 'role': 'measurement',
                                     'size_bytes': path.stat().st_size, 'sha256': digest(path)})
        self.save()

    def run(self):
        capture = None
        try:
            if self.data.get('parameters'):
                raise ValueError('Information cases do not accept parameters')
            tool = Path(self.data['tool'])
            self.check('Tool SHA-256', self.data['tool_sha256'], digest(tool), digest(tool) == self.data['tool_sha256'])
            if self.code == 'INFO-PING':
                observations = {}
                for phase in ('idle', 'stream'):
                    if phase == 'stream':
                        capture = Capture(self, 'ping-stream')
                        capture.snapshot()
                    started = timestamp()
                    samples = ping_samples(self.call('ping-' + phase, 'ping', 10), 10)
                    ordered = sorted(samples)
                    observations[phase] = {'timestamp': started, 'count': len(samples), 'rtt_ms': samples,
                                           'p50_ms': statistics.median(samples), 'p95_ms': ordered[9],
                                           'p99_ms': ordered[9], 'max_ms': ordered[-1]}
                    self.check(phase + ' ping responses', 10, len(samples), len(samples) == 10)
                    if capture:
                        capture.close(); capture = None
                self.record('ping', observations)
            elif self.code == 'INFO-SENSORS':
                capture = Capture(self, 'sensor-status')
                capture.snapshot()
                sensor = self.call('sensor-status', 'sensor-status')
                vicap = self.call('vicap-status', 'vicap-status')
                isp = self.call('isp-status', 'isp-status')
                count = status_count(sensor, 'sensor', 'sc132gs')
                self.check('SC132GS sensor count', 3, count, count == 3)
                self.check('VICAP status present', 'rkcif proc entries without error', vicap[:500],
                           '### /proc/rkcif' in vicap and 'error:' not in vicap)
                self.check('ISP status present', 'rkisp proc entries without error', isp[:500],
                           '### /proc/rkisp' in isp and 'error:' not in isp)
                self.record('sensor-status', {'sensor': sensor, 'vicap': vicap, 'isp': isp})
            elif self.code == 'INFO-IMU':
                status = self.call('imu-status', 'imu-status')
                count = status_count(status, 'imu iio', 'icm42607')
                self.check('IMU application running', 'running pid', status.splitlines()[0],
                           bool(re.search(r'^imu app: running pid=\d+$', status, re.M)))
                self.check('ICM42607 IIO present', 'at least one', count, count >= 1)
                hidraw = self.data.get('hidraw')
                imu_tool = Path(self.data['imu_tool'])
                self.check('IMU sampler SHA-256', self.data['imu_tool_sha256'], digest(imu_tool),
                           digest(imu_tool) == self.data['imu_tool_sha256'])
                if not hidraw or not re.fullmatch(r'/dev/hidraw\d+', hidraw):
                    raise RuntimeError('No HID node bound to locked USB port')
                command = [str(imu_tool), '--hidraw', hidraw, '6', '2']
                proc = subprocess.run(command, capture_output=True, text=True, timeout=15)
                output = proc.stdout + proc.stderr
                self.result['steps'].append({'id': 'imu-samples', 'command': command, 'started_at': timestamp(),
                                             'completed_at': timestamp(), 'exit_code': proc.returncode,
                                             'stdout': proc.stdout[-1500:], 'stderr': proc.stderr[-1000:]})
                self.check('IMU sampler exit', 0, proc.returncode, proc.returncode == 0)
                windows = [(int(n), float(rate)) for n, rate in re.findall(r'\[win \d+\] samples=(\d+) rate=([\d.]+) Hz', output)]
                self.check('IMU actual samples', 'nonempty windows with finite positive rate', windows,
                           bool(windows) and all(n > 0 and math.isfinite(rate) and rate > 0 for n, rate in windows))
                self.record('imu', {'status': status, 'hidraw': hidraw, 'windows': windows, 'raw_output': output})
            elif self.code == 'INFO-TEMPERATURE':
                capture = Capture(self, 'temperature-stream')
                capture.snapshot()
                measurements = []
                for index in range(3):
                    value = temperatures(self.call('temperature-' + str(index), 'get-temperature'))
                    measurements.append({'timestamp': timestamp(), 'celsius': value})
                    if index < 2:
                        time.sleep(1)
                for name in CHANNELS:
                    self.check(name + ' temperature reporting', 'three finite readings during stream',
                               [sample['celsius'][name] for sample in measurements],
                               len(measurements) == 3)
                self.record('temperatures', measurements)
            else:
                raise RuntimeError('Unsupported information case')
            trace = self.out / 'protocol.jsonl'
            self.check('Actual XU evidence', 'nonempty protocol.jsonl', trace.stat().st_size if trace.exists() else 0,
                       trace.exists() and trace.stat().st_size > 0)
            self.result['outcome'] = 'passed'
        except AssertionError as exc:
            self.result.update(outcome='failed', message=str(exc))
        except Exception as exc:
            self.result.update(outcome='error', message=str(exc))
        finally:
            if capture:
                capture.close()
            self.result['recovery'] = {'state': 'passed', 'detail': 'Read-only controls; capture closed'}
            self.result.update(state='finished', completed_at=timestamp())
            self.save()
        return 0 if self.result['outcome'] == 'passed' else 1


def main(code):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    raise SystemExit(InfoCase(code, json.loads(Path(args.input).read_text()), Path(args.output_dir)).run())
