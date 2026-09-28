"""Host-owned switching tests; firmware controls are invoked only through uvc_host_ctl."""
import configparser
import json
from pathlib import Path
import re
import subprocess
import time
from test_runtime import Case
from worker import atomic, digest, timestamp
from frame_images import png
from rv1126b import discover

ROOT = Path(__file__).resolve().parent
SPECS = {
 'SWITCH-ROTATE': ('旋转开关', 'system', 'rotate', 'set-rotate', [0, 1]),
 'SWITCH-RGB-BTNR': ('RGB BTNR', 'rgb', 'btnr', 'set-rgb-btnr', [0, 1]),
 'SWITCH-IR-BTNR': ('IR BTNR', 'ir', 'btnr', 'set-ir-btnr', [0, 1]),
 'SWITCH-PROJECTOR': ('投影仪开关', 'projector', 'enable', 'set-projector', [0, 1]),
 'SWITCH-ALGO-ASYNC': ('算法异步开关', 'algo', 'async', 'set-algo-async', [0, 1]),
 'SWITCH-ALGO-ITER': ('算法迭代次数', 'algo', 'iter', 'set-algo-iter', [1, 2, 3]),
 'SWITCH-ALGO-FPS': ('算法帧率', 'algo', 'fps', 'set-algo-fps', [10, 30]),
 'SWITCH-ALGO-MODEL': ('算法模型', 'algo', 'model_type', 'set-algo-model-type', [0, 1, 2]),
 'SWITCH-ALGO-DENOISE': ('算法去噪', 'algo', 'denoise', 'set-algo-denoise', [0, 25, 50]),
 'SWITCH-ALGO-FULLSIZE': ('算法全尺寸输出', 'algo', 'full_size', 'set-algo-full-size', [0, 1]),
 'SWITCH-ALGO-UNCERTAIN': ('算法不确定性阈值', 'algo', 'uncertain', 'set-algo-uncertain', [0, 50, 100]),
 'SWITCH-ALGO-DISCONTINUE': ('算法不连续阈值', 'algo', 'discontinue', 'set-algo-discontinue', [0, 25, 50]),
 'SWITCH-ALGO-UNCERTAIN-DELTA': ('算法不确定性差值', 'algo', 'uncertain_delta', 'set-algo-uncertain-delta', [0, 50, 100]),
 'SWITCH-DEVICE-MODE': ('设备模式切换', 'system', 'usb_mode', 'set-device-mode', ['rgb+depth', 'rgb+ir', 'rgb+depth+ir']),
 'SWITCH-RESOLUTION': ('分辨率切换', 'algo', 'resolution', 'set-algo-mode', ['640x352', '512x448']),
}

def parse_params(text):
    p = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=('#',))
    p.read_string('[metadata]\n' + text)
    return {s: dict(p[s]) for s in p.sections()}

