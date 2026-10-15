from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from develop_hub.testing.packages import import_case_package


ROOT = Path(__file__).resolve().parents[2]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def driver():
    return module(ROOT / "bench/testing_rv1126b_driver.py", "rv_test_driver")


@pytest.fixture
def version_case():
    return module(ROOT / "samples/case-packages/rk-version/run.py", "rv_version_case")


@pytest.fixture
def config(tmp_path):
    tool = tmp_path / "uvc_host_ctl"
    tool.write_bytes(b"fake binary never executed")
    return {"serial": "00000000000000ab", "ctl": {"path": str(tool), "sha256": hashlib.sha256(tool.read_bytes()).hexdigest()}}


def device_tree(tmp_path, count=1):
    video_root = tmp_path / "video4linux"
    video_root.mkdir()
    for i in range(count):
        usb = tmp_path / f"usb-{i}"
        (usb / "interface").mkdir(parents=True)
        (usb / "idVendor").write_text("2207")
        (usb / "idProduct").write_text("001a")
        video = video_root / f"video{i}"
        video.mkdir()
        (video / "name").write_text("RMSL321_RGB")
        (video / "index").write_text("0")
        try:
            (video / "device").symlink_to(usb / "interface", target_is_directory=True)
        except OSError:
            pytest.skip("Host does not allow symlink creation")
    return video_root


def test_driver_reads_one_pinned_module_with_exact_tool_hash(driver, config, tmp_path, monkeypatch):
    tree = device_tree(tmp_path)
    commands = []

    def command(argv, **kwargs):
        commands.append(argv)
        value = config["serial"] if argv[-1] == "get-cpu-serial" else "RK APP version: factory-V2.1.0 _a"
        return SimpleNamespace(returncode=0, stdout=value, stderr="")

    monkeypatch.setattr(driver.subprocess, "run", command)
    result = driver.inspect_device(config, video_root=tree)
    assert result["serial"] == config["serial"]
    assert result["firmware_version"] == "RK APP version: factory-V2.1.0 _a"
    assert result["simulation"] is False
    assert result["identity_level"] == "device_reported_version"
    assert [argv[-1] for argv in commands] == ["get-cpu-serial", "get-version", "get-cpu-serial"]
    assert all(argv[1:3] == ["--device", "/dev/video0"] for argv in commands)


@pytest.mark.parametrize("count,serial", [(0, "00000000000000ab"), (2, "00000000000000ab"), (1, "00000000000000cd")])
def test_driver_rejects_missing_duplicate_and_wrong_serial(driver, config, tmp_path, monkeypatch, count, serial):
    tree = device_tree(tmp_path, count=count)
    monkeypatch.setattr(driver.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout=serial if argv[-1] == "get-cpu-serial" else "version", stderr=""))
    with pytest.raises(driver.DeviceDiscoveryError, match="exactly one") as error:
        driver.inspect_device(config, video_root=tree)
    if count == 0:
        assert '未检测到可用' in error.value.user_message
    elif count == 2:
        assert '多台身份匹配' in error.value.user_message
    else:
        assert '登记设备不一致' in error.value.user_message
        assert serial in error.value.user_message and config['serial'] in error.value.user_message


def test_inspection_cli_explains_identity_mismatch(driver, config, tmp_path, monkeypatch, capsys):
    data = tmp_path / 'input.json'
    data.write_text(json.dumps({'device': {'config': config}}))
    def mismatch(config):
        raise driver.DeviceDiscoveryError('technical diagnostic', '连接的模组与登记设备不一致')
    monkeypatch.setattr(driver, 'inspect_device', mismatch)
    assert driver.main(['inspect', '--input', str(data)]) == 2
    output = capsys.readouterr()
    assert '连接的模组与登记设备不一致' in output.err and not output.out


def test_driver_tool_tampering_stops_before_device_command(driver, config, tmp_path, monkeypatch):
    tree = device_tree(tmp_path)
    Path(config["ctl"]["path"]).write_text("changed")
    monkeypatch.setattr(driver.subprocess, "run", lambda *a, **k: pytest.fail("must not execute a changed tool"))
    with pytest.raises(RuntimeError, match="integrity"):
        driver.inspect_device(config, video_root=tree)


