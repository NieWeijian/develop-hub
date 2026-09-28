import hashlib
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'bench'))
import switch_runtime as s

def test_retired_cases_removed_and_numeric_matrix():
    for code in ('SWITCH-ALGO-ASYNC','SWITCH-ALGO-DENOISE','SWITCH-ALGO-ITER','SWITCH-IR-BTNR'):
        assert code not in s.SPECS
    assert s.SPECS['SWITCH-ALGO-FPS'][-1]==[10]
    assert s.SPECS['SWITCH-ALGO-DISCONTINUE'][-1]==[0,50]
    assert s.SPECS['SWITCH-ALGO-UNCERTAIN'][-1]==[0,100]
    assert s.SPECS['SWITCH-ALGO-UNCERTAIN-DELTA'][-1]==[0,100]

def test_rgb_btnr_uses_live_default_and_switches_after_open(monkeypatch,tmp_path):
    tool=tmp_path/'tool';tool.write_bytes(b'tool')
    c=s.SwitchCase('SWITCH-RGB-BTNR',{'tool':str(tool),'tool_sha256':hashlib.sha256(b'tool').hexdigest()},tmp_path/'out')
    state={'streaming':False,'live':'1'};commands=[];shots=[]
    class Capture:
        def __init__(self,*args):state['streaming']=True;self.label=''
        def snapshot(self,generation=0):shots.append((generation,state['live']))
        def close(self):state['streaming']=False
    monkeypatch.setattr(s,'Capture',Capture)
    monkeypatch.setattr(c,'bind',lambda:None)
    monkeypatch.setattr(c,'params',lambda step:{'system':{'usb_mode':'rgb+depth'},'rgb':{'btnr':'0'}})
    def call(step,command,*args):
        assert state['streaming']
        assert command=='get-rgb-btnr'
        return 'rgb-btnr: '+state['live']
    def write(step,command,value):
        assert state['streaming']
        assert command=='set-rgb-btnr'
        commands.append(str(value));state['live']=str(value)
    monkeypatch.setattr(c,'call',call);monkeypatch.setattr(c,'write',write)
    assert c.run()==0
    assert commands==['1','0','1']
    assert shots==[(1,'1'),(2,'0')]
    assert not state['streaming']
    assert c.result['recovery']=={'state':'passed','expected':'1','actual':'1'}

def test_retired_definitions_still_available_for_history():
    from develop_hub.basic_cases import CODES,ACTIVE_CODES,RETIRED_CODES,definition
    for code in RETIRED_CODES:
        assert code in CODES and code not in ACTIVE_CODES
        assert definition(code)['deprecated'] is True
