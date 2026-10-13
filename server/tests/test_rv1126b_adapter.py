import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

BENCH=Path(__file__).resolve().parents[2]/'bench'
sys.path.insert(0,str(BENCH))
import rv1126b as rv
from worker import digest
from develop_hub.basic_cases import definition
from develop_hub.test_workbench import TestRoundWorkflow as RoundWorkflow, build_command
from develop_hub.models import SdkRepository


def plan(tmp_path):
    path=tmp_path/'TEST-fixture';(path/'firmware').mkdir(parents=True)
    artifacts=[]
    for name in ('oem.img','system.img','boot.img'):
        file=path/'firmware'/name;file.write_bytes(name.encode())
        artifacts.append({'filename':name,'size_bytes':file.stat().st_size,'sha256':digest(file)})
    config={'source':{'commit':'abcdef0123456789'},'baseline':{'board_config':rv.BOARD},'execute_test_cases':True,
            'flash':{'worker_sha256':digest(BENCH/'worker.py'),'adapter_sha256':digest(BENCH/'rv1126b.py')},
            'artifacts':artifacts,'test_cases':[{'adapter':definition('BASIC-VERSION')}]}
    return SimpleNamespace(path=path),config


def test_preflight_accepts_verified_package(tmp_path):
    job,config=plan(tmp_path)
    assert len(rv.validate_images(job,config))==3


@pytest.mark.parametrize('tamper',['board','image','calibration','worker','adapter','runtime','tool','entry','empty','unknown_case'])
def test_reject_before_erase(tmp_path,tamper):
    job,config=plan(tmp_path)
    if tamper=='board':config['baseline']['board_config']='RK3572'
    elif tamper=='image':(job.path/'firmware/oem.img').write_bytes(b'changed')
    elif tamper=='calibration':config['artifacts'][0]['filename']='calibration.img'
    elif tamper in ('worker','adapter'):config['flash'][tamper+'_sha256']='bad'
    elif tamper=='runtime':config['test_cases'][0]['adapter']['runtime_sha256']='bad'
    elif tamper=='tool':config['test_cases'][0]['adapter']['tool_sha256']='bad'
    elif tamper=='entry':config['test_cases'][0]['adapter']['entrypoint_sha256']='bad'
    elif tamper=='empty':config['test_cases']=[]
    else:config['test_cases'][0]['adapter']['id']='arbitrary-command'
    with pytest.raises(RuntimeError):rv.validate_images(job,config)


def test_artifacts_keep_logical_names_and_freshness(tmp_path):
    root=tmp_path/'sdk';images=root/'output/image';images.mkdir(parents=True)
    target=images/'oem_geometry.ubi';target.write_bytes(b'firmware')
    (images/'oem.img').symlink_to(target.name)
    sdk=SdkRepository(root_path=str(root),artifact_root=str(images),artifact_patterns=['oem.img'])
    result=RoundWorkflow._collect_artifacts(sdk,{})
    assert result[0]['filename']=='oem.img'
    with pytest.raises(RuntimeError,match='not refreshed'):
        RoundWorkflow._collect_artifacts(sdk,RoundWorkflow._artifact_versions(sdk))


def test_sdk_specific_build_commands():
    assert build_command(rv.BOARD)==['./build.sh','all']
    assert build_command('rockchip_rk3572_struct_light_defconfig')==['./build.sh']


def test_full_release_catalog_preflight(tmp_path):
    from develop_hub.basic_cases import ACTIVE_CODES
    job, config = plan(tmp_path)
    config['test_cases'] = [{'adapter':definition(code)} for code in ACTIVE_CODES]
    assert len(rv.validate_images(job, config)) == 3


@pytest.mark.parametrize('code',['FILE-PERSIST-REBOOT','FACTORY-CALIB','STREAM-RGB-DEPTH'])
def test_expanded_cases_reject_bad_support_before_flash(tmp_path, code):
    job, config = plan(tmp_path)
    adapter = definition(code)
    adapter['support_sha256']['factory_runtime.py'] = 'bad'
    config['test_cases'] = [{'adapter':adapter}]
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        rv.validate_images(job, config)


def test_arbitrary_support_path_rejected(tmp_path):
    job, config = plan(tmp_path)
    config['test_cases'][0]['adapter']['support_sha256'] = {'../escape':'bad'}
    with pytest.raises(RuntimeError, match='unsupported'):
        rv.validate_images(job, config)