def test_driver_hotplug_identity_change_is_error(driver, config, tmp_path, monkeypatch):
    tree = device_tree(tmp_path)
    replies = iter([config["serial"], "factory-V2.1.0", "00000000000000cd"])
    monkeypatch.setattr(driver.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=next(replies), stderr=""))
    with pytest.raises(RuntimeError, match="identity changed"):
        driver.inspect_device(config, video_root=tree)


def case_input(config):
    return {"input_schema": "rk-case-input/1", "run_id": "test-run", "binding_id": "version", "case_revision_id": "version-r1",
            "parameters": {"expected_version": "factory-V2.1.0"},
            "device": {"config": config, "observation": {"serial": config["serial"], "video": "/dev/video0", "usb_path": "/sys/usb/example", "simulation": False}},
            "firmware": {"kind": "current_device"}}


def deployment_fixture(driver, config, tmp_path):
    firmware = tmp_path / 'firmware'
    firmware.mkdir()
    files = []
    for part in driver.PARTS:
        path = firmware / (part + '.img')
        path.write_bytes(part.encode())
        files.append({'path': path.name, 'size_bytes': path.stat().st_size, 'sha256': driver.file_digest(path)})
    dfu = tmp_path / 'dfu-util'
    dfu.write_bytes(b'pinned test executable')
    config.update(board_config=driver.BOARD, dfu={'path': str(dfu), 'sha256': driver.file_digest(dfu)})
    usb = tmp_path / 'controller' / 'usb1' / '1-2'
    usb.mkdir(parents=True)
    (usb / 'idVendor').write_text('2207')
    (usb / 'idProduct').write_text('0107')
    data = {'run_id': 'run-a', 'device': {'config': config}, 'firmware': {
        'root_path': 'firmware', 'files': files, 'digest': 'a' * 64, 'board_config': driver.BOARD,
        'chip': 'RV1126B', 'expected_version': 'factory-V2.1.0'}}
    device = {'serial': config['serial'], 'usb_path': str(usb), 'video': '/dev/video0', 'firmware_version': 'factory-V2.0.0 _a'}
    return data, device, usb.parent


@pytest.mark.parametrize('board', ['', 'different-board'])
def test_deploy_binds_port_opposite_slot_and_confirms_exact_boot(driver, config, tmp_path, board):
    data, device, usb_root = deployment_fixture(driver, config, tmp_path)
    data['firmware']['board_config'] = board
    config.pop('board_config')
    replies = iter([device, {**device, 'firmware_version': 'factory-V2.1.0 _b'}])
    commands = []
    def execute(argv, **kwargs):
        commands.append(argv)
        text = ' '.join('name="' + part + '_b"' for part in driver.PARTS) if '-l' in argv else 'Download done.'
        return SimpleNamespace(returncode=0, stdout=text)
    result = driver.deploy(data, tmp_path, tmp_path / 'out', usb_root=usb_root, discover=lambda c: next(replies), execute=execute)
    writes = [argv for argv in commands if '-D' in argv]
    assert [argv[argv.index('-a') + 1] for argv in writes] == ['oem_b', 'system_b', 'boot_b']
    assert all(argv[argv.index('-p') + 1] == '1-2' for argv in writes)
    assert '-R' not in writes[0] and '-R' in writes[-1]
    assert result['boot_verified'] and result['serial'] == config['serial']
    assert result['firmware_digest'] == data['firmware']['digest']


@pytest.mark.parametrize('invalid', ['chip', 'image', 'version'])
def test_deploy_rejects_invalid_input_before_hardware(driver, config, tmp_path, invalid):
    data, device, usb_root = deployment_fixture(driver, config, tmp_path)
    if invalid == 'chip':
        data['firmware']['chip'] = 'other-chip'
    elif invalid == 'image':
        (tmp_path / 'firmware/boot.img').write_bytes(b'changed')
    else:
        data['firmware']['expected_version'] = ''
    with pytest.raises(RuntimeError):
        driver.deploy(data, tmp_path, tmp_path / 'out', usb_root=usb_root,
                      discover=lambda c: pytest.fail('must stop before inspection'),
                      execute=lambda *a, **k: pytest.fail('must not invoke hardware'))