class Capture:
    def __init__(self, case, label, rgb_size=None):
        self.case=case; self.items=[]; self.label=label
        try:
            for node in sorted(Path('/sys/class/video4linux').glob('video*')):
                if (node/'index').read_text().strip() != '0': continue
                usb=next((p for p in (node/'device').resolve().parents if (p/'idVendor').exists()), None)
                if not usb or usb.name != case.data['device']['usb_port']: continue
                name=(node/'name').read_text().upper()
                typ=next((i for text,i in [('RGB',1),('DEPTH',2),('IR',3)] if text in name),None)
                if typ is None: continue
                video='/dev/'+node.name
                listing=subprocess.check_output(['v4l2-ctl','-d',video,'--list-formats-ext'],text=True,timeout=10)
                sizes=[tuple(map(int,m)) for m in re.findall(r'Size: Discrete (\d+)x(\d+)',listing)]
                if not sizes: raise RuntimeError('No discrete format for '+video)
                size=rgb_size if typ==1 and rgb_size else sizes[0]
                if size not in sizes: raise RuntimeError('Requested resolution not advertised: '+str(size))
                w,h=size;fmt='YUYV' if typ==2 else 'NV12'
                folder=case.out/'artifacts'/label/str(typ);folder.mkdir(parents=True,exist_ok=True)
                log=(folder/'capture.log').open('wb')
                proc=subprocess.Popen([str(ROOT/'host-capture'),video,str(w),str(h),fmt,'10',str(typ),str(folder),'180'],stdout=log,stderr=log)
                self.items.append((proc,log,folder,typ))
            if not self.items: raise RuntimeError('No streams at locked USB port')
        except BaseException:
            self.close();raise
    def snapshot(self, generation=0):
        for _,_,folder,_ in self.items:
            tmp=folder/'request.tmp';tmp.write_text(str(generation));tmp.replace(folder/'request')
        deadline=time.monotonic()+50
        for proc,_,folder,typ in self.items:
            meta_path=folder/('g'+str(generation)+'.json')
            while not meta_path.exists():
                if proc.poll() is not None: raise RuntimeError('Capture exited: '+(folder/'capture.log').read_text(errors='replace')[-600:])
                if time.monotonic()>deadline: raise RuntimeError('Stable frames not received: '+str(folder.relative_to(self.case.out)))
                time.sleep(.2)
            meta=json.loads(meta_path.read_text());raw=meta_path.with_suffix('.raw');preview=meta_path.with_suffix('.png')
            png(raw.read_bytes(),meta,preview);atomic(meta_path,meta)
            for path,role in [(raw,'raw_frame'),(preview,'image'),(meta_path,'frame_metadata')]:
                self.case.result['files'].append({'path':str(path.relative_to(self.case.out)),'role':role,'label':self.label,'stream':{1:'RGB',2:'Depth',3:'IR'}[typ],'size_bytes':path.stat().st_size,'sha256':digest(path)})
            self.case.check(self.label+' '+str(typ)+' stable frame', '>=12 sequential complete frames',meta,meta['consecutive_valid_frames']>=12)
    def close(self):
        for proc,log,_,_ in self.items:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=8)
                except subprocess.TimeoutExpired: proc.kill();proc.wait()
            log.close()

