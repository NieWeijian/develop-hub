"""One frozen, operator-authorized action on a bound module. No arbitrary shell."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import stat
import subprocess
import time


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def validate_sn(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 56 or any(not 0x20 < ord(c) <= 0x7e or c in '\\"' for c in value):
        raise ValueError('SN requires 1-56 visible ASCII bytes, without spaces, quotes or backslashes')
    return value


def parse_listing(text):
    entries = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([^/\t]+)\t(\d+)(/?)', line)
        if not match or match[1] in ('.', '..') or match[1] in entries:
            raise ValueError('Invalid file-list response')
        entries[match[1]] = {'size': int(match[2]), 'directory': bool(match[3])}
    return entries


def explicit_log_write_failures(errors):
    """Separate device write rejection from an unavailable host/tool result."""
    failures = []
    for error in errors:
        response = error.get('response', {})
        text = str(response.get('stdout', '')) + '\n' + str(response.get('stderr', ''))
        if re.search(r'\bwrite failed\b|ENOSPC|No space left|errno[=: ]+28\b', text, re.I):
            failures.append(error)
    return failures


def iso_time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return result


def analyze_aging(text, min_seconds, max_temperature_c, elapsed_wall_seconds=None):
    metrics = {}
    def result(ok, reason):
        return {'passed': ok, 'reason': reason, 'metrics': metrics}
    if not math.isfinite(float(min_seconds)) or min_seconds < 1 or not math.isfinite(float(max_temperature_c)):
        return result(False, 'Invalid aging thresholds')
    if 'aging: skip existing result=' in text:
        return result(False, 'Existing aging result was reused; reset and execute a fresh cycle')
    starts = list(re.finditer(r'struct_light_aging start', text))
    finals = list(re.finditer(r'aging: final result=([01]) reason=([^\r\n]+)', text))
    if len(starts) != 1 or len(finals) != 1 or finals[0].start() <= starts[0].start():
        return result(False, 'Require one fresh start and one final result; missing evidence or restart detected')
    final = finals[0]
    lines = text[starts[0].end():final.start()].splitlines()
    samples = []
    for line in lines:
        if 'aging_telemetry ' not in line:
            continue
        values = dict(re.findall(r'([a-z0-9_]+)=(-?\d+(?:\.\d+)?)(?=\s|$)', line))
        required = ('elapsed_sec', 'fps_ir_l', 'fps_ir_r', 'fps_rgb', 'sensor_ok', 'imu_ok', 'rk803_ok', 'temp_ok', 'overall_ok', 'temp_cpu_x10', 'temp_imu_x10', 'temp_sensor_x10')
        if any(key not in values for key in required):
            return result(False, 'Incomplete aging telemetry')
        samples.append({key: float(values[key]) for key in required})
    if not samples:
        return result(False, 'No fresh aging telemetry')
    elapsed = [s['elapsed_sec'] for s in samples]
    metrics.update(samples=len(samples), elapsed_seconds=max(elapsed), temperature_peak_c=max(s[key] / 10 for s in samples for key in ('temp_cpu_x10', 'temp_imu_x10', 'temp_sensor_x10')))
    if any(b < a for a, b in zip(elapsed, elapsed[1:])):
        return result(False, 'Aging elapsed clock reset')
    if max(elapsed) < min_seconds:
        return result(False, 'Observed aging duration is shorter than required')
    if elapsed_wall_seconds is not None and (elapsed_wall_seconds < min_seconds or max(elapsed) > elapsed_wall_seconds + 10):
        return result(False, 'Capture wall time does not support the claimed aging duration')
    if any(s[key] != 1 for s in samples for key in ('sensor_ok', 'imu_ok', 'rk803_ok', 'temp_ok', 'overall_ok')):
        return result(False, 'Aging telemetry recorded an unhealthy component')
    if metrics['temperature_peak_c'] > max_temperature_c:
        return result(False, 'Aging temperature exceeded the configured limit')
    if final[1] != '1' or final[2].strip() != 'duration_elapsed':
        return result(False, 'Firmware did not finish the requested aging cycle successfully')
    return result(True, 'Fresh aging cycle, elapsed time, health and temperature evidence passed')


def analyze_ota(variant, log, progress_before_cut='', cut_at=''):
    flashed = bool(re.search(r'ota-upgrade-\w+-(?:start|success)|ota-done', log))
    metrics = {'variant': variant, 'upgrade_observed': flashed, 'evidence_source': 'operator_submitted_raw_log'}
    def result(ok, reason):
        return {'passed': ok, 'reason': reason, 'metrics': metrics}
    if variant == 'corrupt':
        return result('ota-md5check-fail' in log and not flashed, 'Require checksum rejection before any partition upgrade')
    if variant == 'wrong_name':
        return result(bool(re.search(r'\bota-(?:namecheck|filename-check)-fail\b', log)) and not flashed,
                      'Require explicit filename rejection; checksum failure does not prove filename validation')
    if variant != 'interrupted':
        return result(False, 'Unsupported OTA variant')
    try:
        cut = iso_time(cut_at)
        entries = []
        for line in progress_before_cut.splitlines():
            match = re.search(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)).*?(ota-upgrade-\w+-start)', line)
            if match:
                entries.append((iso_time(match[1]), match[2]))
        if not entries:
            return result(False, 'No timestamped partition upgrade start before the power cut')
        started, phase = entries[-1]
        seconds = (cut - started).total_seconds()
        metrics.update(cut_at=cut_at, phase=phase, seconds_after_upgrade_start=seconds)
        if not 0 <= seconds <= 120 or phase not in log:
            return result(False, 'Power-cut evidence is outside the observed upgrade window')
        event_pattern = r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)).*?(ota-(?:upgrade-\w+-(?:start|success|fail)|done|fail))'
        events = [(iso_time(m[1]), m[2]) for m in re.finditer(event_pattern, log)]
        if (started, phase) not in events:
            return result(False, 'Submitted progress event is not present in the complete timestamped log')
        pending = [item for item in events if item[1].endswith('-start') and item[0] <= cut]
        if pending and max(pending, key=lambda item: item[0]) != (started, phase):
            return result(False, 'Power-cut evidence does not identify the latest upgrade stage')
        end_prefix = phase[:-len('start')]
        if any(started <= when <= cut and (marker in ('ota-fail', 'ota-done') or marker in (end_prefix + 'success', end_prefix + 'fail')) for when, marker in events):
            return result(False, 'The observed upgrade stage had already ended before the power cut')
        # Any completion before the submitted cut invalidates this injection attempt.
        for line in (log + '\n' + progress_before_cut).splitlines():
            if 'ota-done' not in line:
                continue
            match = re.search(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))', line)
            if not match or iso_time(match[1]) <= cut:
                return result(False, 'Upgrade had already completed before power was removed')
        return result(True, 'Timestamped manual power cut during a partition upgrade; recovery checked separately')
    except (ValueError, TypeError):
        return result(False, 'Invalid power-cut timestamp; ISO time with timezone is required')


class Blocked(RuntimeError):
    pass


class Runtime:
    def __init__(self, request, output):
        self.request = request
        self.output = Path(output)
        self.out = self.output.parent
        self.out.mkdir(parents=True, exist_ok=True)
        self.parameters = request.get('parameters', {})
        self.context = request.get('context', {})
        self.device = request.get('device', {})
        self.tool = Path(request.get('tool', ''))
        self.result = {'outcome': 'blocked', 'message': '', 'started_at': timestamp(), 'data': {}, 'assertions': [], 'commands': [], 'files': []}
        self.video = None
        self.usb_generation = None

    def cancelled(self):
        if (self.out / 'CANCEL_REQUESTED').exists():
            raise Blocked('Operator requested cancellation; partial evidence retained')

    def guard_binding(self):
        if self.usb_generation is None:
            return
        port = Path('/sys/bus/usb/devices') / self.device['usb_port']
        try:
            generation = ((port / 'busnum').read_text().strip(), (port / 'devnum').read_text().strip())
            node = Path('/sys/class/video4linux') / Path(self.video).name / 'device'
            if generation != self.usb_generation or port.resolve() not in node.resolve().parents:
                raise Blocked('USB connection generation changed; refusing further commands to a reused video node')
        except OSError as exc:
            raise Blocked('Locked USB device disappeared; no further commands issued') from exc

    def save(self):
        atomic(self.output, self.result)

    def check(self, name, expected, actual, passed):
        self.result['assertions'].append({'name': name, 'expected': expected, 'actual': actual, 'passed': bool(passed)})
        self.save()
        if not passed:
            raise AssertionError(name)

    def previous(self, operation):
        return self.context.get(operation) or self.context.get('results', {}).get(operation) or {}

    def artifact(self, path, role='evidence'):
        path = Path(path)
        self.result['files'].append({'path': str(path.relative_to(self.out)), 'role': role, 'size_bytes': path.stat().st_size, 'sha256': digest(path)})

    def call(self, *args, required=True, timeout=30):
        if not self.video:
            raise Blocked('No verified module binding')
        self.guard_binding()
        command = [str(self.tool), '--device', self.video, *map(str, args)]
        env = dict(os.environ, RMSL_XU_TRACE=str(self.out / 'protocol.jsonl'), RMSL_TRACE_STEP=str(args[0]))
        start = timestamp()
        try:
            process = subprocess.run(command, capture_output=True, env=env, timeout=timeout)
            stdout, stderr, rc = process.stdout.decode(errors='replace'), process.stderr.decode(errors='replace'), process.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, rc = (exc.stdout or b'').decode(errors='replace'), 'Command timed out', 124
        record = {'command': command, 'started_at': start, 'completed_at': timestamp(), 'exit_code': rc, 'stdout': stdout, 'stderr': stderr}
        self.result['commands'].append(record)
        self.save()
        if rc and required:
            raise Blocked('Tool failed: ' + str(args[0]) + ': ' + stderr[-300:])
        return record

    def bind(self, allow_version_change=False):
        import sys
        root = str(Path(__file__).resolve().parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        import rv1126b
        rv1126b.CTL = self.tool
        found = [d for d in rv1126b.discover() if d['serial'] == self.device['serial'] and d['usb_port'] == self.device['usb_port']]
        if len(found) != 1:
            raise Blocked('Locked CPU serial and USB port not found; wait for the original module')
        if not allow_version_change and found[0]['version'] != self.device['version']:
            raise Blocked('Firmware differs from the frozen module version')
        self.video = found[0]['video']
        port = Path('/sys/bus/usb/devices') / self.device['usb_port']
        self.usb_generation = ((port / 'busnum').read_text().strip(), (port / 'devnum').read_text().strip())
        self.result['data']['device'] = found[0]
        return found[0]

    def health(self, allow_version_change=False):
        found = self.bind(allow_version_change)
        sn = self.call('get-sn')['stdout'].strip()
        health = self.call('health')['stdout']
        self.result['data'].update(sn=sn, version=found['version'], health=health)
        expected_sn = self.previous('inspect').get('sn')
        if expected_sn and self.request.get('operation') in ('ota_status', 'verify_health') and self.parameters.get('variant'):
            self.check('OTA preserved module SN', expected_sn, sn, sn == expected_sn)
        healthy = all(re.search(pattern, health, re.M) for pattern in (r'^usb link: UP\b', r'^app: RUNNING$', r'^sensor: 3/3 SC132GS OK$', r'^imu: OK$'))
        self.check('Module health', 'USB UP / app RUNNING / sensors 3/3 / IMU OK', health, healthy)

    def inspect(self):
        self.health()
        self.result['data']['inspected_at'] = timestamp()

    def set_sn(self):
        target = validate_sn(self.parameters.get('target_sn'))
        self.bind()
        self.call('set-sn', target)
        # No additional get-sn, sleep or health command before the operator removes power.
        self.result['data'].update(target_sn=target, write_completed_at=timestamp(), next_action='立即拔下模组供电及 USB；确认断电后再插回。')
        self.check('SN write acknowledged', 'set-sn exit 0', 0, True)

    def verify_disconnected(self):
        port = self.device['usb_port']
        if not re.fullmatch(r'\d+-\d+(?:\.\d+)*', port):
            raise ValueError('Invalid locked USB port')
        path = Path('/sys/bus/usb/devices') / port
        absent = not path.exists()
        time.sleep(0.5)
        absent = absent and not path.exists()
        self.check('Physical USB port absent', True, absent, absent)
        observed = timestamp()
        self.result['data'].update(usb_port=port, disconnected_at=observed, observation='sysfs port absent in two samples 500 ms apart; operator confirms power removal')
        written = self.previous('set_sn').get('write_completed_at')
        if written:
            self.result['data']['write_to_observation_seconds'] = (iso_time(observed) - iso_time(written)).total_seconds()

    def verify_sn(self):
        if not self.previous('verify_disconnected').get('disconnected_at'):
            raise Blocked('Physical disconnect evidence is required before cold-boot readback')
        self.bind()
        actual = self.call('get-sn')['stdout'].strip()
        target = validate_sn(self.parameters.get('target_sn'))
        self.result['data'].update(expected_sn=target, actual_sn=actual, reconnected_at=timestamp())
        self.check('Cold-boot SN persistence', target, actual, actual == target)

    def log_snapshot(self):
        result = {}
        for directory in ('/userdata/log', '/userdata/log/archived'):
            response = self.call('file-list', directory, required=False)
            if response['exit_code']:
                if directory.endswith('/archived'):
                    result[directory] = None
                    continue
                raise Blocked('Cannot inspect log directory')
            result[directory] = parse_listing(response['stdout'])
        return result

    def failed_log_write(self, errors):
        # A known device rejection remains FAIL even if post-failure inspection
        # is unavailable. Attempt both inspections and retain their limitations.
        data = self.result['data']
        data['write_failure_observed'] = True
        data['failure_category'] = 'device_write_rejected'
        data['failure_reason'] = 'log_write_failed'
        data['root_cause'] = 'Write rejection observed; filesystem fullness and rolling cause are separate diagnostic evidence'
        diagnostic_errors = data.setdefault('diagnostic_errors', [])
        try:
            self.bind()
            current = self.log_snapshot()
            data.setdefault('snapshots', []).append({'at': timestamp(), 'entries': current})
        except Exception as exc:
            diagnostic_errors.append({'operation': 'log_snapshot', 'message': str(exc)})
        try:
            self.health()
        except Exception as exc:
            diagnostic_errors.append({'operation': 'health', 'message': str(exc)})
        response = errors[-1].get('response', {})
        detail = (response.get('stderr') or response.get('stdout') or 'write rejected').strip()[-300:]
        self.check('Log pressure failed: ' + detail, 'No device write rejection during pressure uploads',
                   {'successful_bytes': data.get('successful_bytes', 0), 'errors': errors}, False)

    def log_pressure(self):
        target_mib = float(self.parameters.get('total_mib', 24))
        if not math.isfinite(target_mib) or not 20 < target_mib <= 64:
            raise ValueError('Log pressure target must be >20 and <=64 MiB')
        self.bind()
        run_id = self.request['run_id'].replace('-', '')
        if not re.fullmatch(r'[a-zA-Z0-9]{8,64}', run_id):
            raise ValueError('Invalid run identifier')
        prefix = 'qa-' + run_id[:12] + '-'
        before = self.log_snapshot()
        if any(name.startswith(prefix) for name in before['/userdata/log']):
            raise Blocked('Pressure files already exist for this run; never overwrite or replay')
        payload = self.out / 'pressure-payload.bin'
        payload.write_bytes(os.urandom(60000))
        self.artifact(payload, 'input')
        uploaded = 0
        names = []
        snapshots = [{'at': timestamp(), 'entries': before}]
        errors = []
        goal = math.ceil(target_mib * 1024 * 1024)
        self.result['data'].update(target_bytes=goal, successful_bytes=0, file_prefix=prefix, files_written=names, snapshots=snapshots, errors=errors, actual_full_observed=False,
                                   recovery='Pressure .log files are eligible for firmware rolling; no broad clean-log is performed')
        deadline = time.monotonic() + 3600
        while uploaded < goal:
            self.cancelled()
            if time.monotonic() > deadline:
                raise Blocked('Pressure run exceeded one hour; evidence retained, do not replay writes')
            name = prefix + f'{len(names):04d}.log'
            response = self.call('file-put', payload, '/userdata/log/' + name, required=False, timeout=120)
            if response['exit_code'] or not re.search(r'\bwrote 60000 bytes to ', response['stdout']):
                errors.append({'name': name, 'response': response})
                self.result['data']['actual_full_observed'] = bool(re.search(r'ENOSPC|No space left|errno[=: ]+28', response['stdout'] + response['stderr'], re.I))
                break
            uploaded += 60000
            names.append(name)
            self.result['data']['successful_bytes'] = uploaded
            if len(names) % 20 == 0:
                snapshots.append({'at': timestamp(), 'entries': self.log_snapshot()})
            self.save()
        failures = explicit_log_write_failures(errors)
        if failures:
            self.failed_log_write(failures)
        snapshots.append({'at': timestamp(), 'entries': self.log_snapshot()})
        self.health()
        if errors:
            raise Blocked('Upload acknowledgement unavailable; inspect command/transport evidence before retrying')
        self.check('Requested pressure bytes acknowledged', goal, uploaded, uploaded >= goal)

    def log_verify(self):
        previous = self.previous('log_pressure')
        if not previous:
            raise Blocked('Pressure execution evidence missing')
        successful = previous.get('successful_bytes', 0)
        errors = previous.get('errors', [])
        self.result['data'].update(successful_bytes=successful, errors=errors,
                                   actual_full_observed=previous.get('actual_full_observed', False))
        failures = explicit_log_write_failures(errors)
        if failures:
            # Older frozen runtimes incorrectly passed the collection gate even
            # after a device write rejection. Preserve and classify that evidence.
            self.failed_log_write(failures)
        self.bind()
        current = self.log_snapshot()
        deleted = sorted(set(previous.get('files_written', [])) - set(current['/userdata/log']))
        managed_size = sum(item['size'] for entries in current.values() if entries is not None for name, item in entries.items() if not item['directory'] and name.endswith(('.log', '.gz', '.pending')))
        self.result['data'].update(reclaimed_files=deleted, managed_log_bytes=managed_size,
                                   coverage='cumulative_upload_and_observed_rolling; actual filesystem capacity is not measurable through current CLI', snapshots=current)
        self.health()
        if errors:
            raise Blocked('Upload acknowledgement unavailable; inspect command/transport evidence before retrying')
        if successful <= 20 * 1024 * 1024:
            raise Blocked('Pressure evidence does not cover >20 MiB acknowledged uploads')
        if any(value is None for value in current.values()):
            raise Blocked('Cannot inspect all managed log directories; rolling convergence evidence is incomplete')
        self.check('More than 20 MiB uploaded', '>20971520', successful, True)
        self.check('Rolling reclaimed test files and converged', '<=4 MiB and deleted test files',
                   {'bytes': managed_size, 'reclaimed': len(deleted)}, bool(deleted) and managed_size <= 4 * 1024 * 1024)

    def serial_probe(self):
        ports = []
        candidates = [*Path('/dev/serial/by-id').glob('*'), *Path('/dev').glob('ttyUSB*'), *Path('/dev').glob('ttyACM*')]
        seen = set()
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen or not path.exists() or not stat.S_ISCHR(path.stat().st_mode):
                continue
            seen.add(resolved)
            ports.append({'path': str(path), 'resolved': str(resolved), 'readable': os.access(path, os.R_OK), 'writable': os.access(path, os.W_OK)})
        self.result['data'].update(available=ports, recommendation='Connect FTDI or CP210x USB-TTL to 3588. No board UART is selected automatically.')
        self.check('Serial enumeration completed', 'Enumerated; manual capture allowed when empty', len(ports), True)

    def aging_capture(self):
        if self.parameters.get('capture_mode') == 'manual':
            self.result['data'].update(mode='manual', requested_at=timestamp(), hardware_active=True, message='Start raw serial logging, then power the aging fixture. Submit complete log and capture timestamps at next gate.')
            self.check('Manual evidence gate opened', 'Await raw log; no aging pass claimed', 'waiting', True)
            return
        import termios
        path = Path(self.parameters.get('serial_device', ''))
        resolved = path.resolve()
        if not re.fullmatch(r'/dev/tty(?:USB|ACM)\d+', str(resolved)) or not path.exists() or not stat.S_ISCHR(path.stat().st_mode):
            raise Blocked('Select a detected USB serial device on 3588')
        baud = int(self.parameters.get('baud_rate', 115200))
        speed = getattr(termios, 'B' + str(baud), None)
        if speed is None:
            raise ValueError('Baud rate not supported by this host')
        seconds = int(self.parameters.get('capture_timeout_seconds', 15000))
        if not 60 <= seconds <= 90000:
            raise ValueError('Invalid serial capture timeout')
        import fcntl
        fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        previous = termios.tcgetattr(fd)
        raw = self.out / 'aging-serial.log'
        started = timestamp()
        start = time.monotonic()
        size, tail = 0, ''
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            config = termios.tcgetattr(fd)
            config[0] = config[1] = config[3] = 0
            config[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            config[4] = config[5] = speed
            config[6][termios.VMIN], config[6][termios.VTIME] = 0, 0
            termios.tcsetattr(fd, termios.TCSANOW, config)
            termios.tcflush(fd, termios.TCIFLUSH)
            self.result['data'].update(mode='serial', serial_device=str(path), serial_resolved=str(resolved), baud_rate=baud, capture_started_at=started, capture_ready=True, hardware_active=True)
            self.save()
            with raw.open('wb') as handle:
                while time.monotonic() - start < seconds:
                    self.cancelled()
                    ready, _, _ = select.select([fd], [], [], 1)
                    if not ready:
                        continue
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        raise Blocked('USB serial device disconnected during capture')
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise Blocked('Serial evidence exceeds 64 MiB; capture stopped')
                    handle.write(chunk)
                    handle.flush()
                    tail = (tail + chunk.decode(errors='replace'))[-4096:]
                    if re.search(r'aging: final result=[01] reason=[^\r\n]+[\r\n]', tail):
                        break
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, previous)
            except OSError as exc:
                self.result['data']['serial_restore_error'] = str(exc)
            finally:
                os.close(fd)
            self.result['data'].update(capture_ended_at=timestamp(), elapsed_wall_seconds=time.monotonic() - start, serial_log_path=str(raw))
            if raw.exists():
                self.artifact(raw)
            self.save()
        self.check('Serial log captured', '>0 bytes', size, size > 0)

    def aging_verify(self):
        capture = self.previous('aging_capture')
        if self.parameters.get('capture_mode') == 'manual':
            if self.parameters.get('manual_log_path'):
                path = Path(self.parameters['manual_log_path'])
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.out.resolve()) or path.stat().st_size > 64 * 1024 * 1024 or digest(path) != self.parameters.get('manual_log_sha256'):
                    raise Blocked('Uploaded manual log is outside this action or its checksum changed')
                text = path.read_text(errors='replace')
            else:
                text = self.parameters.get('manual_log', '')
            elapsed = (iso_time(self.parameters.get('ended_at', '')) - iso_time(self.parameters.get('started_at', ''))).total_seconds()
            source = 'operator_submitted_raw_log'
        else:
            path = Path(capture.get('serial_log_path', ''))
            if not path.is_file() or self.out.parent.resolve() not in path.resolve().parents or path.stat().st_size > 64 * 1024 * 1024:
                raise Blocked('Frozen serial evidence missing or outside this run')
            text = path.read_text(errors='replace')
            elapsed = capture.get('elapsed_wall_seconds', 0)
            source = '3588_serial_capture'
        evidence = self.out / 'aging-evidence.log'
        evidence.write_text(text)
        self.artifact(evidence)
        analysis = analyze_aging(text, float(self.parameters['duration_seconds']), float(self.parameters['max_temperature_c']), elapsed)
        finished = bool(re.search(r'struct_light_aging start[\s\S]*aging: final result=[01] reason=', text))
        self.result['data'].update(analysis, evidence_source=source, elapsed_wall_seconds=elapsed, hardware_active=not finished)
        self.check('Aging cycle evidence', 'Fresh full-duration healthy cycle below temperature limit', analysis, analysis['passed'])

    def ota_prepare(self):
        self.bind()
        source = Path(self.parameters.get('package_path', ''))
        if not source.is_absolute() or source.is_symlink() or not source.is_file() or not 1 <= source.stat().st_size <= 1024 * 1024 * 1024:
            raise Blocked('OTA source must be an existing regular package on 3588 (1 byte to 1 GiB)')
        expected = self.parameters.get('expected_sha256', '').lower()
        actual = digest(source)
        self.check('OTA source SHA-256', expected, actual, bool(re.fullmatch(r'[0-9a-f]{64}', expected)) and expected == actual)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.tar(?:\.gz|\.bz2|\.xz)?', source.name):
            raise Blocked('Select a tar OTA package with a safe basename, not a DFU partition image')
        variant = self.parameters.get('variant')
        if variant not in ('corrupt', 'wrong_name', 'interrupted'):
            raise ValueError('Unsupported OTA variant')
        directory = self.out / 'ota-package'
        directory.mkdir(exist_ok=False)
        name = 'wrong-name.tar.gz' if variant == 'wrong_name' else source.name
        if variant == 'wrong_name' and source.name == name:
            name = 'deliberately-invalid-name.tar.gz'
        package = directory / name
        original_md5 = digest(source, 'md5')
        shutil.copyfile(source, package)
        if digest(package) != actual:
            raise Blocked('Source package changed while copying')
        if variant == 'corrupt':
            with package.open('r+b') as handle:
                offset = package.stat().st_size // 2
                handle.seek(offset)
                byte = handle.read(1)
                handle.seek(offset)
                handle.write(bytes([byte[0] ^ 1]))
        trigger = directory / (name + '.md5')
        trigger.write_text(original_md5 + '  ' + name + '\n')
        for path in (package, trigger):
            self.artifact(path, 'ota-input')
        import shlex
        commands = [
            '# 以下在 3588 的终端执行。先核对 CPU serial=' + self.device['serial'] + '，USB port=' + self.device['usb_port'] + '；video 节点变化时重新核对，不要照抄旧节点。',
            shlex.quote(str(self.tool)) + ' --device ' + shlex.quote(self.video) + ' adb-mode 60',
            'adb devices -l',
            '# 将下面占位值换成已核对模组对应的 ADB serial。每个新终端都要设置；ADB serial 不等于 CPU serial。',
            "ADB_SERIAL='REPLACE_WITH_VERIFIED_ADB_SERIAL'",
            'adb -s "$ADB_SERIAL" shell mkdir -p /tmp/ota/firmware',
            'adb -s "$ADB_SERIAL" shell ls -la /tmp/ota/firmware',
            '# 若有旧 .md5 触发文件，先人工核查处理，再启动 monitor。不要让旧包触发新一轮升级。终端一：',
            'adb -s "$ADB_SERIAL" shell sh /oem/usr/bin/ota_check_firmware.sh',
            '# 终端二：在传包前启动下面的状态采集。每行带 3588 UTC 接收时间；保存原始模组行，避免猜模组时区。',
            'adb -s "$ADB_SERIAL" shell "tail -n +1 -F /tmp/ota/firmware/update_firmware.status" | while IFS= read -r line; do printf \'%s %s\\n\' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line"; done | tee ' + shlex.quote(str(self.out / 'ota-status-timestamped.log')),
            '# 终端三：先传包，最后传 .md5 才会触发；保留所有终端输出。',
            'adb -s "$ADB_SERIAL" push ' + shlex.quote(str(package)) + ' /tmp/ota/firmware/' + name,
            'adb -s "$ADB_SERIAL" push ' + shlex.quote(str(trigger)) + ' /tmp/ota/firmware/' + trigger.name,
            '# 中断场景：看到 ota-upgrade-<part>-start 后，在该分区升级尚未成功/失败前拔掉电源；同时在 3588 运行下行记录 UTC 断电时刻。',
            'date -u +%Y-%m-%dT%H:%M:%SZ',
            '# 保存本轮时间戳日志，恢复供电和普通 UVC 模式，插回原端口再提交健康回检。损坏包须 MD5 拒绝；错误名称须明确名称拒绝，接受则失败。'
        ]
        self.result['data'].update(variant=variant, source_path=str(source), source_sha256=actual, package_path=str(package), package_sha256=digest(package), trigger_path=str(trigger), original_md5=original_md5,
                                   instructions=commands, filename_validation_note='Current observed firmware script does not enforce a package filename pattern; accepting the wrong name must fail this case.', injection_mode='manual_adb_tar', hardware_active=True)
        self.check('Isolated OTA test package prepared', variant, variant, True)

    def ota_status(self):
        raw = self.parameters.get('ota_log', '')
        path = self.out / 'ota-evidence.log'
        path.write_text(raw)
        self.artifact(path)
        analysis = analyze_ota(self.parameters.get('variant'), raw, self.parameters.get('progress_before_cut', ''), self.parameters.get('cut_at', ''))
        self.result['data']['injection_analysis'] = analysis
        # Always attempt physical recovery verification, even when injection evidence failed.
        try:
            self.health(allow_version_change=True)
            self.result['data']['hardware_active'] = False
        except Exception as exc:
            self.result['data']['recovery_error'] = str(exc)
            self.result['assertions'].append({'name': 'OTA recovery', 'expected': 'same CPU / USB port / SN and healthy boot', 'actual': str(exc), 'passed': False})
        self.check('OTA abnormal input or power-cut evidence', 'Valid injection evidence and expected response', analysis, analysis['passed'])
        if any(not item['passed'] for item in self.result['assertions']):
            raise AssertionError('OTA recovery failed')

    def run_operation(self):
        allowed = ('inspect', 'set_sn', 'verify_disconnected', 'verify_sn', 'log_pressure', 'log_verify', 'serial_probe', 'aging_capture', 'aging_verify', 'ota_prepare', 'ota_status', 'verify_health')
        try:
            if self.request.get('operation') not in allowed:
                raise ValueError('Unsupported operation')
            if not self.tool.is_file() or digest(self.tool) != self.request.get('tool_sha256'):
                raise Blocked('Frozen tool checksum mismatch')
            if self.request['operation'] == 'verify_health':
                self.health(allow_version_change=bool(self.parameters.get('variant')))
            else:
                getattr(self, self.request['operation'])()
            self.result.update(outcome='passed', message='Step evidence recorded; continue at the next gate')
        except AssertionError as exc:
            self.result.update(outcome='failed', message=str(exc))
        except Exception as exc:
            self.result.update(outcome='blocked', message=str(exc))
        self.result['completed_at'] = timestamp()
        trace = self.out / 'protocol.jsonl'
        if trace.exists():
            self.artifact(trace, 'protocol')
        self.save()
        return self.result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text())
    Runtime(request, args.output).run_operation()
