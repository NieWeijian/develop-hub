import importlib.util
import json
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location("bench_worker",Path(__file__).resolve().parents[2]/"bench/worker.py")
worker=importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def job(tmp_path):
    path=tmp_path/"TEST-000123"; path.mkdir(); (path/"firmware").mkdir(); (path/"logs").mkdir()
    image=path/"firmware/update.img"; image.write_bytes(b"fixture")
    config={"schema_version":2,"round_id":path.name,"execution_boundary":"firmware_flash","flash":{"target_chip":"RK3572","device_serial":"fixture-serial"},"test_cases":[{"id":"case1","category":"basic"}],"artifacts":[{"filename":"update.img","size_bytes":7,"sha256":worker.digest(image)}]}
    worker.atomic(path/"test-run.json",config)
    worker.atomic(path/"READY",{"config_sha256":worker.digest(path/"test-run.json")})
    return path


@pytest.mark.parametrize("failure",[None,"serial","chip","image","exit","success_text"])
def test_flash_evidence_and_no_repeat(tmp_path,monkeypatch,failure):
    path=job(tmp_path); calls=[]
    def run(self,args,name,timeout=60):
        calls.append(args[1])
        if args[1]=="LD": return 0,"List of rockusb connected(1) Mode=Loader SerialNo="+("wrong" if failure=="serial" else "fixture-serial")
        if args[1]=="RCI": return 0,"bad" if failure=="chip" else "Chip Info: 41 31 35 33"
        if args[1]=="SFI": return 0,"Chip Tag:351A"
        return (1 if failure=="exit" else 0), ("error" if failure=="success_text" else "Upgrade firmware success")
    monkeypatch.setattr(worker.Job,"run",run)
    if failure=="image": (path/"firmware/update.img").write_bytes(b"altered")
    worker.main(path)
    result=json.loads((path/"result.json").read_text())
    assert result["state"]=="finished"
    assert result["outcome"]==("passed" if failure is None else "error")
    assert result["cases"][0]["state"]=="not_run"
    if failure in {"serial","chip","image"}: assert "UF" not in calls
    before=list(calls); worker.main(path); assert calls==before


def test_restart_never_reflashes(tmp_path):
    path=job(tmp_path)
    worker.atomic(path/"result.json",{"round_id":path.name,"revision":3,"state":"running","phase":"flashing"})
    worker.main(path)
    assert json.loads((path/"result.json").read_text())["state"]=="interrupted"


def test_command_output_is_live(tmp_path,monkeypatch):
    import sys
    path=job(tmp_path); instance=worker.Job(path); seen=[]
    original=instance.save
    def save(*args,**kwargs):
        seen.append(instance.result['log_tail'])
        return original(*args,**kwargs)
    monkeypatch.setattr(instance,'save',save)
    code,output=instance.run([sys.executable,'-c',"import time; print('started'); time.sleep(1.2); print('finished')"],'live.log')
    assert code==0
    assert any('started' in line and 'finished' not in line for line in seen)
    assert 'finished' in output


def test_flash_and_test_rejects_missing_identity_before_hardware(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'bench'))
    path = job(tmp_path)
    config = json.loads((path / 'test-run.json').read_text())
    config['execute_test_cases'] = True
    worker.atomic(path / 'test-run.json', config)
    worker.atomic(path / 'READY', {'config_sha256': worker.digest(path / 'test-run.json')})
    calls = []
    monkeypatch.setattr(worker.Job, 'run', lambda *args, **kwargs: calls.append(args))
    worker.main(path)
    result = json.loads((path / 'result.json').read_text())
    assert result['outcome'] == 'error'
    assert 'Firmware source metadata is required' in result['message']
    assert calls == []
