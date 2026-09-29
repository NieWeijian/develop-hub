import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'bench'))
import stream_quality as sq

def test_time_sync_precedes_actual_stream_launch_and_records_command(tmp_path,monkeypatch):
    case=sq.StreamCase('STREAM-RGB-DEPTH',{},tmp_path/'result');events=[]
    def call(*args):
        events.append('sync');assert args[:2]==('time-sync','time-sync')
        case.result['steps'].append({'id':'time-sync','exit_code':0})
    monkeypatch.setattr(case,'call',call)
    class Process:
        pid=123
    def launch(args,**kwargs):
        assert events==['sync'];events.append('launch');return Process()
    monkeypatch.setattr(sq.subprocess,'Popen',launch)
    with (tmp_path/'capture.log').open('wb') as log:
        args=['uvc_host_stream-metrics','--device','/dev/video71']
        proc,step,start,end=case.sync_and_start_capture(args,log)
    case.save();saved=json.loads((case.out/'result.json').read_text())
    assert [x['id'] for x in saved['steps']]==['time-sync','start-stream']
    assert step['command']==args and step['pid']==123 and step['started_at']
    assert step['state']=='running' and step['exit_code'] is None and end>=start

def test_failed_sync_never_opens_stream(tmp_path,monkeypatch):
    case=sq.StreamCase('STREAM-RGB-DEPTH',{},tmp_path/'result')
    def fail(*args):raise RuntimeError('sync failed')
    monkeypatch.setattr(case,'call',fail)
    monkeypatch.setattr(sq.subprocess,'Popen',lambda *a,**k:pytest.fail('must not open stream'))
    log=(tmp_path/'capture.log').open('wb')
    with pytest.raises(RuntimeError,match='sync failed'):case.sync_and_start_capture(['stream'],log)
    assert log.closed

def test_launch_failure_preserves_attempted_command(tmp_path,monkeypatch):
    case=sq.StreamCase('STREAM-RGB-DEPTH',{},tmp_path/'result')
    monkeypatch.setattr(case,'call',lambda *args:None)
    def fail(*args,**kwargs):raise OSError('cannot execute')
    monkeypatch.setattr(sq.subprocess,'Popen',fail)
    log=(tmp_path/'capture.log').open('wb')
    with pytest.raises(OSError):case.sync_and_start_capture(['missing-stream'],log)
    step=json.loads((case.out/'result.json').read_text())['steps'][-1]
    assert step['state']=='start_failed' and step['command']==['missing-stream']
    assert step['exit_code'] is None and step['completed_at'] and log.closed