def flash_job(tmp_path, monkeypatch, current_slot='a', used=3, legacy_used=0):
    job, config = plan(tmp_path)
    device = {'serial': 'fixture-serial', 'usb_path': '/sys/locked-usb',
              'video': '/dev/video-fixture', 'version': 'factory-fixture _' + current_slot}
    config['flash'].update(device_serial=device['serial'], usb_path=device['usb_path'])
    target_slot = 'b' if current_slot == 'a' else 'a'
    root = tmp_path / 'bench'
    root.mkdir()
    counts = {device['serial']: {current_slot: 2, target_slot: used},
              'other-device': {'a': 7, 'b': 8}}
    (root / 'flash-counts.json').write_text(json.dumps(counts))
    legacy = root / 'legacy-counts'
    legacy.write_text(target_slot + ' ' + str(legacy_used) + '\n')
    bus = tmp_path / 'usb-devices'
    dfu = bus / '1-2'
    dfu.mkdir(parents=True)
    (dfu / 'idVendor').write_text('2207')
    (dfu / 'idProduct').write_text('0107')
    monkeypatch.setattr(rv, 'ROOT', root)
    monkeypatch.setattr(rv, 'LEGACY_COUNTS', legacy)
    monkeypatch.setattr(rv, 'Path', lambda value: bus if str(value) == '/sys/bus/usb/devices' else Path(value))
    monkeypatch.setattr(rv, 'validate_images', lambda job, config: config['artifacts'])
    monkeypatch.setattr(rv, 'discover', lambda: [device])
    calls, events, tested = [], [], []
    def run(args, name, timeout):
        calls.append((name, args))
        if name == 'dfu-list.log':
            return 0, ' '.join('name="' + part + '_' + target_slot + '"' for part in rv.PARTS)
        return 0, 'Download done.'
    job.run = run
    job.save = lambda phase=None, **values: events.append({'phase': phase, **values})
    job.usb_location = lambda path: 'locked-physical-port'
    job.run_tests = lambda config, physical: tested.append(physical)
    job.result = {}
    return job, config, device, root, calls, events, tested


@pytest.mark.parametrize('current_slot,used,legacy_used', [('a', 3, 0), ('b', 37, 0), ('a', 5, 42)])
def test_flash_has_no_write_count_limit(tmp_path, monkeypatch, current_slot, used, legacy_used):
    job, config, device, root, calls, events, tested = flash_job(
        tmp_path, monkeypatch, current_slot, used, legacy_used)
    rv.execute(job, config)
    target = 'b' if current_slot == 'a' else 'a'
    counts = json.loads((root / 'flash-counts.json').read_text())
    assert counts[device['serial']] == {current_slot: 2, target: max(used, legacy_used) + 1}
    assert counts['other-device'] == {'a': 7, 'b': 8}
    enter = next(args for name, args in calls if name == 'enter-dfu.log')
    assert enter[-1] == ','.join(part + '_' + target for part in rv.PARTS)
    writes = [args for name, args in calls if name.startswith('flash-')]
    assert [args[args.index('-a') + 1] for args in writes] == [part + '_' + target for part in rv.PARTS]
    assert all(args[args.index('-p') + 1] == '1-2' for args in writes)
    assert events[0]['source_slot'] == current_slot
    assert events[0]['target_slot'] == target
    assert events[1]['write_attempt'] == max(used, legacy_used) + 1
    assert job.result['flash_exit_code'] == 0
    assert tested == ['locked-physical-port']


@pytest.mark.parametrize('failure,expected', [
    ('serial', 'CPU serial / physical USB port'),
    ('usb_path', 'CPU serial / physical USB port'),
    ('slot', 'Cannot determine current boot slot'),
])
def test_unlimited_writes_keep_device_and_slot_guards(tmp_path, monkeypatch, failure, expected):
    job, config, device, root, calls, events, tested = flash_job(tmp_path, monkeypatch)
    before = (root / 'flash-counts.json').read_text()
    if failure == 'slot':
        device['version'] = 'unknown-slot'
    else:
        device[failure] = 'wrong-device'
    with pytest.raises(RuntimeError, match=expected):
        rv.execute(job, config)
    assert calls == []
    assert tested == []
    assert (root / 'flash-counts.json').read_text() == before


@pytest.mark.parametrize('failed_command', ['enter-dfu.log', 'flash-oem.log'])
def test_failed_flash_still_counts_write_attempt(tmp_path, monkeypatch, failed_command):
    job, config, device, root, calls, events, tested = flash_job(tmp_path, monkeypatch)
    original_run = job.run
    def run(args, name, timeout):
        result = original_run(args, name, timeout)
        return (1, 'failed') if name == failed_command else result
    job.run = run
    with pytest.raises(RuntimeError, match='ENTER_DFU failed|DFU write not confirmed'):
        rv.execute(job, config)
    assert json.loads((root / 'flash-counts.json').read_text())[device['serial']]['b'] == 4
    assert tested == []
    assert 'flash_exit_code' not in job.result


@pytest.mark.parametrize('source', [{}, {'commit': ''}, {'identity': {'kind': 'version', 'value': ''}}])
def test_invalid_identity_rejected_before_hardware(tmp_path, monkeypatch, source):
    job, config = plan(tmp_path)
    config['source'] = source
    calls = []
    monkeypatch.setattr(rv, 'discover', lambda: calls.append('discover'))
    with pytest.raises(RuntimeError, match='Invalid firmware identity before flash'):
        rv.execute(job, config)
    assert calls == []
