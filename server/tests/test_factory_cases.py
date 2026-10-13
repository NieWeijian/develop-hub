import gzip
import random
import subprocess
import re
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'bench'))
from factory_runtime import FactoryCase, CALIB, listing, restorable_board
from test_runtime import Case
from develop_hub.basic_cases import definition, ROOT


class FakeCase(FactoryCase):
    def bind(self, timeout=120): pass
    def reboot(self, step, *command): self.reboots.append(step)
    def mode(self, target): self.current_mode = target
    def capture(self, label): self.captures.append(label)
    def call(self, step, command, *args):
        self.calls.append((step, command, args))
        if command == 'get-param': return '[system]\nusb_mode=' + self.current_mode
        if command == 'get-sn': return re.search(rb'sn_code:\s*(\S+)', self.remote[CALIB])[1].decode()
        if command == 'set-sn':
            self.remote[CALIB] = ('sn_code: ' + args[0] + '\n').encode()
            if self.fail_write:
                self.fail_write = False
                raise RuntimeError('Simulated lost response after mutation')
        elif command == 'get-board-sn': return 'upper=' + self.boards[0] + '\nlower=' + self.boards[1]
        elif command == 'set-board-sn': self.boards = args
        elif command == 'set-calib': self.remote[CALIB] = Path(args[0]).read_bytes()
        elif command == 'file-get': Path(args[1]).write_bytes(self.remote[args[0]])
        elif command == 'file-put': self.remote[args[1]] = Path(args[0]).read_bytes()
        elif command == 'get-log': return 'module log tail'
        elif command == 'file-list':
            return '\n'.join(path.rsplit('/',1)[-1]+'\t'+str(len(data)) for path,data in self.remote.items() if path.rsplit('/',1)[0] == args[0])
        else: return ''
        return 'ok'


@pytest.fixture
def factory(tmp_path):
    def make(code):
        d=definition(code)
        case=FakeCase(code, {'definition':d,'tools_package':d['tools_package'],'tool':str(ROOT/d['tools_package']['binary_path']),'tool_sha256':d['tool_sha256'],'parameters':{}}, tmp_path/code)
        case.remote={CALIB:b'sn_code: ORIGINAL\n', '/userdata/log/lasted.log':b'hello module\n', '/userdata/log/archived/001.log.gz':gzip.compress(b'archived module log\n')}
        case.boards=('UPPER-ORIGINAL','LOWER-ORIGINAL');case.current_mode='calib';case.reboots=[];case.calls=[];case.captures=[];case.fail_write=False
        return case
    return make


@pytest.mark.parametrize('code',['FACTORY-SN','FACTORY-BOARD-SN','FACTORY-CALIB','FILE-PERSIST-REBOOT','FILE-MODULE-LOG','FILE-ARCHIVE-LOG'])
def test_cases_complete_and_restore(factory, code):
    case=factory(code)
    assert case.run()==0,case.result
    assert case.remote[CALIB]==b'sn_code: ORIGINAL\n'
    assert case.boards==('UPPER-ORIGINAL','LOWER-ORIGINAL')
    if code in ('FACTORY-SN','FACTORY-BOARD-SN'):
        assert case.current_mode=='rgb+depth'
        assert case.captures
        assert 'reboot-sn-test' in case.reboots and 'reboot-restore' in case.reboots
    if code=='FACTORY-CALIB':
        writes=[args[0] for _,command,args in case.calls if command=='set-calib']
        assert len(writes)==2
        assert Path(writes[0]).read_bytes()==(ROOT/definition(code)['fixture_path']).read_bytes()


def test_partial_sn_write_restores_original(factory):
    case=factory('FACTORY-SN');case.fail_write=True
    assert case.run()==1
    assert case.remote[CALIB]==b'sn_code: ORIGINAL\n'
    assert case.result['recovery']['state']=='passed'


@pytest.mark.parametrize('original,expected', [(('', ''), ('0', '0')), (('', 'LOWER'), ('0', 'LOWER')), (('UPPER', ''), ('UPPER', '0'))])
def test_empty_board_sn_restores_zero(factory, original, expected):
    case=factory('FACTORY-BOARD-SN');case.boards=original
    assert case.run()==0,case.result
    assert case.boards==expected
    assert case.current_mode=='rgb+depth'
    assert case.result['board_sn_restore_policy']['original']==dict(zip(('upper','lower'),original))
    assert case.result['board_sn_restore_policy']['restore_target']==dict(zip(('upper','lower'),expected))
    assert all(all(value for value in args) for _,command,args in case.calls if command=='set-board-sn')


def test_invalid_nonempty_board_sn_still_blocks(factory):
    case=factory('FACTORY-BOARD-SN');case.boards=('bad#sn','LOWER')
    assert case.run()==1 and case.result['outcome']=='blocked'
    assert not any(command.startswith('set-') for _,command,_ in case.calls)


