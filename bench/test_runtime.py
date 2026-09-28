import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from worker import atomic, timestamp, digest

class Case:
    def __init__(self, code, data, out):
        self.code=code; self.data=data; self.out=out; out.mkdir(parents=True,exist_ok=True)
        self.result={'id':code,'version':'1.0.0','state':'running','outcome':None,'started_at':timestamp(),'assertions':[],'steps':[],'files':[],'recovery':{'state':'not_required'}}
        atomic(out/'case-input.json',data); self.save()
    def save(self): atomic(self.out/'result.json',self.result)
    def check(self,name,expected,actual,passed):
        self.result['assertions'].append({'name':name,'expected':expected,'actual':actual,'passed':bool(passed)});self.save()
        if not passed: raise AssertionError(name)
    def call(self,step,*args):
        command=[self.data['tool'],'--device',self.data['video'],*map(str,args)]
        env={**os.environ,'RMSL_XU_TRACE':str(self.out/'protocol.jsonl'),'RMSL_TRACE_STEP':step}
        start=timestamp()
        proc=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=30)
        output=proc.stdout.decode(errors='replace');error=proc.stderr.decode(errors='replace')
        with (self.out/'stdout.log').open('a') as f: f.write('\n['+step+']\n'+output)
        with (self.out/'stderr.log').open('a') as f: f.write('\n['+step+']\n'+error)
        self.result['steps'].append({'id':step,'command':command,'started_at':start,'completed_at':timestamp(),'exit_code':proc.returncode,'stdout':output[-1500:],'stderr':error[-1000:]});self.save()
        if proc.returncode: raise RuntimeError(step+' tool exit '+str(proc.returncode))
        return output.strip()
    def fov(self,step):
        value=float(self.call(step,'get-fov').splitlines()[-1])
        if not math.isfinite(value): raise RuntimeError('Invalid FOV response')
        return value
    def choices(self):
        text=self.call('supported-fov','get-fov-list')
        return [float(v) for v in text.split(':',1)[-1].split()]
    def run(self):
        restore=None
        try:
            self.check('Tool SHA-256',self.data['tool_sha256'],digest(Path(self.data['tool'])),digest(Path(self.data['tool']))==self.data['tool_sha256'])
            if self.code=='BASIC-VERSION':
                text=self.call('read-version','get-version')
                self.check('Firmware version',self.data['commit'][:7],text,self.data['commit'][:7] in text)
            elif self.code=='BASIC-FOV-READ':
                actual=self.fov('read-fov');choices=self.choices()
                self.check('FOV belongs to supported list',choices,actual,any(abs(actual-v)<=0.02 for v in choices))
            elif self.code=='BASIC-FOV-WRITE':
                original=self.fov('save-fov'); choices=self.choices()
                target=self.data.get('parameters',{}).get('target_fov',next((v for v in choices if abs(v-original)>0.02),original))
                self.check('Supported target',choices,target,isinstance(target,(int,float)) and any(abs(target-v)<=0.02 for v in choices))
                restore=original;self.result['recovery']={'state':'pending','original':original};self.save()
                self.call('write-fov','set-fov',target);time.sleep(0.5);actual=self.fov('verify-fov')
                self.check('Written FOV readback',target,actual,abs(target-actual)<=0.02)
            elif self.code=='BASIC-FILE-ROUNDTRIP':
                fixture=Path(__file__).parent/'test-cases'/self.code/'1.0.0/fixtures/payload.bin'
                inputs=self.out/'inputs';inputs.mkdir(exist_ok=True);local=inputs/'payload.bin';local.write_bytes(fixture.read_bytes())
                artifacts=self.out/'artifacts';artifacts.mkdir(exist_ok=True);download=artifacts/'download.bin'
                remote='/tmp/qa-'+hashlib.sha256(str(self.out).encode()).hexdigest()[:16]+'.bin'
                self.result['files'].append({'path':'inputs/payload.bin','size_bytes':local.stat().st_size,'sha256':digest(local),'role':'input'})
                self.result['recovery']={'state':'retained_temporary_file','remote_path':remote,'reason':'Current XU CLI has no delete command; file is isolated under /tmp and cleared on reboot'}
                self.save()
                self.call('upload','file-put',str(local),remote)
                self.call('download','file-get',remote,str(download))
                self.result['files'].append({'path':'artifacts/download.bin','size_bytes':download.stat().st_size,'sha256':digest(download),'role':'output'})
                self.check('File size',local.stat().st_size,download.stat().st_size,local.stat().st_size==download.stat().st_size)
                self.check('File SHA-256',digest(local),digest(download),digest(local)==digest(download))
            else: raise RuntimeError('Unsupported case')
            trace=self.out/'protocol.jsonl'
            self.check('Actual XU evidence','nonempty protocol.jsonl',trace.stat().st_size if trace.exists() else 0,trace.exists() and trace.stat().st_size>0)
            self.result['outcome']='passed'
        except AssertionError as exc: self.result.update(outcome='failed',message=str(exc))
        except Exception as exc: self.result.update(outcome='error',message=str(exc))
        finally:
            if restore is not None:
                try:
                    self.call('restore-fov','set-fov',restore);time.sleep(0.5);actual=self.fov('verify-restore')
                    self.result['recovery']={'state':'passed' if abs(actual-restore)<=0.02 else 'failed','expected':restore,'actual':actual}
                    if abs(actual-restore)>0.02: self.result['outcome']='error'
                except Exception as exc: self.result.update(outcome='error',recovery={'state':'failed','error':str(exc)})
            self.result.update(state='finished',completed_at=timestamp());self.save()
        return 0 if self.result['outcome']=='passed' else 1

def main(code):
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True);parser.add_argument('--output-dir',required=True);args=parser.parse_args()
    raise SystemExit(Case(code,json.loads(Path(args.input).read_text()),Path(args.output_dir)).run())
