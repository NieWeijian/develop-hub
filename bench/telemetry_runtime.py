"""Read-only information reporting cases for the locked RMSL321 module."""
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import time

from test_runtime import Case
from switch_runtime import Capture, parse_params
from worker import atomic, digest, timestamp

CHANNELS = ('soc', 'imu', 'tx', 'ir_l', 'ir_r', 'rgb')

# RMSL321 board mapping, from firmware temperature.cpp and sensor endpoint names.
SENSORS = {'ir_l': ('m00_b_sc132gs', '3-0030'),
           'ir_r': ('m01_b_sc132gs', '3-0032'),
           'rgb': ('m02_b_sc132gs', '1-0030')}
CIF_ROLES = {'rkcif-mipi-lvds': 'ir_r', 'rkcif-mipi-lvds1': 'ir_l', 'rkcif-mipi-lvds2': 'rgb'}
ISP_INPUTS = {'rkisp-vir0': 'rkcif-mipi-lvds', 'rkisp-vir1': 'rkcif-mipi-lvds1', 'rkisp-vir2': 'rkcif-mipi-lvds2'}
CIF_ERRORS = ('csi over flow', 'csi bandwidth lack', 'csi size err', 'all err count',
              'err sot sync', 'err fs/fe not match', 'err frm seq', 'err crc once',
              'err crc', 'err ecc2', 'err ctrl', 'frame loss')


def sensor_mapping(text):
    entries = re.findall(r'^\s*(v4l-subdev\d+):\s+(m\d+_b_sc132gs)\s+(\d+-[0-9a-f]+)\s+node=(/dev/v4l-subdev\d+)\s*$', text, re.M)
    if status_count(text, 'sensor', 'sc132gs') != 3 or len(entries) != 3:
        raise ValueError('Expected three SC132GS entries')
    if any(len({row[index] for row in entries}) != 3 for index in range(4)):
        raise ValueError('SC132GS entries must be unique')
    if any('/dev/' + row[0] != row[3] for row in entries):
        raise ValueError('Sensor node identity mismatch')
    result = {}
    for role, expected in SENSORS.items():
        matches = [row for row in entries if row[1:3] == expected]
        if len(matches) != 1:
            raise ValueError('Incorrect SC132GS role mapping: ' + role)
        result[role] = {'sensor': matches[0][1], 'address': matches[0][2], 'node': matches[0][3]}
    return result


def proc_nodes(text, expected):
    parts = re.split(r'^### /proc/([^\s]+)\s*\n', text, flags=re.M)
    names = parts[1::2]
    if len(names) != len(set(names)) or set(names) != set(expected):
        raise ValueError('Missing, duplicate or unexpected proc nodes: ' + repr(names))
    return dict(zip(names, parts[2::2]))


def required(pattern, text, label):
    match = re.search(pattern, text, re.M)
    if not match:
        raise ValueError('Missing or malformed ' + label)
    return match


def vicap_stats(text):
    result = {}
    for node, body in proc_nodes(text, CIF_ROLES).items():
        sensor, address = SENSORS[CIF_ROLES[node]]
        required(r'^\s*src subdev:' + re.escape(sensor + ' ' + address) + r'\s*$', body, node + ' source mapping')
        frames = int(required(r'^\s*frame amount:(\d+)\s*$', body, node + ' frame amount')[1])
        fps = float(required(r'^\s*fps:([\d.]+)\s*$', body, node + ' fps')[1])
        if not math.isfinite(fps):
            raise ValueError('Invalid FPS')
        errors = {}
        for field in CIF_ERRORS:
            numbers = required(r'^\s*' + re.escape(field) + r':\s*(\d+(?:[ \t]+\d+)*)[ \t]*$', body, node + ' ' + field)[1]
            errors[field] = [int(value) for value in numbers.split()]
        result[node] = {'frames': frames, 'fps': fps, 'errors': errors}
    return result