@pytest.mark.parametrize('code', ['FILE-MODULE-LOG', 'FILE-ARCHIVE-LOG'])
@pytest.mark.parametrize('size', [60001, 1024 * 1024 + 1])
def test_large_logs_are_downloaded_and_validated(factory, code, size):
    case = factory(code)
    contents = random.Random(24).randbytes(size)
    archived = code == 'FILE-ARCHIVE-LOG'
    payload = gzip.compress(contents, mtime=0) if archived else contents
    remote = '/userdata/log/archived/001.log.gz' if archived else '/userdata/log/lasted.log'
    case.remote[remote] = payload
    assert len(payload) >= size
    assert case.run() == 0, case.result
    downloads = [args for _, command, args in case.calls if command == 'file-get']
    assert len(downloads) == 1 and downloads[0][0] == remote
    actual = Path(downloads[0][1])
    assert actual.read_bytes() == payload
    artifact = next(item for item in case.result['files'] if item['path'] == str(actual.relative_to(case.out)))
    assert artifact['size_bytes'] == len(payload)
    if archived:
        assert any(item['name'].endswith('complete length') and item['passed'] for item in case.result['assertions'])
        assert any(item['name'].endswith('gzip integrity') and item['passed'] for item in case.result['assertions'])


def test_file_get_rejection_preserves_actual_command_result(factory, monkeypatch):
    case = factory('FILE-MODULE-LOG')
    case.remote['/userdata/log/lasted.log'] = b'x' * 60001
    case.data['video'] = '/dev/fixture'
    original_call = case.call
    def call(step, command, *args):
        if command == 'file-get':
            return Case.call(case, step, command, *args)
        return original_call(step, command, *args)
    monkeypatch.setattr(case, 'call', call)
    calls = []
    def refused(command, **kwargs):
        calls.append(command)
        assert command[3] == 'file-get'
        Path(kwargs['env']['RMSL_XU_TRACE']).write_text('{"fixture":"firmware-rejected"}\n')
        return subprocess.CompletedProcess(command, 9, b'file-get received rejection\n', b'firmware read failed\n')
    monkeypatch.setattr('test_runtime.subprocess.run', refused)
    assert case.run() == 1
    assert case.result['outcome'] == 'error'
    assert len(calls) == 1
    step = case.result['steps'][-1]
    assert step['id'] == 'download-log-0' and step['exit_code'] == 9
    assert step['command'] == calls[0]
    assert 'received rejection' in step['stdout']
    assert 'firmware read failed' in step['stderr']
    assert 'received rejection' in (case.out / 'stdout.log').read_text()
    assert 'firmware read failed' in (case.out / 'stderr.log').read_text()
    assert 'firmware-rejected' in (case.out / 'protocol.jsonl').read_text()


def test_truncated_archive_fails_complete_length_check(factory, monkeypatch):
    case = factory('FILE-ARCHIVE-LOG')
    case.remote['/userdata/log/archived/001.log.gz'] = gzip.compress(random.Random(24).randbytes(70000), mtime=0)
    original_call = case.call
    def call(step, command, *args):
        output = original_call(step, command, *args)
        if command == 'file-get':
            target = Path(args[1])
            target.write_bytes(target.read_bytes()[:-8])
        return output
    monkeypatch.setattr(case, 'call', call)
    assert case.run() == 1 and case.result['outcome'] == 'failed'
    assert any(item['name'].endswith('complete length') and not item['passed'] for item in case.result['assertions'])


def test_archive_crc_failure_does_not_pass(factory):
    case = factory('FILE-ARCHIVE-LOG')
    payload = bytearray(gzip.compress(random.Random(24).randbytes(70000), mtime=0))
    payload[-8] ^= 1
    case.remote['/userdata/log/archived/001.log.gz'] = bytes(payload)
    assert case.run() == 1 and case.result['outcome'] == 'error'
    assert any(item['name'].endswith('complete length') and item['passed'] for item in case.result['assertions'])
    assert 'CRC' in case.result['message']


@pytest.mark.parametrize('delta', [-1000, 1000])
def test_active_log_change_during_download_does_not_require_old_listing_length(factory, monkeypatch, delta):
    case = factory('FILE-MODULE-LOG')
    case.remote['/userdata/log/lasted.log'] = b'x' * 70000
    original_call = case.call
    def call(step, command, *args):
        if command == 'file-get':
            case.remote[args[0]] = b'x' * (70000 + delta)
        return original_call(step, command, *args)
    monkeypatch.setattr(case, 'call', call)
    assert case.run() == 0, case.result
    assert (case.out / 'artifacts/lasted.log').stat().st_size == 70000 + delta


def test_absent_archive_still_blocks_without_downloading(factory):
    case = factory('FILE-ARCHIVE-LOG')
    del case.remote['/userdata/log/archived/001.log.gz']
    assert case.run() == 1 and case.result['outcome'] == 'blocked'
    assert case.result['message'] == 'No archived logs available'
    assert not any(command == 'file-get' for _, command, _ in case.calls)


def test_corrupt_archive_does_not_pass(factory):
    case=factory('FILE-ARCHIVE-LOG');case.remote['/userdata/log/archived/001.log.gz']=b'not gzip'
    assert case.run()==1 and case.result['outcome']=='error'


def test_unsafe_listing_rejected():
    with pytest.raises(ValueError): listing('../escape\t12')
    assert not restorable_board(('', 'valid'))
