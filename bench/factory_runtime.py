"""Hub-owned storage/factory tests. Firmware mutations use only frozen CLI tools."""
import gzip
import hashlib
import json
from pathlib import Path
import re
import time

from test_runtime import Case
from switch_runtime import Capture, parse_params
from rv1126b import discover
from worker import atomic, digest, timestamp

ROOT = Path(__file__).resolve().parent
CALIB = '/calibration/calib_data/calib_param_1280x1080.yaml'
MAX_FILE = 60000


class Blocked(RuntimeError): pass


def listing(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([^/\t]+)\t(\d+)(/?)', line)
        if not match or match[1] in ('.', '..') or match[1] in result:
            raise ValueError('Invalid directory listing')
        result[match[1]] = {'size': int(match[2]), 'directory': bool(match[3])}
    return result


def board_sn(text):
    match = re.fullmatch(r'upper=([^\n]*)\nlower=([^\n]*)', text)
    if not match: raise ValueError('Invalid board SN response')
    return match[1], match[2]


def restorable_board(values):
    return all(1 <= len(s) <= 27 and all(0x20 < ord(c) <= 0x7e and c != '#' for c in s) for s in values)


class FactoryCase(Case):
    def artifact(self, path, role='output'):
        self.result['files'].append({'path': str(path.relative_to(self.out)), 'role': role,
                                     'size_bytes': path.stat().st_size, 'sha256': digest(path)})
        self.save()

    def download(self, step, remote, name):
        path = self.out / 'artifacts' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.call(step, 'file-get', remote, path)
        self.artifact(path)
        return path

    def match_file(self, step, remote, expected):
        actual = self.download(step, remote, step + '.bin')
        self.check(step + ' SHA-256', digest(expected), digest(actual), digest(expected) == digest(actual))

    def bind(self, timeout=120):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            devices = [d for d in discover() if d['serial'] == self.data['device']['serial'] and d['usb_port'] == self.data['device']['usb_port']]
            if len(devices) == 1:
                if devices[0]['version'] != self.data['device']['version']: raise RuntimeError('Firmware changed')
                self.data['video'] = devices[0]['video']
                return
            time.sleep(1)
        raise RuntimeError('Locked CPU serial / USB port did not return')

    def reboot(self, step, *command):
        self.call(step, *(command or ('reboot',)))
        end = time.monotonic() + 45
        # Prove disappearance, rather than immediately re-reading the old boot.
        while time.monotonic() < end:
            if not any(d['serial'] == self.data['device']['serial'] and d['usb_port'] == self.data['device']['usb_port'] for d in discover()):
                break
            time.sleep(.5)
        else: raise RuntimeError('Reboot requested but USB disappearance not observed')
        self.bind()

    def mode(self, target):
        current = parse_params(self.call('read-mode', 'get-param'))['system']['usb_mode']
        if current != target: self.reboot('mode-' + target, 'set-device-mode', target)
        actual = parse_params(self.call('verify-mode', 'get-param'))['system']['usb_mode']
        self.check('Device mode', target, actual, target == actual)

    def capture(self, label):
        capture = Capture(self, label)
        try: capture.snapshot()
        finally: capture.close()

    def logs(self, archived):
        if not archived:
            tail = self.call('log-tail', 'get-log')
            path = self.out / 'artifacts/module-tail.log'; path.write_text(tail)
            self.artifact(path)
            self.check('Module log tail', 'nonempty', len(tail), bool(tail.strip()))
        directory = '/userdata/log/archived' if archived else '/userdata/log'
        entries = listing(self.call('list-logs', 'file-list', directory))
        names = sorted(n for n, item in entries.items() if not item['directory'] and (n.endswith('.gz') if archived else n == 'lasted.log'))
        if not names: raise Blocked('No archived logs available' if archived else 'No active module log available')
        oversized = [n for n in names if entries[n]['size'] > MAX_FILE]
        if oversized: raise Blocked('Firmware file-get supports at most 60000 bytes; ranged download required: ' + ', '.join(oversized))
        for index, name in enumerate(names):
            path = self.download('download-log-' + str(index), directory + '/' + name, name)
            self.check(name + ' not empty', '>0 bytes', path.stat().st_size, path.stat().st_size > 0)
            if archived:
                self.check(name + ' complete length', entries[name]['size'], path.stat().st_size, path.stat().st_size == entries[name]['size'])
                # Reading to EOF verifies gzip trailer CRC and decompressed size.
                count = 0
                with gzip.open(path, 'rb') as stream:
                    while chunk := stream.read(65536):
                        count += len(chunk)
                        if count > 64 * 1024 * 1024: raise RuntimeError('Archive exceeds 64 MiB validation bound')
                self.check(name + ' gzip integrity', 'valid gzip and nonempty content', count, count > 0)

    def run(self):
        restore = None
        changed = False
        finish_normal = False
        original_mode = None
        try:
            (self.out / 'artifacts').mkdir(exist_ok=True)
            definition = self.data['definition']
            package = self.data['tools_package']
            missing = set(definition['required_capabilities']) - set(package['capabilities'])
            if missing: raise Blocked('Tool unsupported: ' + ', '.join(sorted(missing)))
            if self.data.get('parameters'): raise ValueError('This case does not accept parameters')
            self.check('Tool SHA-256', self.data['tool_sha256'], digest(Path(self.data['tool'])), digest(Path(self.data['tool'])) == self.data['tool_sha256'])
            self.bind()
            if self.code in ('FILE-MODULE-LOG', 'FILE-ARCHIVE-LOG'):
                self.logs(self.code == 'FILE-ARCHIVE-LOG')
            elif self.code == 'FILE-PERSIST-REBOOT':
                local = self.out / 'artifacts/payload.bin'
                local.write_bytes(bytes(range(256)) * 16); self.artifact(local, 'input')
                name = 'qa-' + hashlib.sha256(str(self.out).encode()).hexdigest()[:12] + '.bin'
                if name in listing(self.call('check-unused-path', 'file-list', '/userdata/log')):
                    raise Blocked('Dedicated test path already exists')
                remote = '/userdata/log/' + name
                self.result['retained_test_file'] = {'path': remote, 'size_bytes': 4096, 'reason': 'Firmware tools have no file-delete interface; isolated file retained as test evidence'}
                self.save()
                self.call('upload-persistent-file', 'file-put', local, remote)
                self.match_file('before-reboot', remote, local)
                self.reboot('reboot-file-test')
                self.match_file('after-reboot', remote, local)
            elif self.code in ('FACTORY-SN', 'FACTORY-BOARD-SN', 'FACTORY-CALIB'):
                params = parse_params(self.call('backup-mode', 'get-param'))
                original_mode = params['system']['usb_mode']
                finish_normal = self.code in ('FACTORY-SN', 'FACTORY-BOARD-SN')
                if self.code == 'FACTORY-BOARD-SN':
                    original = board_sn(self.call('backup-board-sn', 'get-board-sn'))
                    if not restorable_board(original):
                        raise Blocked('Original board SN is empty or not writable; firmware must provide empty-value restoration before this case can mutate it')
                    restore = ('board', original)
                else:
                    backup = self.download('backup-calibration', CALIB, 'backup/calib_param_1280x1080.yaml')
                    self.check('Calibration backup', 'nonempty file', backup.stat().st_size, backup.stat().st_size > 0)
                    restore = ('calibration', backup)
                    original = self.call('backup-module-sn', 'get-sn')
                self.mode('calib')
                suffix = hashlib.sha256(str(self.out).encode()).hexdigest()[:12]
                changed = True # Even an interrupted tool call may have applied its write.
                if self.code == 'FACTORY-BOARD-SN':
                    upper, lower = original
                    self.call('write-upper-sn', 'set-board-sn', 'QA-U-' + suffix, lower)
                    actual = board_sn(self.call('read-upper-sn', 'get-board-sn'))
                    self.check('Upper changed, lower unchanged', ('QA-U-' + suffix, lower), actual, actual == ('QA-U-' + suffix, lower))
                    target = ('QA-U-' + suffix, 'QA-L-' + suffix)
                    self.call('write-lower-sn', 'set-board-sn', *target)
                    getter = lambda step: board_sn(self.call(step, 'get-board-sn'))
                elif self.code == 'FACTORY-SN':
                    target = 'QA-M-' + suffix
                    self.call('write-module-sn', 'set-sn', target)
                    getter = lambda step: self.call(step, 'get-sn')
                else:
                    fixture = ROOT / definition['fixture_path']
                    self.check('Calibration fixture SHA-256', definition['fixture_sha256'], digest(fixture), digest(fixture) == definition['fixture_sha256'])
                    local = self.out / 'artifacts/input/calib_param_1280x1080.yaml'
                    local.parent.mkdir(parents=True, exist_ok=True); local.write_bytes(fixture.read_bytes()); self.artifact(local, 'input')
                    self.call('upload-calibration', 'set-calib', local)
                    self.match_file('uploaded-calibration', CALIB, local)
                    self.check('Uploaded module SN', 'R3211101263200010', self.call('uploaded-sn', 'get-sn'), self.call('verify-uploaded-sn', 'get-sn') == 'R3211101263200010')
                    target = None
                if target is not None:
                    actual = getter('immediate-sn-readback')
                    self.check('Written SN readback', target, actual, actual == target)
                    self.reboot('reboot-sn-test')
                    actual = getter('reboot-sn-readback')
                    self.check('SN persists after reboot', target, actual, actual == target)
                    self.mode('rgb+depth')
                    actual = getter('normal-mode-sn')
                    self.check('SN in rgb+depth mode', target, actual, actual == target)
                    self.capture('normal-mode-after-sn')
            else: raise RuntimeError('Unknown factory/storage case')
            self.result['outcome'] = 'passed'
        except Blocked as exc: self.result.update(outcome='blocked', message=str(exc))
        except AssertionError as exc: self.result.update(outcome='failed', message=str(exc))
        except Exception as exc: self.result.update(outcome='error', message=str(exc))
        finally:
            try:
                if changed:
                    self.bind(); self.mode('calib')
                    if restore[0] == 'board':
                        self.call('restore-board-sn', 'set-board-sn', *restore[1])
                    else:
                        self.call('restore-calibration', 'set-calib', restore[1])
                    self.reboot('reboot-restore')
                    if restore[0] == 'board':
                        value = board_sn(self.call('verify-board-restored', 'get-board-sn'))
                        self.check('Original board SN restored', restore[1], value, value == restore[1])
                    else:
                        self.match_file('original-calibration-restored', CALIB, restore[1])
                        value = self.call('verify-module-sn-restored', 'get-sn')
                        self.check('Original module SN restored', original, value, value == original)
                if original_mode is not None:
                    self.mode('rgb+depth' if finish_normal and changed else original_mode)
                self.result['recovery'] = {'state': 'passed', 'detail': 'Original data restored and verified' if changed else 'No SN/calibration data changed'}
            except Exception as exc:
                self.result.update(outcome='error', recovery={'state': 'failed', 'error': str(exc)})
            self.result.update(state='finished', completed_at=timestamp()); self.save()
        return 0 if self.result['outcome'] == 'passed' else 1


def main(code):
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument('--input', required=True); parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    raise SystemExit(FactoryCase(code, json.loads(Path(args.input).read_text()), Path(args.output_dir)).run())
