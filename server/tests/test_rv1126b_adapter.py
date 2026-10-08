import importlib.util
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
    config={'baseline':{'board_config':rv.BOARD},'execute_test_cases':True,
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
