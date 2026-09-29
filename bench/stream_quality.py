"""RGB + Depth quality measurements; explicit metric definitions, optional limits."""
import csv
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from factory_runtime import FactoryCase, Blocked
from switch_runtime import parse_params
from worker import atomic, digest, timestamp
from frame_images import png

ROOT=Path(__file__).resolve().parent
DEFAULTS={'duration_seconds':18000,'rgb_width':640,'rgb_height':540,'depth_width':640,'depth_height':352,'fps':10,'stall_timeout_seconds':5,'warmup_seconds':10}
LIMITS={'min_fps','max_transport_drop_percent','max_latency_mean_ms','max_latency_max_ms','max_interval_jitter_ms','max_latency_jitter_ms'}


def validate(parameters):
    if set(parameters)-set(DEFAULTS)-{'thresholds'}:raise ValueError('Unsupported stream parameter')
    result={**DEFAULTS,**parameters}
    for key,default in DEFAULTS.items():
        value=result[key];maximum=86400 if key=='duration_seconds' else 300 if key=='stall_timeout_seconds' else 60 if key in ('fps','warmup_seconds') else 4096
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=maximum:raise ValueError('Invalid '+key)
    thresholds=result.get('thresholds',{})
    if not isinstance(thresholds,dict) or set(thresholds)-{'rgb','depth'}:raise ValueError('Threshold streams must be rgb/depth')
    for stream,values in thresholds.items():
        if not isinstance(values,dict) or set(values)-LIMITS:raise ValueError('Unsupported threshold')
        if any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or v<0 for v in values.values()):raise ValueError('Thresholds must be finite nonnegative numbers')
    return result


class Moments:
    def __init__(self):self.n=0;self.mean=0.;self.m2=0.;self.low=None;self.high=None
    def add(self,value):
        self.n+=1;delta=value-self.mean;self.mean+=delta/self.n;self.m2+=delta*(value-self.mean)
        self.low=value if self.low is None else min(self.low,value);self.high=value if self.high is None else max(self.high,value)
    def result(self):return {'samples':self.n,'mean':self.mean if self.n else None,'min':self.low,'max':self.high,'stddev':math.sqrt(max(0,self.m2/self.n)) if self.n else None}


def measure(folder):
    summary=json.loads((folder/'summary.json').read_text());requested=summary['duration_seconds']
    duration=requested if summary['completed'] else max(0,min(requested,(summary.get('end_us',0)-summary['start_us'])/1000000)) if summary['start_us'] else 0
    arrival=Moments();latency=Moments();last_host=last_seq=last_source=last_device=None
    valid=invalid=driver_errors=missing=resets=source_gaps=source_regressions=timestamp_errors=0;windows={}
    for path in sorted(folder.glob('frames-*.csv'),key=lambda p:int(p.stem.split('-')[-1])):
        with path.open() as stream:
            for row in csv.DictReader(stream):
                values={k:int(v) for k,v in row.items()};seq=values['v4l2_sequence'];host=values['host_us']
                if last_seq is not None:
                    delta=(seq-last_seq)&0xffffffff
                    if 0<delta<0x80000000:missing+=delta-1
                    else:resets+=1
                last_seq=seq;driver_errors+=values['driver_error']
                if not values['valid']:invalid+=1;continue
                valid+=1;windows[int((host-summary['start_us'])/10000000)]=windows.get(int((host-summary['start_us'])/10000000),0)+1
                if last_host is not None:arrival.add((host-last_host)/1000)
                last_host=host
                source=values['source_sequence'];device=values['device_us']
                if last_source is not None:
                    if source>last_source:source_gaps+=max(0,source-last_source-1)
                    else:source_regressions+=1
                if device<=0 or (last_device is not None and device<=last_device):timestamp_errors+=1
                else:latency.add((host-device)/1000)
                last_source=source;last_device=device
    total=valid+invalid
    return {'completed':summary['completed'],'stalled':summary['stalled'],'duration_seconds':duration,'requested_duration_seconds':requested,
            'valid_frames':valid,'invalid_frames':invalid,'driver_error_frames':driver_errors,'fps':valid/duration if duration else 0,
            'v4l2_missing_frames':missing,'transport_drop_percent':100*missing/(total+missing) if total+missing else 0,
            'v4l2_resets_or_duplicates':resets,'source_sequence_gaps_diagnostic_only':source_gaps,'source_regressions_or_duplicates':source_regressions,
            'timestamp_errors':timestamp_errors,'arrival_interval_ms':arrival.result(),'estimated_latency_ms':latency.result(),
            'fps_windows':[{'start_seconds':i*10,'fps':windows.get(i,0)/min(10,duration-i*10)} for i in range(math.ceil(duration/10))]}