def test_deploy_stops_after_unconfirmed_partition(driver, config, tmp_path):
    data, device, usb_root = deployment_fixture(driver, config, tmp_path)
    writes = []
    def execute(argv, **kwargs):
        if '-D' in argv:
            writes.append(argv)
            return SimpleNamespace(returncode=0, stdout='transfer incomplete')
        return SimpleNamespace(returncode=0, stdout=' '.join('name="' + part + '_b"' for part in driver.PARTS))
    with pytest.raises(RuntimeError, match='Partition write not confirmed'):
        driver.deploy(data, tmp_path, tmp_path / 'out', usb_root=usb_root, discover=lambda c: device, execute=execute)
    assert len(writes) == 1
    assert not (tmp_path / 'out/deployment-result.json').exists()


def test_deploy_wrong_boot_version_never_passes(driver, config, tmp_path):
    data, device, usb_root = deployment_fixture(driver, config, tmp_path)
    ticks = iter(range(0, 2000, 100))
    calls = 0
    def discover(c):
        nonlocal calls
        calls += 1
        return device if calls == 1 else {**device, 'firmware_version': 'factory-V2.1.00 _b'}
    def execute(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout='Download done. ' + ' '.join('name="' + part + '_b"' for part in driver.PARTS))
    with pytest.raises(RuntimeError, match='expected version was not confirmed'):
        driver.deploy(data, tmp_path, tmp_path / 'out', usb_root=usb_root, discover=discover,
                      execute=execute, clock=lambda: next(ticks), sleep=lambda _: None)
    assert not (tmp_path / 'out/deployment-result.json').exists()


@pytest.mark.parametrize("reported,expected_verdict", [("RK APP version: factory-V2.1.0 (compile date) _a", "passed"), ("RK APP version: factory-V2.1.00 _a", "failed")])
def test_new_case_reports_version_token_assertion_without_binary_identity_claim(version_case, config, tmp_path, monkeypatch, reported, expected_verdict):
    calls = []
    monkeypatch.setattr(version_case, "usb_identity", lambda *args: None)

    def run(argv, **kwargs):
        calls.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout=config["serial"] if argv[-1] == "get-cpu-serial" else reported, stderr="")

    monkeypatch.setattr(version_case.subprocess, "run", run)
    result = version_case.execute(case_input(config), tmp_path / "output")
    assert result["verdict"] == expected_verdict
    assert result["run_id"] == "test-run" and result["binding_id"] == "version"
    assert {a["id"] for a in result["assertions"]} == {"tool-integrity", "device-identity", "firmware-version"}
    assert result["identity_level"] == "device_reported_version"
    assert result["recovery"]["state"] == "not_required"
    assert result["attachments"]
    assert calls == ["get-cpu-serial", "get-version", "get-cpu-serial"]
    assert json.loads((tmp_path / "output/result.json").read_text())["verdict"] == expected_verdict


def test_version_case_does_not_read_version_from_wrong_module(version_case, config, tmp_path, monkeypatch):
    monkeypatch.setattr(version_case, "usb_identity", lambda *args: None)
    commands = []

    def run(argv, **kwargs):
        commands.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout="00000000000000cd", stderr="")

    monkeypatch.setattr(version_case.subprocess, "run", run)
    result = version_case.execute(case_input(config), tmp_path / "output")
    assert result["verdict"] == "error"
    assert commands == ["get-cpu-serial"]
    assert result["assertions"][-1]["passed"] is False


def test_version_case_physical_identity_change_stops_before_tool_call(version_case, config, tmp_path, monkeypatch):
    def mismatch(*args):
        raise RuntimeError("USB changed")

    monkeypatch.setattr(version_case, "usb_identity", mismatch)
    monkeypatch.setattr(version_case.subprocess, "run", lambda *a, **k: pytest.fail("must not access a changed device"))
    result = version_case.execute(case_input(config), tmp_path / "output")
    assert result["verdict"] == "error" and "USB changed" in result["message"]


def test_version_example_is_importable_under_new_static_contract(tmp_path):
    directory = ROOT / "samples/case-packages/rk-version"
    archive = tmp_path / "rk-version.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in directory.iterdir():
            if path.is_file():
                output.write(path, path.name)
    result = import_case_package(archive, tmp_path / "packages")
    assert result["package_id"] == "rk.uvc-version"
    assert result["trusted"] is False
    assert result["cases"][0]["requirements"]["capabilities"] == ["uvc.control.version.v1"]
