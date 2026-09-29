"""Current-firmware execution boundary: no build, DFU or flash operation."""
import json
from pathlib import Path
from worker import atomic, digest, timestamp
from rv1126b import discover
from switch_runtime import SPECS

ROOT=Path(__file__).resolve().parent
INFO_CODES=tuple(json.loads((ROOT/'info-codes.json').read_text()))
FACTORY_CODES=tuple(json.loads((ROOT/'factory-codes.json').read_text()))

def execute(job,config):
    if config.get('flash') or config.get('artifacts'):raise RuntimeError('Quick tests cannot include flash instructions or firmware artifacts')
    if not config.get('test_cases'):raise RuntimeError('No selected cases')
    for name,sha in config['package_sha256'].items():
        path=(ROOT/name).resolve()
        if ROOT not in path.parents or digest(path)!=sha:raise RuntimeError('Test package mismatch: '+name)
    target=config['device']
    matching=[d for d in discover() if all(d[k]==target[k] for k in ('serial','usb_port','version'))]
    if len(matching)!=1:raise RuntimeError('Current firmware/CPU serial/USB port differs from snapshot')
    job.save('test_cases',execution_boundary='current_firmware',identity_verified=True,boot_version=target['version'],device=target,message='Current firmware verified; build and flash skipped')
    all_pass=True
    for index,item in enumerate(config['test_cases']):
        code=item['code']
        if code not in SPECS and code not in INFO_CODES and code not in FACTORY_CODES:raise RuntimeError('Unregistered quick testcase')
        out=job.path/'cases'/code/'attempt-001';out.mkdir(parents=True,exist_ok=False)
        tool_package=item.get('adapter',{}).get('tools_package') or json.loads((ROOT/'tools.lock.json').read_text())
        tool=ROOT/tool_package['binary_path']
        if digest(tool)!=tool_package['binary_sha256']:raise RuntimeError('Frozen tool binary checksum mismatch')
        data={'definition':item.get('adapter',{}),'tools_package':tool_package,'device':target,'tool':str(tool),'tool_sha256':digest(tool),'video':matching[0]['video'],'parameters':item.get('parameters',{})}
        if code in INFO_CODES or code in FACTORY_CODES:
            from rv1126b import locked_hidraw
            matches=[d for d in discover() if all(d[k]==target[k] for k in ('serial','usb_port','version'))]
            if len(matches)!=1:raise RuntimeError('Locked device unavailable for information case')
            data['device']=matches[0];data['video']=matches[0]['video']
            data['imu_tool']=str(ROOT/'host-imu')
            data['imu_tool_sha256']=digest(ROOT/'host-imu')
            if code=='INFO-IMU':data['hidraw']=locked_hidraw(target['usb_port'])
        atomic(out/'case-input.json',data)
        job.result['cases'][index].update(state='running')
        job.save(current_case=code,message='Running '+code)
        rc,_=job.run(['/usr/bin/python3',str(ROOT/'test-cases'/code/'1.0.0/run.py'),'--input',str(out/'case-input.json'),'--output-dir',str(out)],code+'.log',900)
        result=json.loads((out/'result.json').read_text())
        assertions=result.get('assertions',[])
        passed=rc==0 and result.get('outcome')=='passed' and assertions and all(a['passed'] for a in assertions) and result.get('recovery',{}).get('state')=='passed'
        all_pass=bool(all_pass and passed)
        job.result['cases'][index].update(state='finished',outcome=result.get('outcome','error'),recovery=result.get('recovery'),message=result.get('message'),assertions_passed=sum(a['passed'] for a in assertions),assertions_total=len(assertions),images=sum(f['role']=='image' for f in result.get('files',[])))
        job.save()
        if result.get('recovery',{}).get('state')=='failed':raise RuntimeError('Device recovery failed; remaining cases blocked')
    job.save('tests_complete',state='finished',outcome='passed' if all_pass else 'failed',message='Current-firmware quick tests completed',completed_at=timestamp(),current_case=None)
