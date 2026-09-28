import importlib.util
from pathlib import Path
import sys
import json
import pytest

BENCH=Path(__file__).resolve().parents[2]/'bench'
sys.path.insert(0,str(BENCH))
from test_runtime import Case
from worker import digest

@pytest.fixture
def make_case(tmp_path,monkeypatch):
    monkeypatch.setattr('test_runtime.time.sleep',lambda _:None)
    tool=tmp_path/'tool';tool.write_bytes(b'tool')
    def make(code,responses):
        c=Case(code,{'tool':str(tool),'tool_sha256':digest(tool),'video':'/dev/video71','commit':'abcdef012345','parameters':{}},tmp_path/code)
        def call(step,*args):
            (c.out/'protocol.jsonl').write_text('{"test_fixture":true}\n')
            value=responses[step]
            if isinstance(value,Exception): raise value
            return value
        c.call=call
        return c
    return make

def test_version_pass(make_case):
    c=make_case('BASIC-VERSION',{'read-version':'firmware abcdef0'})
    assert c.run()==0
    assert c.result['outcome']=='passed'

def test_wrong_version_fails(make_case):
    c=make_case('BASIC-VERSION',{'read-version':'firmware badbad0'})
    assert c.run()==1
    assert c.result['outcome']=='failed'

def test_tool_error_is_not_pass(make_case):
    c=make_case('BASIC-VERSION',{'read-version':RuntimeError('USB gone')})
    assert c.run()==1
    assert c.result['outcome']=='error'

def test_restore_after_failed_readback(make_case):
    c=make_case('BASIC-FOV-WRITE',{'save-fov':'100','supported-fov':'80 100','write-fov':'','verify-fov':'100','restore-fov':'','verify-restore':'100'})
    assert c.run()==1
    assert c.result['recovery']['state']=='passed'
    assert c.result['outcome']=='failed'

def test_restore_failure_overrides_success(make_case):
    c=make_case('BASIC-FOV-WRITE',{'save-fov':'100','supported-fov':'80 100','write-fov':'','verify-fov':'80','restore-fov':RuntimeError('USB lost')})
    assert c.run()==1
    assert c.result['recovery']['state']=='failed'
    assert c.result['outcome']=='error'

def test_no_trace_cannot_pass(make_case):
    c=make_case('BASIC-VERSION',{})
    c.call=lambda *args:'firmware abcdef0'
    assert c.run()==1
    assert c.result['outcome']=='failed'


def test_flash_only_report_cannot_pass_tests():
    from develop_hub.basic_cases import report_passed
    assert not report_passed({'state':'finished','phase':'flash_complete','outcome':'passed','flash_exit_code':0},['a'])

def test_missing_case_or_failed_assertion_cannot_pass():
    from develop_hub.basic_cases import report_passed
    report={'state':'finished','phase':'tests_complete','outcome':'passed','flash_exit_code':0,'boot_verified':True,'cases':[{'id':'a','state':'finished','outcome':'passed','assertions_total':2,'assertions_passed':2}]}
    assert report_passed(report,['a'])
    assert not report_passed(report,['a','b'])
    report['cases'][0]['assertions_passed']=1
    assert not report_passed(report,['a'])
