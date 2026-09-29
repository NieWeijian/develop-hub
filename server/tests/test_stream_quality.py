import csv
import json
import math
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'bench'))
from stream_quality import validate, measure, Moments


def data(folder, rows, duration=2):
    (folder/'summary.json').write_text(json.dumps({'completed':True,'stalled':False,'duration_seconds':duration,'start_us':10000000}))
    with (folder/'frames-0.csv').open('w') as f:
        writer=csv.writer(f);writer.writerow(['host_us','v4l2_sequence','source_sequence','device_us','valid','driver_error']);writer.writerows(rows)


def test_default_resolution_duration_and_warmup():
    p=validate({})
    assert (p['rgb_width'],p['rgb_height'],p['depth_width'],p['depth_height'])==(640,540,640,352)
    assert p['duration_seconds']==18000 and p['warmup_seconds']==10
    assert p.get('thresholds',{})=={}


@pytest.mark.parametrize('params',[{'duration_seconds':0},{'duration_seconds':True},{'duration_seconds':86401},{'warmup_seconds':61},{'thresholds':{'ir':{}}},{'thresholds':{'rgb':{'min_fps':float('nan')}}}])
def test_invalid_parameters(params):
    with pytest.raises(ValueError):validate(params)


def test_full_window_fps_excludes_startup_and_source_gaps_not_loss(tmp_path):
    rows=[[10000000+i*100000,i,i*3,10000000+i*100000-20000,1,0] for i in range(20)]
    data(tmp_path,rows)
    m=measure(tmp_path)
    assert m['fps']==10 and m['transport_drop_percent']==0
    assert m['source_sequence_gaps_diagnostic_only']==38
    assert m['arrival_interval_ms']['stddev']==0
    assert m['estimated_latency_ms']['mean']==20
    assert m['fps_windows']==[{'start_seconds':0,'fps':10}]


def test_transport_gap_counter_wrap_and_reset(tmp_path):
    seqs=[0xfffffffe,0xffffffff,0,2,0]
    rows=[[10000000+i*100000,s,i,10000000+i*100000-20000,1,0] for i,s in enumerate(seqs)]
    data(tmp_path,rows)
    m=measure(tmp_path)
    assert m['v4l2_missing_frames']==1
    assert m['v4l2_resets_or_duplicates']==1
    assert m['transport_drop_percent']==pytest.approx(100/6)


def test_last_partial_window_uses_its_actual_duration(tmp_path):
    data(tmp_path,[[10000000+i*100000,i,i,10000000+i*100000-10000,1,0] for i in range(120)],12)
    assert measure(tmp_path)['fps_windows']==[{'start_seconds':0,'fps':10},{'start_seconds':10,'fps':10}]


def test_invalid_frames_and_bad_timestamps_are_visible(tmp_path):
    data(tmp_path,[[10000000,0,0,9990000,1,0],[10100000,1,1,9990000,1,1],[10200000,2,2,0,0,0]])
    m=measure(tmp_path)
    assert m['invalid_frames']==1 and m['timestamp_errors']==1 and m['driver_error_frames']==1


def test_online_jitter():
    m=Moments()
    for v in [90,100,110]:m.add(v)
    assert m.result()['stddev']==pytest.approx(math.sqrt(200/3))
