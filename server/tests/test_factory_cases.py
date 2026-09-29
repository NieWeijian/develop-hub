import gzip
import re
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'bench'))
from factory_runtime import FactoryCase, CALIB, listing, restorable_board
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


def test_empty_board_sn_blocks_before_write(factory):
    case=factory('FACTORY-BOARD-SN');case.boards=('','')
    assert case.run()==1 and case.result['outcome']=='blocked'
    assert not any(command.startswith('set-') for _,command,_ in case.calls)


def test_archive_over_limit_is_blocked(factory):
    case=factory('FILE-ARCHIVE-LOG');case.remote['/userdata/log/archived/001.log.gz']=b'x'*60001
    assert case.run()==1 and case.result['outcome']=='blocked'
    assert not any(command=='file-get' for _,command,_ in case.calls)


def test_corrupt_archive_does_not_pass(factory):
    case=factory('FILE-ARCHIVE-LOG');case.remote['/userdata/log/archived/001.log.gz']=b'not gzip'
    assert case.run()==1 and case.result['outcome']=='error'


def test_unsafe_listing_rejected():
    with pytest.raises(ValueError): listing('../escape\t12')
    assert not restorable_board(('', 'valid'))
