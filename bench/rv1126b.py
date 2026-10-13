"""RMSL321 runtime discovery and fixed RV1126B DFU adapter.

Only boot/system/oem are written to the opposite slot. Calibration, userdata,
bootloader and environment images are never accepted by this adapter.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parent
CTL = ROOT / json.loads((ROOT / 'tools.lock.json').read_text())['binary_path']
BOARD = 'BoardConfig-SPI_NAND-RK805-RV1126B_EVB1_V10_V11-DualCamDis-v2.0-arm64.mk'
PARTS = ('oem', 'system', 'boot')
LEGACY_COUNTS = Path('/home/linaro/Desktop/DEV-structlight/tools/.ota_counts')


def discover():
    devices = []
    for node in sorted(Path('/sys/class/video4linux').glob('video*')):
        try:
            if 'RMSL321_RGB' not in (node / 'name').read_text():
                continue
            if (node / 'index').read_text().strip() != '0':
                continue
            usb = next((p for p in (node / 'device').resolve().parents if (p/'idVendor').exists()), None)
            if not usb or (usb/'idVendor').read_text().strip() != '2207' or (usb/'idProduct').read_text().strip() != '001a':
                continue
            video = '/dev/' + node.name
            result = subprocess.run([str(CTL), '--device', video, 'get-cpu-serial'], capture_output=True, text=True, timeout=10)
            serial = result.stdout.strip()
            if result.returncode or not re.fullmatch(r'[0-9a-fA-F]{8,64}', serial) or not int(serial,16):
                continue
            result = subprocess.run([str(CTL), '--device', video, 'get-version'], capture_output=True, text=True, timeout=10)
            if result.returncode:
                continue
            devices.append({'serial':serial, 'mode':'UVC / RV1126B DFU', 'chip':'RV1126B', 'video':video,
                            'usb_path':str(usb), 'usb_port':usb.name, 'version':result.stdout.strip()})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return devices


def validate_images(job, config):
    from worker import digest, validate_adapter_registration
    from firmware_identity import source_identity
    try:
        source_identity(config.get('source'))
    except ValueError as exc:
        raise RuntimeError('Invalid firmware identity before flash: '+str(exc)) from exc
    if config.get('baseline',{}).get('board_config') != BOARD:
        raise RuntimeError('RV1126B Board Config is not supported by the RMSL321 V2.0 adapter')
    images = config.get('artifacts',[])
    if len(images)!=3 or {i['filename'] for i in images} != {p+'.img' for p in PARTS}:
        raise RuntimeError('RMSL321 requires exactly oem.img, system.img and boot.img')
    for item in images:
        path = job.path/'firmware'/item['filename']
        if path.is_symlink() or (job.path/'firmware').is_symlink() or path.resolve().parent != (job.path/'firmware').resolve():
            raise RuntimeError('Unsafe firmware path')
        if path.stat().st_size!=item['size_bytes'] or digest(path)!=item['sha256']:
            raise RuntimeError('Firmware digest mismatch: '+item['filename'])
    flash = config.get('flash', {})
    if flash.get('worker_sha256') != digest(ROOT/'worker.py') or flash.get('adapter_sha256') != digest(Path(__file__)):
        raise RuntimeError('Controller version/hash differs from the frozen round')
    adapters=[c.get('adapter') or {} for c in config['test_cases']]
    if not adapters or any(a.get('tool_sha256')!=digest(CTL) for a in adapters):
        raise RuntimeError('Verified XU control tool required before flashing')
    for adapter in adapters:
        code = adapter.get('id')
        validate_adapter_registration(ROOT, adapter)
        folder = ROOT/'test-cases'/code/'1.0.0'
        checks = [(ROOT/'test_runtime.py', 'runtime_sha256'), (folder/'run.py', 'entrypoint_sha256'), (folder/'case.json','definition_sha256')]
        for path, key in checks:
            if not path.is_file() or digest(path) != adapter.get(key):
                raise RuntimeError('Test package hash mismatch before flash: '+key)
        for fixture in adapter.get('files', []):
            path = (folder/fixture['path']).resolve()
            if folder.resolve() not in path.parents or not path.is_file() or digest(path) != fixture['sha256']:
                raise RuntimeError('Test fixture mismatch before flash')
    if not config.get('execute_test_cases'):
        raise RuntimeError('RMSL321 flashing requires post-flash version and test verification')
    return images


def execute(job, config):
    from worker import atomic, digest
    images=validate_images(job,config)
    target=config['flash']
    devices=discover()
    matching=[d for d in devices if d['serial']==target['device_serial'] and d['usb_path']==target['usb_path']]
    if len(matching)!=1:
        raise RuntimeError('Locked RMSL321 CPU serial / physical USB port is unavailable')
    device=matching[0]
    slot_match=re.search(r'(?:^|\s)_?([abAB])\s*$',device['version'])
    if not slot_match:
        raise RuntimeError('Cannot determine current boot slot from firmware version')
    current=slot_match[1].lower();slot='b' if current=='a' else 'a'
    counter=ROOT/'flash-counts.json'
    counts=json.loads(counter.read_text()) if counter.exists() else {}
    device_counts=counts.setdefault(device['serial'],{'a':0,'b':0})
    # Keep historical write counts for audit; counts never limit future writes.
    legacy={}
    if LEGACY_COUNTS.exists():
        for line in LEGACY_COUNTS.read_text().splitlines():
            key,value=line.split();legacy[key]=int(value)
    used=max(device_counts.get(slot,0),legacy.get(slot,0))
    rc,output=job.run(['sudo','-n','dfu-util','--version'],'dfu-version.log',10)
    if rc:
        raise RuntimeError('dfu-util or its permissions are unavailable')
    physical=job.usb_location(Path(device['usb_path']))
    if not physical:
        raise RuntimeError('Cannot bind physical USB port')
    job.save('checking_device',message='Verified RMSL321 identity, image hashes and opposite boot slot',
             device=device,source_slot=current,target_slot=slot,
             firmware_sha256=next(i['sha256'] for i in images if i['filename']=='oem.img'),
             firmware_artifacts=images,usb_location=physical)
    # Reserve a write before requesting erase; interrupted/failed attempts still count.
    device_counts[slot]=used+1
    atomic(counter,counts)
    job.save('flashing',message='Entering DFU; writing boot/system/oem to slot '+slot+'; do not disconnect',write_attempt=device_counts[slot])
    part_list=','.join(p+'_'+slot for p in PARTS)
    rc,output=job.run([str(CTL),'--device',device['video'],'enter-dfu',part_list],'enter-dfu.log',30)
    if rc or 'control command send failed' in output:
        raise RuntimeError('ENTER_DFU failed')
    deadline=time.monotonic()+150
    dfu_port = None
    while True:
        nodes = [p for p in Path('/sys/bus/usb/devices').iterdir() if (p/'idVendor').exists()
                 and (p/'idProduct').exists() and (p/'idVendor').read_text().strip() == '2207'
                 and (p/'idProduct').read_text().strip() == '0107' and job.usb_location(p) == physical]
        dfu_port = nodes[0].name if len(nodes) == 1 else None
        if not dfu_port:
            if time.monotonic() > deadline:
                raise RuntimeError('DFU did not appear at the locked physical USB port')
            time.sleep(2)
            continue
        rc,listing=job.run(['sudo','-n','dfu-util','-d','2207:0107','-p',dfu_port,'-l'],'dfu-list.log',15)
        if rc==0 and all('name="'+p+'_'+slot+'"' in listing for p in PARTS):
            break
        if time.monotonic()>deadline:
            raise RuntimeError('DFU did not expose all expected target-slot partitions on the locked USB port')
        time.sleep(2)
    records=[]
    for index,part in enumerate(PARTS):
        image=job.path/'firmware'/(part+'.img')
        item=next(i for i in images if i['filename']==image.name)
        if digest(image)!=item['sha256']:
            raise RuntimeError('Firmware changed before write')
        args=['sudo','-n','env','DFU_TIMEOUT=120000','dfu-util','-d','2207:0107','-p',dfu_port,'-a',part+'_'+slot,'-D',str(image)]
        last=index==len(PARTS)-1
        if last:args.append('-R')
        rc,output=job.run(args,'flash-'+part+'.log',240)
        # Reset may report device disappearance, but a complete download is still required.
        complete='Download done.' in output
        if not complete or rc not in ({0,251} if last else {0}):
            raise RuntimeError('DFU write not confirmed for '+part+' (exit '+str(rc)+')')
        records.append({'partition':part+'_'+slot,'sha256':item['sha256'],'exit_code':rc,'download_confirmed':True})
        job.save(flash_partitions=records)
    job.result['flash_exit_code']=0
    job.run_tests(config,physical)

if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--list']:
        print(json.dumps(discover()))
    else:
        raise SystemExit('Only --list is supported')


def locked_hidraw(usb_port):
    """Select the HID node belonging to the already locked USB device."""
    matches=[]
    for node in Path('/sys/class/hidraw').glob('hidraw*'):
        try:
            resolved=(node/'device').resolve()
            usb=next((parent for parent in resolved.parents if (parent/'idVendor').exists()),None)
            if usb and usb.name==usb_port:
                matches.append('/dev/'+node.name)
        except OSError:
            continue
    if len(matches)!=1:
        raise RuntimeError('Expected one HID node at locked USB port; found '+str(matches))
    return matches[0]
