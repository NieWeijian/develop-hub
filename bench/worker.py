#!/usr/bin/env python3
"""Durable file protocol and upgrade_tool adapter. Never execute JSON shell commands."""
import datetime
import fcntl
import hashlib
import json
import os
import pty
import errno
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def atomic(path, value):
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)


class Job:
    def __init__(self, path):
        self.path=path
        self.result={'schema_version':1,'round_id':path.name,'revision':0,'state':'running','outcome':None,'phase':'validating','started_at':timestamp(),'log_tail':'','cases':[]}

    def save(self, phase=None, **values):
        changed=phase is not None and phase!=self.result.get('phase')
        self.result.update(values)
        if phase: self.result['phase']=phase
        self.result.update(updated_at=timestamp(),heartbeat_at=timestamp(),revision=self.result['revision']+1)
        atomic(self.path/'result.json',self.result)
        if changed or values.get('state'):
            with (self.path/'events.jsonl').open('a') as f:
                f.write(json.dumps({k:self.result.get(k) for k in ('revision','updated_at','phase','state','outcome','message')})+'\n')

    def run(self, args, name, timeout=60):
        log=self.path/'logs'/name
        master,slave=pty.openpty()
        proc=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=slave,stderr=slave,start_new_session=True)
        os.close(slave)
        self.result['log_tail']=''
        self.result['command']=args
        output=''; start=time.monotonic(); last=0
        selector=selectors.DefaultSelector(); selector.register(master,selectors.EVENT_READ)
        try:
            with log.open('ab',buffering=0) as f:
                while True:
                    if time.monotonic()-start>timeout:
                        os.killpg(proc.pid,signal.SIGKILL); proc.wait(); raise RuntimeError(name+' timed out')
                    for key,_ in selector.select(0.25):
                        try: chunk=os.read(key.fd,4096)
                        except OSError as exc:
                            if exc.errno!=errno.EIO: raise
                            chunk=b''
                        if chunk:
                            f.write(chunk); output=(output+chunk.decode(errors='replace'))[-24000:]
                            self.result['log_tail']=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output).replace('\r','\n')[-4000:]
                        else: selector.unregister(key.fileobj)
                    if time.monotonic()-last>1:
                        self.save(); last=time.monotonic()
                    if proc.poll() is not None and not selector.get_map(): break
            self.save()
            return proc.returncode,output
        finally:
            selector.close()
            if proc.poll() is None: os.killpg(proc.pid,signal.SIGKILL); proc.wait()
            os.close(master)

    @staticmethod
    def usb_location(device):
        device=device.resolve()
        for parent in device.parents:
            if re.fullmatch(r'usb\d+',parent.name):
                return str(parent.parent)+'/'+device.name.split('-',1)[-1]
        return ''

    def run_tests(self, config, location):
        root=Path(__file__).resolve().parent
        tool_package=next((item.get('adapter',{}).get('tools_package') for item in config['test_cases'] if item.get('adapter',{}).get('tools_package')),None) or json.loads((root/'tools.lock.json').read_text())
        tool=root/tool_package['binary_path']
        if digest(tool)!=tool_package['binary_sha256']:raise RuntimeError('Frozen tool binary checksum mismatch')
        self.save('waiting_boot',message='Waiting for matching USB port and firmware version',flash_exit_code=0,usb_location=location)
        deadline=time.monotonic()+180;video=None;version=''
        while time.monotonic()<deadline:
            for node in sorted(Path('/sys/class/video4linux').glob('video*')):
                if 'RGB' not in (node/'name').read_text(): continue
                if (node/'index').exists() and (node/'index').read_text().strip() != '0': continue
                usb=next((d for d in (node/'device').resolve().parents if (d/'idVendor').exists()),None)
                if not usb or self.usb_location(usb)!=location: continue
                candidate='/dev/'+node.name
                rc,text=self.run([str(tool),'--device',candidate,'get-version'],'boot-version.log',30)
                if rc == 0: version = text.strip()
                identity_ok = rc == 0 and config['source']['commit'][:7] in text
                if identity_ok and config.get('flash',{}).get('target_chip') == 'RV1126B':
                    identity_rc, cpu = self.run([str(tool),'--device',candidate,'get-cpu-serial'],'boot-cpu.log',30)
                    identity_ok = identity_rc == 0 and cpu.strip() == config['flash']['device_serial']
                    slot = re.search(r'(?:^|\s)_?([abAB])\s*$', text.strip())
                    identity_ok = identity_ok and bool(slot) and slot[1].lower() == self.result.get('target_slot')
                if identity_ok:
                    video=candidate;version=text.strip();break
            if video: break
            self.save(message='Waiting for matching USB port and firmware version', observed_version=version)
            time.sleep(2)
        if not video: raise RuntimeError('Boot identity/version not verified; tests blocked')
        self.save('test_cases',message='Firmware version verified; executing basic cases',boot_verified=True,boot_version=version,usb_location=location,video=video)
        all_pass=True
        for index,item in enumerate(config['test_cases']):
            adapter=item.get('adapter') or {};code=adapter.get('id','')
            from switch_runtime import SPECS
            if code not in ('BASIC-VERSION','BASIC-FOV-READ','BASIC-FOV-WRITE','BASIC-FILE-ROUNDTRIP','SWITCH-ROTATE-CONTROL') and code not in SPECS and code not in json.loads((root/'info-codes.json').read_text()) and code not in json.loads((root/'factory-codes.json').read_text()): raise RuntimeError('Unregistered test adapter')
            for name,expected in adapter.get('support_sha256',{}).items():
                if name not in ('stream_quality.py','stream-quality-capture','factory_runtime.py','switch_runtime.py','telemetry_runtime.py','frame_images.py','host-capture','host-imu','rv1126b.py') or digest(root/name)!=expected:raise RuntimeError('Capture runtime hash mismatch')
            entry=root/'test-cases'/code/'1.0.0/run.py'
            if digest(tool)!=adapter['tool_sha256'] or digest(root/'test_runtime.py')!=adapter['runtime_sha256'] or digest(entry)!=adapter['entrypoint_sha256'] or digest(entry.parent/'case.json')!=adapter['definition_sha256']:
                raise RuntimeError('Test package version/hash mismatch')
            for fixture in adapter.get('files',[]):
                if digest(entry.parent/fixture['path'])!=fixture['sha256']: raise RuntimeError('Fixture hash mismatch')
            out=self.path/'cases'/code/'attempt-001';out.mkdir(parents=True,exist_ok=False)
            data={'tools_package':tool_package,'round_id':config['round_id'],'case_id':code,'definition':adapter,'tool':str(tool),'tool_sha256':adapter['tool_sha256'],'video':video,'usb_location':location,'commit':config['source']['commit'],'firmware_sha256':self.result['firmware_sha256'],'parameters':item.get('parameters',{})}
            if code in SPECS or code in json.loads((root/'info-codes.json').read_text()) or code in json.loads((root/'factory-codes.json').read_text()):
                from rv1126b import discover
                matching=[d for d in discover() if d['serial']==config['flash']['device_serial'] and self.usb_location(Path(d['usb_path']))==location]
                if len(matching)!=1:raise RuntimeError('Target device unavailable for switching case')
                data['device']=matching[0];data['video']=matching[0]['video']
                if code.startswith('INFO-'):
                    data['imu_tool']=str(root/'host-imu')
                    data['imu_tool_sha256']=digest(root/'host-imu')
                    if code=='INFO-IMU':
                        from rv1126b import locked_hidraw
                        data['hidraw']=locked_hidraw(matching[0]['usb_port'])
            atomic(out/'case-input.json',data)
            self.result['cases'][index]={'id':item['id'],'code':code,'name':item['name'],'state':'running'}
            self.save(message='Running '+code,current_case=code)
            rc,_=self.run(['/usr/bin/python3',str(entry),'--input',str(out/'case-input.json'),'--output-dir',str(out)],code+'.log',87300 if code=='STREAM-RGB-DEPTH' else 900 if code in SPECS or code in json.loads((root/'factory-codes.json').read_text()) else 180 if code.startswith('INFO-') else 120)
            result=json.loads((out/'result.json').read_text())
            passed=rc==0 and result.get('state')=='finished' and result.get('outcome')=='passed' and bool(result.get('assertions')) and all(a.get('passed') is True for a in result['assertions'])
            all_pass=all_pass and passed
            self.result['cases'][index]={'id':item['id'],'code':code,'name':item['name'],'state':'finished','outcome':result.get('outcome','error'),'recovery':result.get('recovery'), 'assertions_passed':sum(a['passed'] for a in result.get('assertions',[])),'assertions_total':len(result.get('assertions',[]))}
            self.save()
            if result.get('recovery',{}).get('state')=='failed':
                raise RuntimeError('Device recovery failed; subsequent cases blocked')
        self.save('tests_complete',state='finished',outcome='passed' if all_pass else 'failed',message='Basic cases completed',completed_at=timestamp(),current_case=None)

    def execute(self):
        marker=self.path/'READY'; config_file=self.path/'test-run.json'
        if marker.is_symlink() or config_file.is_symlink(): raise RuntimeError('Symlink input rejected')
        marker_data=json.loads(marker.read_text())
        self.result['config_sha256']=marker_data['config_sha256']
        if digest(config_file)!=marker_data['config_sha256']: raise RuntimeError('JSON digest mismatch')
        config=json.loads(config_file.read_text())
        if config.get('schema_version')!=2 or config.get('execution_boundary') not in ('firmware_flash','current_firmware'): raise RuntimeError('Unsupported job protocol')
        self.result['round_id']=config['round_id']
        self.result['config_sha256']=marker_data['config_sha256']
        self.result['cases']=[{'id':c['id'],'code':c.get('code'),'name':c.get('name'),'category':c.get('category','basic'),'state':'queued' if config.get('execute_test_cases') else 'not_run','reason':'Waiting for verified firmware boot' if config.get('execute_test_cases') else 'Current boundary is firmware flashing'} for c in config['test_cases']]
        if config.get('execution_boundary')=='current_firmware':
            from quick_tests import execute
            execute(self,config)
            return
        if config.get('flash',{}).get('target_chip')=='RV1126B':
            from rv1126b import execute
            execute(self, config)
            return
        if config.get('flash',{}).get('target_chip')!='RK3572': raise RuntimeError('Only verified RK3572 adapter is enabled')
        images=config['artifacts']
        if len(images)!=1 or images[0]['filename']!='update.img': raise RuntimeError('Exactly update.img is required')
        image=self.path/'firmware/update.img'
        if (self.path/'firmware').is_symlink() or image.is_symlink() or image.resolve().parent!=(self.path/'firmware').resolve(): raise RuntimeError('Invalid firmware path')
        if image.stat().st_size!=images[0]['size_bytes'] or digest(image)!=images[0]['sha256']: raise RuntimeError('Firmware digest mismatch')
        self.save('waiting_device',message='Waiting for exactly one RK3572 in Loader/Maskrom mode')
        tool=str(Path(__file__).resolve().parent/'tools/upgrade-tool')
        deadline=time.monotonic()+600
        while True:
            rc,devices=self.run([tool,'LD'],'device.log')
            count=re.search(r'connected\((\d+)\)',devices)
            if not count or rc!=0: raise RuntimeError('Cannot enumerate rockusb devices')
            if int(count[1])>1: raise RuntimeError('Multiple rockusb devices; target selection required')
            if int(count[1])==1:
                serial=re.search(r'SerialNo=(\S+)',devices)
                if not serial or serial[1]!=config['flash'].get('device_serial'): raise RuntimeError('Connected device serial differs from locked target')
                break
            if time.monotonic()>deadline: raise RuntimeError('No device connected within 600 seconds')
            time.sleep(2)
        # Tool device output and firmware metadata must both identify the selected chip.
        self.save('checking_device',message='Checking chip and firmware identity')
        rc,chip=self.run([tool,'RCI'],'chip.log')
        if rc!=0 or '41 31 35 33' not in chip: raise RuntimeError('Cannot verify connected RK3572 chip; flashing blocked')
        rc,info=self.run([tool,'SFI',str(image)],'firmware-info.log')
        if rc!=0 or not re.search(r'Chip Tag:\s*351A\b',info): raise RuntimeError('Cannot verify RK3572 firmware; flashing blocked')
        physical=next((self.usb_location(d) for d in Path('/sys/bus/usb/devices').iterdir() if (d/'idVendor').exists() and (d/'serial').exists() and (d/'idVendor').read_text().strip()=='2207' and (d/'serial').read_text().strip()==config['flash']['device_serial']), '')
        if config.get('execute_test_cases') and not physical: raise RuntimeError('Cannot identify physical target USB port')
        self.save('flashing',message='Writing verified update.img; do not disconnect device',firmware_sha256=images[0]['sha256'])
        rc,output=self.run([tool,'UF',str(image)],'flash.log',600)
        if rc!=0 or not re.search(r'(?i)(upgrade|download) firmware (success|ok)',output): raise RuntimeError('Upgrade tool did not confirm firmware write success (exit '+str(rc)+')')
        if config.get('execute_test_cases'):
            self.result['flash_exit_code']=rc
            self.run_tests(config,physical)
            return
        self.save('flash_complete',state='finished',outcome='passed',message='Firmware tool confirmed write success; Test Cases have not run',flash_exit_code=rc,completed_at=timestamp(),boot_verified=False)


def main(path):
    path=Path(path).resolve()
    (path/'logs').mkdir(exist_ok=True)
    with (path/'.lock').open('w') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        job=Job(path)
        if (path/'result.json').exists():
            old=json.loads((path/'result.json').read_text())
            if old.get('state') in ('finished','interrupted','rejected'): return
            job.result=old
            job.save('interrupted',state='interrupted',outcome='error',message='Controller restarted; automatic reflash is disabled')
            return
        try:
            job.save(message='Validating delivered inputs')
            job.execute()
        except Exception as exc:
            for case in job.result.get('cases',[]):
                if case.get('state')=='running': case.update(state='finished',outcome='error',reason=str(exc))
                elif case.get('state')=='queued': case.update(state='blocked',reason=str(exc))
            job.save(state='finished',outcome='error',message=str(exc),completed_at=timestamp())

if __name__=='__main__': main(sys.argv[1])