class SwitchCase(Case):
    def params(self,step): return parse_params(self.call(step,'get-param'))
    def bind(self,wait=90):
        deadline=time.monotonic()+wait
        while time.monotonic()<deadline:
            ds=[d for d in discover() if d['serial']==self.data['device']['serial'] and d['usb_port']==self.data['device']['usb_port']]
            if len(ds)==1:
                if ds[0]['version'] != self.data['device']['version']: raise RuntimeError('Firmware changed during test')
                self.data['video']=ds[0]['video'];return
            time.sleep(2)
        raise RuntimeError('Locked device did not return')
    def value(self,p,section,key):
        return p['algo']['width']+'x'+p['algo']['height'] if key=='resolution' else p[section][key]
    def write(self,step,command,value):
        self.call(step,command,value)
        if command in ('set-device-mode','set-algo-mode'):
            time.sleep(8);self.bind()
        else:time.sleep(.4)
    def run(self):
        original=None;capture=None;section=key=command=None;outer_mode=None
        try:
            self.bind()
            self.check('Tool SHA-256',self.data['tool_sha256'],digest(Path(self.data['tool'])),digest(Path(self.data['tool']))==self.data['tool_sha256'])
            _,section,key,command,values=SPECS[self.code]
            initial=self.params('save-parameters');atomic(self.out/'original-parameters.json',initial)
            original=self.value(initial,section,key)
            self.result.update(scope='configuration_readback_and_host_stream_capture',image_evidence={'state':'host_capture'},recovery={'state':'pending'})
            # IR BTNR needs an IR stream. Keep this setup separate and restore the original mode.
            if self.code=='SWITCH-IR-BTNR' and 'ir' not in initial['system']['usb_mode']:
                outer_mode=initial['system']['usb_mode'];self.write('enable-ir-mode','set-device-mode','rgb+ir')
            if key=='btnr':
                capture=Capture(self,'btnr-baseline');capture.snapshot(0)
                live=self.call('initial-live-btnr','get-'+section+'-btnr')
                match=re.fullmatch(section+r'-btnr: ([01])',live)
                self.result['assertions'].append({'name':'Persisted BTNR applied on stream start','expected':original,'actual':live,'passed':bool(match and match[1]==original)})
                self.save()
                for generation,target in enumerate((1-int(original),int(original)),1):
                    self.write('live-set-'+str(target),command,target)
                    actual=self.value(self.params('live-readback-'+str(target)),section,key)
                    self.check('BTNR config '+str(target),str(target),actual,actual==str(target))
                    deadline=time.monotonic()+3
                    while True:
                        live=self.call('live-status-'+str(target),'get-'+section+'-btnr')
                        match=re.fullmatch(section+r'-btnr: ([01])',live)
                        if match and match[1]==str(target) or time.monotonic()>deadline:break
                        time.sleep(.2)
                    capture.label='btnr-'+str(target);capture.snapshot(generation)
                    self.result['assertions'].append({'name':'Live BTNR '+str(target),'expected':str(target),'actual':live,'passed':bool(match and match[1]==str(target))});self.save()
                values=[]
            if self.code=='SWITCH-PROJECTOR':
                # This value is a command mailbox, not a physical-state getter.
                # A fresh stream initializes the projector; stream close turns it off.
                original=None
                if initial['projector']['enable']!='2':raise RuntimeError('Projector command already pending')
                capture=Capture(self,'projector');capture.snapshot(0)
                for generation,target in enumerate(values,1):
                    self.write('projector-'+str(target),command,target)
                    deadline=time.monotonic()+5
                    while self.params('mailbox-'+str(target))['projector']['enable']!='2':
                        if time.monotonic()>deadline:raise RuntimeError('Projector command not consumed')
                        time.sleep(.2)
                    capture.label='projector-'+('on' if target else 'off')
                    capture.snapshot(generation)
                capture.close();capture=None
                self.check('Projector mailbox idle','2',self.params('projector-restored')['projector']['enable'],self.params('projector-final')['projector']['enable']=='2')
                self.result['recovery']={'state':'passed','detail':'Streams closed; mailbox idle. Physical on/off effect retained as images, not automatically judged.'}
                values=[]
            for index,target in enumerate(values):
                if capture: capture.close();capture=None
                self.write('set-'+str(index),command,target)
                actual=self.value(self.params('readback-'+str(index)),section,key)
                self.check('Readback '+str(target),str(target),actual,actual==str(target))
                capture=Capture(self,'state-'+str(index)+'-'+str(target).replace('+','_'))
                capture.snapshot()
                if key=='btnr':
                    live=self.call('live-btnr-'+str(index),'get-'+section+'-btnr')
                    self.check('Live BTNR '+str(target),str(target),live,bool(re.search(r'\b'+str(target)+r'\b',live)))
                capture.close();capture=None
            if self.code=='SWITCH-RESOLUTION':
                # Traverse RGB formats separately while preserving the algorithm mode.
                self.write('restore-algo-for-rgb',command,original)
                listing=subprocess.check_output(['v4l2-ctl','-d',self.data['video'],'--list-formats-ext'],text=True,timeout=10)
                for w,h in dict.fromkeys(re.findall(r'Size: Discrete (\d+)x(\d+)',listing)):
                    capture=Capture(self,'rgb-'+w+'x'+h,(int(w),int(h)));capture.snapshot();capture.close();capture=None
            self.result['outcome']='passed' if all(a['passed'] for a in self.result['assertions']) else 'failed'
            if self.result['outcome']=='failed':self.result['message']='; '.join(a['name'] for a in self.result['assertions'] if not a['passed'])
        except AssertionError as exc:self.result.update(outcome='failed',message=str(exc))
        except Exception as exc:self.result.update(outcome='error',message=str(exc))
        finally:
            if capture and key!='btnr':capture.close();capture=None
            if original is not None:
                try:
                    self.bind();self.write('restore-original',command,original)
                    actual=self.value(self.params('verify-restored'),section,key)
                    if actual!=original:raise RuntimeError('Original value not restored: '+actual+' != '+original)
                    if capture:capture.close();capture=None
                    if outer_mode:
                        self.write('restore-device-mode','set-device-mode',outer_mode)
                        if self.params('verify-device-mode')['system']['usb_mode']!=outer_mode:raise RuntimeError('Original device mode not restored')
                    self.result['recovery']={'state':'passed','expected':original,'actual':actual}
                except Exception as exc:self.result.update(outcome='error',recovery={'state':'failed','error':str(exc)})
            if capture:capture.close()
            self.result.update(state='finished',completed_at=timestamp());self.save()
        return 0 if self.result['outcome']=='passed' else 1