def isp_stats(text):
    result = {}
    for node, body in proc_nodes(text, ISP_INPUTS).items():
        required(r'^Input\s+' + re.escape(ISP_INPUTS[node]) + r'\s', body, node + ' input mapping')
        frames = int(required(r'\(frame:(\d+)\s', body, node + ' frame')[1])
        error = int(required(r'^Interrupt\s+Cnt:\d+\s+ErrCnt:(\d+)', body, node + ' ErrCnt')[1])
        loss = int(required(r'\bframeloss:(\d+)\)', body, node + ' frameloss')[1])
        result[node] = {'frames': frames, 'errors': {'ErrCnt': [error], 'frameloss': [loss]}}
    return result


def sensor_fps_targets(streams):
    # Firmware Pipeline::aiq_init uses negotiated RGB FPS to clock RGB and IR.
    # algo.fps=0 means algorithm-library default, not a measured sensor rate.
    rgb = streams['rgb']['fps']
    if not math.isfinite(rgb) or rgb <= 0:
        raise ValueError('Invalid negotiated RGB FPS')
    divisor = 3 if rgb == 30 else (2 if rgb == 20 else 1)
    return {'rgb': rgb, 'ir_l': rgb / divisor, 'ir_r': rgb / divisor}


def counter_delta(before, after):
    if len(before) != len(after):
        raise ValueError('Counter vector changed')
    return [b - a for a, b in zip(before, after)]


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
    def judge(self, name, expected, actual, passed):
        # Collect every channel/node result instead of hiding later failures.
        self.result['assertions'].append({'name': name, 'expected': expected, 'actual': actual, 'passed': bool(passed)})
        self.save()

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
            parameters = self.data.get('parameters') or {}
            allowed = {'INFO-PING': {'max_rtt_ms'}, 'INFO-SENSORS': {'window_seconds', 'fps_tolerance_percent'}, 'INFO-TEMPERATURE': set()}.get(self.code, set())
            if set(parameters) - allowed:
                raise ValueError('Unsupported information case parameters')
            def positive(name, default, maximum):
                value = parameters.get(name, default)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
                    raise ValueError('Invalid parameter: ' + name)
                return value
            tool = Path(self.data['tool'])
            self.check('Tool SHA-256', self.data['tool_sha256'], digest(tool), digest(tool) == self.data['tool_sha256'])
            required_caps=self.data.get('definition',{}).get('required_capabilities',[])
            available_caps=(self.data.get('tools_package') or {}).get('capabilities',[])
            missing=sorted(set(required_caps)-set(available_caps))
            if missing:
                self.result.update(outcome='blocked',message='Tool package does not support: '+', '.join(missing))
                return 1
            if self.code == 'INFO-PING':
                limit = positive('max_rtt_ms', 50, 30000)
                observations = {}
                for phase in ('idle', 'stream'):
                    if phase == 'stream':
                        capture = Capture(self, 'ping-stream')
                        capture.snapshot()
                    started = timestamp()
                    samples = ping_samples(self.call('ping-' + phase, 'ping', 10), 10)
                    ordered = sorted(samples)
                    observations[phase] = {'timestamp': started, 'count': len(samples), 'rtt_ms': samples,
                                           'min_ms': ordered[0], 'mean_ms': statistics.mean(samples),
                                           'median_ms': statistics.median(samples), 'max_ms': ordered[-1], 'limit_ms': limit}
                    self.check(phase + ' ping responses', 10, len(samples), len(samples) == 10)
                    self.judge(phase + ' RTT (ms)', {'each_sample_max_ms': limit}, observations[phase], max(samples) <= limit)
                    if capture:
                        capture.snapshot(1)
                        capture.close(); capture = None
                self.record('ping', observations)
            elif self.code == 'INFO-SENSORS':
                window = positive('window_seconds', 5, 30)
                tolerance = positive('fps_tolerance_percent', 10, 50)
                params = parse_params(self.call('sensor-parameters', 'get-param'))
                capture = Capture(self, 'sensor-status')
                capture.snapshot()
                streams = capture.negotiated_streams()
                targets = sensor_fps_targets(streams)
                self.record('sensor-configuration', {'device_mode': params.get('system', {}).get('usb_mode'), 'algorithm_fps_configuration': params.get('algo', {}).get('fps'), 'negotiated_streams': streams, 'expected_sensor_fps': targets, 'expectation_source': 'Pipeline::aiq_init: RGB negotiated FPS; IR divided by 3 at 30 FPS / 2 at 20 FPS'})
                sensor = self.call('sensor-status', 'sensor-status')
                try:
                    mapping = sensor_mapping(sensor)
                except ValueError as exc:
                    self.check('Three unique SC132GS and role mapping', SENSORS, str(exc), False)
                self.check('Three unique SC132GS and role mapping', SENSORS, mapping, True)
                observations = []
                for index in range(2):
                    item = {'timestamp': timestamp()}
                    for kind, parser in (('vicap', vicap_stats), ('isp', isp_stats)):
                        item[kind + '_raw'] = self.call(kind + '-' + str(index), kind + '-status')
                        item[kind + '_time'] = time.monotonic()
                        try:
                            item[kind] = parser(item[kind + '_raw'])
                        except ValueError as exc:
                            self.check(kind + ' nodes and required fields', list(CIF_ROLES if kind == 'vicap' else ISP_INPUTS), str(exc), False)
                        self.check(kind + ' nodes and required fields ' + str(index), list(CIF_ROLES if kind == 'vicap' else ISP_INPUTS), list(item[kind]), True)
                    observations.append(item)
                    if index == 0:
                        time.sleep(window)
                self.record('sensor-status', {'sensor': sensor, 'mapping': mapping, 'observations': observations})
                before, after = observations
                for kind in ('vicap', 'isp'):
                    for node, current in after[kind].items():
                        previous = before[kind][node]
                        delta = current['frames'] - previous['frames']
                        self.judge(node + ' frame count advancing', '>0 (counter reset is failure)', delta, delta > 0)
                        for field, values in current['errors'].items():
                            change = counter_delta(previous['errors'][field], values)
                            self.judge(node + ' ' + field + ' increment', [0] * len(change), change, all(v == 0 for v in change))
                        if kind == 'vicap':
                            elapsed = after['vicap_time'] - before['vicap_time']
                            rates = {'before_reported': previous['fps'], 'after_reported': current['fps'], 'window_measured': delta / elapsed}
                            target = targets[CIF_ROLES[node]]
                            low, high = target * (1 - tolerance / 100), target * (1 + tolerance / 100)
                            self.judge(node + ' actual FPS', {'target': target, 'min': low, 'max': high, 'window_seconds': elapsed}, rates, all(low <= rate <= high for rate in rates.values()))
                capture.snapshot(1)
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
                # This CLI opens a module even for --help. Inspect the frozen manifest instead.
                package = self.data.get('tools_package') or {}
                if 'temperature.read.v1' not in package.get('capabilities', []):
                    self.result.update(outcome='blocked', message='Frozen tool package does not declare temperature.read.v1; import a supported tool package')
                    return 1
                capture = Capture(self, 'temperature-stream')
                capture.snapshot()
                measurements = []
                started = time.monotonic()
                for index in range(31):
                    time.sleep(max(0, started + index - time.monotonic()))
                    output = self.call('temperature-' + str(index), 'get-temperature')
                    try:
                        value = temperatures(output)
                    except ValueError as exc:
                        self.check('Six valid temperature channels', list(CHANNELS), str(exc), False)
                    measurements.append({'timestamp': timestamp(), 'elapsed_seconds': time.monotonic() - started, 'celsius': value})
                    atomic(self.out / 'temperature-samples.json', measurements)
                summary = {}
                for name in CHANNELS:
                    values = [sample['celsius'][name] for sample in measurements]
                    summary[name] = {'min_c': min(values), 'max_c': max(values), 'median_c': statistics.median(values), 'samples': len(values)}
                    self.check(name + ' temperature (C)', 'valid readings over 30 seconds; min/max/median', summary[name], measurements[-1]['elapsed_seconds'] >= 30)
                self.record('temperatures', {'duration_seconds': measurements[-1]['elapsed_seconds'], 'summary': summary, 'measurements': measurements})
                capture.snapshot(1)
            else:
                raise RuntimeError('Unsupported information case')
            trace = self.out / 'protocol.jsonl'
            self.check('Actual XU evidence', 'nonempty protocol.jsonl', trace.stat().st_size if trace.exists() else 0,
                       trace.exists() and trace.stat().st_size > 0)
            self.result['outcome'] = 'passed' if all(a['passed'] for a in self.result['assertions']) else 'failed'
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
