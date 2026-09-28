import hashlib
import json
from pathlib import Path
import sys
import pytest

BENCH=Path(__file__).resolve().parents[2]/'bench'
sys.path.insert(0,str(BENCH))
from frame_images import png
from switch_runtime import parse_params,SwitchCase
from develop_hub.developer_tests import passed
from develop_hub.evidence_store import artifact

def test_quick_pass_requires_identity_and_all_case_assertions():
    r={'execution_boundary':'current_firmware','identity_verified':True,'state':'finished','phase':'tests_complete','outcome':'passed','cases':[{'id':'a','state':'finished','outcome':'passed','assertions_total':2,'assertions_passed':2,'recovery':{'state':'passed'}}]}
    assert passed(r,['a'])
    assert not passed({**r,'identity_verified':False},['a'])
    assert not passed({**r,'flash_exit_code':0},['a'])
    assert not passed(r,['b'])
    r['cases'][0]['assertions_passed']=1
    assert not passed(r,['a'])

def test_quick_mode_rejects_flash_before_touching_device():
    import quick_tests
    with pytest.raises(RuntimeError,match='cannot include flash'):
        quick_tests.execute(None,{'flash':{'enabled':True}})

def test_inline_param_comments_and_zero_defaults():
    p=parse_params('version=1\n[algo]\niter=0 # lib default\nwidth=640\nheight=352\n')
    assert p['algo']['iter']=='0'

def test_png_preserves_raw_and_rejects_truncation(tmp_path):
    raw=bytes([128])*24
    meta={'width':4,'height':4,'type':1,'header_size':0}
    png(raw,meta,tmp_path/'rgb.png')
    assert raw==bytes([128])*24
    assert (tmp_path/'rgb.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    with pytest.raises(ValueError,match='Truncated'):png(b'',meta,tmp_path/'bad.png')

def test_artifact_allowlist_and_integrity(tmp_path):
    attempt=tmp_path/'cases/SWITCH-ROTATE/attempt-001';attempt.mkdir(parents=True)
    f=attempt/'a.png';f.write_bytes(b'png')
    (attempt/'result.json').write_text(json.dumps({'files':[{'path':'a.png','role':'image','size_bytes':3,'sha256':hashlib.sha256(b'png').hexdigest()}]}))
    assert artifact(tmp_path,'SWITCH-ROTATE','a.png')==f
    with pytest.raises(ValueError):artifact(tmp_path,'SWITCH-ROTATE','../../outside')
    with pytest.raises(ValueError):artifact(tmp_path,'SWITCH-ROTATE','unknown.png')
    f.write_bytes(b'bad')
    with pytest.raises(ValueError,match='digest'):artifact(tmp_path,'SWITCH-ROTATE','a.png')

def test_capture_failure_still_restores_parameter(monkeypatch,tmp_path):
    import switch_runtime as s
    tool=tmp_path/'tool';tool.write_bytes(b'tool')
    c=SwitchCase('SWITCH-ROTATE',{'tool':str(tool),'tool_sha256':hashlib.sha256(b'tool').hexdigest()},tmp_path/'out')
    state={'rotate':'0'};writes=[]
    monkeypatch.setattr(c,'bind',lambda:None)
    monkeypatch.setattr(c,'params',lambda step:{'system':{'rotate':state['rotate'],'usb_mode':'rgb+depth'}})
    def write(step,command,value):writes.append((step,str(value)));state['rotate']=str(value)
    monkeypatch.setattr(c,'write',write)
    def fail(*args,**kwargs):raise RuntimeError('No stable frames')
    monkeypatch.setattr(s,'Capture',fail)
    assert c.run()==1
    assert c.result['outcome']=='error'
    assert writes[-1]==('restore-original','0')
    assert c.result['recovery']['state']=='passed'