class StreamCase(FactoryCase):
    def sync_and_start_capture(self, args, log):
        sync_start=time.monotonic_ns()//1000
        try:
            self.call('time-sync','time-sync',sync_start)
        except BaseException:
            log.close()
            raise
        sync_end=time.monotonic_ns()//1000
        step={'id':'start-stream','command':args,'started_at':timestamp(),'exit_code':None,'state':'starting','log_path':'artifacts/uvc_host_stream.log'}
        self.result['steps'].append(step);self.save()
        try:
            proc=subprocess.Popen(args,stdout=log,stderr=log)
        except BaseException as exc:
            step.update(state='start_failed',error=str(exc),completed_at=timestamp());log.close();self.save();raise
        step.update(state='running',pid=proc.pid)
        return proc,step,sync_start,sync_end

    def run(self):
        processes=[];mode=None;synced=False;capture_step=None
        try:
            p=validate(self.data.get('parameters') or {});(self.out/'artifacts').mkdir(exist_ok=True)
            self.check('Tool SHA-256',self.data['tool_sha256'],digest(Path(self.data['tool'])),digest(Path(self.data['tool']))==self.data['tool_sha256'])
            package=self.data['tools_package'];needed=self.data['definition']['required_capabilities']
            if set(needed)-set(package['capabilities']):raise Blocked('Tools lack stream setup/time-sync capabilities')
            helper=ROOT/'uvc_host_stream-metrics';expected=self.data['definition']['support_sha256']['uvc_host_stream-metrics']
            self.check('Capture helper SHA-256',expected,digest(helper),digest(helper)==expected)
            self.bind();mode=parse_params(self.call('original-mode','get-param'))['system']['usb_mode'];self.mode('rgb+depth')
            nodes={}
            for node in Path('/sys/class/video4linux').glob('video*'):
                if (node/'index').read_text().strip()!='0':continue
                usb=next((q for q in (node/'device').resolve().parents if (q/'idVendor').exists()),None)
                if usb is None or usb.name!=self.data['device']['usb_port']:continue
                name=(node/'name').read_text().upper()
                for kind in ('rgb','depth'):
                    if kind.upper() in name:
                        if kind in nodes:raise RuntimeError('Ambiguous '+kind+' node')
                        nodes[kind]='/dev/'+node.name
            if set(nodes)!={'rgb','depth'}:raise Blocked('RGB and Depth nodes required at locked USB port')
            log=(self.out/'artifacts/uvc_host_stream.log').open('wb')
            args=[str(helper),'--device',nodes['rgb'],'--metrics-root',str(self.out/'artifacts'),'--duration',str(p['duration_seconds']),'--warmup',str(p['warmup_seconds']),'--stall',str(p['stall_timeout_seconds']),'--rgb-width',str(p['rgb_width']),'--rgb-height',str(p['rgb_height']),'--depth-width',str(p['depth_width']),'--depth-height',str(p['depth_height']),'--fps',str(p['fps'])]
            synced=True
            proc,capture_step,sync_start,sync_end=self.sync_and_start_capture(args,log)
            self.result['capture_tool']={'name':'uvc_host_stream + Hub metrics observer','command':args,'sha256':expected,'upstream_commit':package.get('upstream_commit'),'upstream_binary_sha256':package.get('files',{}).get('bin/uvc_host_stream')};self.save()
            for kind in ('rgb','depth'):
                processes.append((kind,proc,log,self.out/'artifacts'/kind))
            deadline=time.monotonic()+120
            while not all((folder/'ready').exists() for _,_,_,folder in processes):
                if any(proc.poll() is not None for _,proc,_,_ in processes):raise RuntimeError('Stream configure/start failed; check capture logs and requested formats')
                if time.monotonic()>deadline:raise RuntimeError('Streams did not become ready')
                time.sleep(.1)
            configuration={}
            for kind,_,_,_ in processes:
                text=subprocess.check_output(['v4l2-ctl','-d',nodes[kind],'--get-parm'],text=True,timeout=10,env={**os.environ,'LC_ALL':'C'})
                m=re.search(r'Frames per second:\s*[\d.]+\s*\((\d+)/(\d+)\)',text)
                actual=int(m[1])/int(m[2]) if m and int(m[2]) else None
                self.check(kind+' negotiated FPS',p['fps'],actual,actual==p['fps'])
                configuration[kind]={'node':nodes[kind],'width':p[kind+'_width'],'height':p[kind+'_height'],'fps':actual,'format':'NV12' if kind=='rgb' else 'YUYV (16-bit depth transport)'}
            begin=time.monotonic_ns()//1000+3000000
            for _,_,_,folder in processes:(folder/'begin-us').write_text(str(begin))
            self.result['measurement_contract']={'streams':configuration,'duration_seconds':p['duration_seconds'],'warmup_seconds_minimum':p['warmup_seconds'],'warmup_rule':'Time-sync before stream start; both streams: 3 consecutive full 1-second windows within requested FPS ±20%, timeout 120 seconds; then 3-second start guard. Warmup excluded from requested duration.','thresholds':p.get('thresholds',{}),'sync_call_elapsed_ms':(sync_end-sync_start)/1000,'latency_note':'Estimated frame timestamp to host dequeue latency after one-way time-sync; includes synchronization bias and long-run clock drift, not a precision bound.','drop_note':'V4L2 sequence gaps only; source gaps are diagnostics, not asserted end-to-end frame loss.'};self.save()
            end=time.monotonic()+p['duration_seconds']+30
            while any(proc.poll() is None for _,proc,_,_ in processes):
                if any(proc.poll() not in (None,0) for _,proc,_,_ in processes):raise RuntimeError('Capture stopped or stalled before requested duration')
                if time.monotonic()>end:raise RuntimeError('Capture completion timeout')
                time.sleep(1)
            all_metrics={}
            for kind,proc,_,folder in processes:
                if proc.returncode!=0:raise RuntimeError(kind+' capture failed')
                metrics=measure(folder);all_metrics[kind]=metrics
                for label,value in [('FPS',metrics['fps']),('传输序号缺口丢帧率 (%)',metrics['transport_drop_percent']),('估算延时 (ms)',metrics['estimated_latency_ms']),('到帧间隔及抖动 (ms)',metrics['arrival_interval_ms']),('延时抖动标准差 (ms)',metrics['estimated_latency_ms']['stddev'])]:
                    self.check(kind+' '+label,'实测记录；性能判定仅看单独配置的阈值项',value,True)
                self.check(kind+' completed measurement',{'duration_seconds':p['duration_seconds'],'valid_frames':'>=2','invalid_frames':0,'driver_error_frames':0,'timestamp_errors':0},{k:v for k,v in metrics.items() if k!='fps_windows'},metrics['completed'] and metrics['valid_frames']>=2 and metrics['invalid_frames']==0 and metrics['driver_error_frames']==0 and metrics['timestamp_errors']==0)
                values={'min_fps':metrics['fps'],'max_transport_drop_percent':metrics['transport_drop_percent'],'max_latency_mean_ms':metrics['estimated_latency_ms']['mean'],'max_latency_max_ms':metrics['estimated_latency_ms']['max'],'max_interval_jitter_ms':metrics['arrival_interval_ms']['stddev'],'max_latency_jitter_ms':metrics['estimated_latency_ms']['stddev']}
                for key,limit in p.get('thresholds',{}).get(kind,{}).items():
                    actual=values[key];passed=actual is not None and (actual>=limit if key=='min_fps' else actual<=limit)
                    if 'latency' in key and (metrics['estimated_latency_ms']['min'] is None or metrics['estimated_latency_ms']['min']<0):passed=False
                    self.result['assertions'].append({'name':kind+' '+key,'expected':limit,'actual':actual,'passed':passed})
            atomic(self.out/'artifacts/metrics.json',all_metrics)
            self.result['outcome']='passed' if all(a['passed'] for a in self.result['assertions']) else 'failed'
        except Blocked as exc:self.result.update(outcome='blocked',message=str(exc))
        except AssertionError as exc:self.result.update(outcome='failed',message=str(exc))
        except Exception as exc:self.result.update(outcome='error',message=str(exc))
        finally:
            for _,proc,log,_ in processes:
                if proc.poll() is None:
                    proc.terminate()
                    try:proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait()
                log.close()
            if capture_step is not None and processes:
                capture_step.update(exit_code=processes[0][1].returncode,state='finished',completed_at=timestamp())
                self.save()
            if self.result.get('outcome') in ('error','failed'):
                partial={}
                for kind,_,_,folder in processes:
                    if (folder/'summary.json').exists():
                        try:partial[kind]=measure(folder)
                        except Exception as exc:partial[kind]={'measurement_error':str(exc)}
                if partial:atomic(self.out/'artifacts/partial-metrics.json',partial)
            try:
                if mode is not None:
                    self.bind()
                    if synced:self.reboot('reset-test-clock-sync')
                    self.mode(mode)
                self.result['recovery']={'state':'passed','detail':'Capture closed; original mode restored; reboot clears test time-sync offset' if synced else 'Capture closed; mode restored'}
            except Exception as exc:self.result.update(outcome='error',recovery={'state':'failed','error':str(exc)})
            try:
                for kind in ('rgb','depth'):
                    folder=self.out/'artifacts'/kind
                    for phase in ('start','end'):
                        raw=folder/(phase+'.raw');meta_path=folder/(phase+'.json')
                        if raw.exists() and meta_path.exists():
                            meta=json.loads(meta_path.read_text());png(raw.read_bytes(),meta,folder/(phase+'.png'));atomic(meta_path,meta)
                if self.result.get('outcome')=='passed':
                    expected=[self.out/'artifacts'/kind/(phase+'.png') for kind in ('rgb','depth') for phase in ('start','end')]
                    self.check('RGB / Depth 图片留存','正式开始、结束前各两路图片',sum(path.exists() for path in expected),all(path.exists() for path in expected))
            except Exception as exc:self.result.update(outcome='error',message='结果图片保存失败: '+str(exc))
            for path in (self.out/'artifacts').rglob('*'):
                if not path.is_file():continue
                is_frame=path.parent.name in ('rgb','depth') and path.stem in ('start','end')
                role=({'png':'image','raw':'raw_frame','json':'frame_metadata'}.get(path.suffix[1:],'measurement') if is_frame else 'measurement')
                self.artifact(path,role)
                if is_frame:self.result['files'][-1].update(stream='RGB' if path.parent.name=='rgb' else 'Depth',label='正式统计开始' if path.stem=='start' else '结束前最后一秒采样')
            self.result.update(state='finished',completed_at=timestamp());self.save()
        return 0 if self.result['outcome']=='passed' else 1


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True);parser.add_argument('--output-dir',required=True);args=parser.parse_args()
    raise SystemExit(StreamCase('STREAM-RGB-DEPTH',json.loads(Path(args.input).read_text()),Path(args.output_dir)).run())
