#!/usr/bin/env python3
"""RMSL321 identity and opposite-slot DFU adapter for the new protocol.

No legacy database, tools.lock.json or fixed installation directory is read.
Deployment requires a compatible chip, version and exactly boot/system/oem.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

BOARD = 'BoardConfig-SPI_NAND-RK805-RV1126B_EVB1_V10_V11-DualCamDis-v2.0-arm64.mk'
PARTS = ('oem', 'system', 'boot')


class DeviceDiscoveryError(RuntimeError):
    def __init__(self, message, user_message):
        super().__init__(message)
        self.user_message = user_message


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def validate_tool(config: dict) -> Path:
    tool = config.get("ctl", {})
    path = Path(tool.get("path", ""))
    expected = tool.get("sha256", "")
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise RuntimeError("Register an absolute regular ctl binary path and its SHA-256")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected.lower():
        raise RuntimeError("Registered control tool integrity check failed")
    return path


def call(tool: Path, video: str, command: str) -> str:
    result = subprocess.run([str(tool), "--device", video, command], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"{command} failed (exit {result.returncode}): {result.stderr[-500:]}")
    text = result.stdout.strip()
    if not text or len(text) > 4096:
        raise RuntimeError(f"{command} returned an empty or oversized response")
    return text


def inspect_device(config: dict, *, video_root: Path = Path("/sys/class/video4linux")) -> dict:
    serial = config.get("serial", "")
    if not isinstance(serial, str) or not re.fullmatch(r"[0-9a-fA-F]{8,64}", serial) or not int(serial, 16):
        raise RuntimeError("Register the nonzero CPU serial of the target module")
    tool = validate_tool(config)
    expected_port = config.get("usb_path")
    matches, failures, other_serials = [], [], []
    wrong_port = False
    for node in sorted(video_root.glob("video*")):
        try:
            if "RMSL321_RGB" not in (node / "name").read_text() or (node / "index").read_text().strip() != "0":
                continue
            resolved = (node / "device").resolve(strict=True)
            usb = next((parent for parent in (resolved, *resolved.parents) if (parent / "idVendor").exists()), None)
            if usb is None or (usb / "idVendor").read_text().strip().lower() != "2207" or (usb / "idProduct").read_text().strip().lower() != "001a":
                continue
            if expected_port and str(usb.resolve()) != str(Path(expected_port).resolve()):
                wrong_port = True
                continue
            video = "/dev/" + node.name
            actual = call(tool, video, "get-cpu-serial")
            if actual.lower() != serial.lower():
                other_serials.append(actual)
                continue
            version = call(tool, video, "get-version")
            # A hotplug between serial and version reads must not claim identity.
            if call(tool, video, "get-cpu-serial").lower() != serial.lower() or (node / "device").resolve(strict=True) != resolved:
                raise RuntimeError("Device identity changed during inspection")
            matches.append({"serial": serial, "observed_serial": actual, "firmware_version": version,
                            "chip": "RV1126B", "video": video, "usb_path": str(usb.resolve()), "usb_port": usb.name,
                            "identity_level": "device_reported_version", "simulation": False,
                            "capabilities": ["uvc.control.version.v1"], "tool_integrity_verified": True})
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            failures.append(str(exc))
    if len(matches) != 1:
        detail = "; ".join(failures[-3:])
        if len(matches) > 1:
            message = '检测到多台身份匹配的模组，请只连接本次要测试的一台设备'
        elif other_serials:
            actuals = '、'.join(sorted(set(other_serials)))
            message = f'连接的模组与登记设备不一致。登记序列号：{serial}；检测到：{actuals}。请接回登记模组，或将当前模组登记为新设备后在方案中选择。'
        elif failures:
            message = '已检测到模组接口，但身份读取失败，请检查设备连接或占用情况。详情：' + detail
        elif wrong_port:
            message = '检测到模组连接在其他 USB 端口，请接回登记端口后重试'
        else:
            message = '未检测到可用的 RMSL321 视频接口，请确认模组已连接测试节点、上电并完成启动'
        raise DeviceDiscoveryError(f"Expected exactly one module with the registered serial/USB path; found {len(matches)}. {detail}", message)
    return matches[0]


def physical_location(path):
    device = Path(path).resolve()
    for parent in device.parents:
        if re.fullmatch(r'usb\d+', parent.name):
            return str(parent.parent) + '/' + device.name.split('-', 1)[-1]
    raise RuntimeError('Cannot bind physical USB port')


def validate_deployment(data, input_root):
    config, firmware = data['device']['config'], data['firmware']
    if firmware.get('chip') != 'RV1126B':
        raise RuntimeError('Firmware chip must be RV1126B')
    version = firmware.get('expected_version', '')
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', version):
        raise RuntimeError('A complete expected firmware version token is required')
    root = (input_root / firmware['root_path']).resolve()
    if input_root.resolve() not in root.parents:
        raise RuntimeError('Firmware directory escapes the execution directory')
    files = firmware.get('files', [])
    if len(files) != 3 or {Path(f['path']).name for f in files} != {p + '.img' for p in PARTS}:
        raise RuntimeError('Exactly oem.img, system.img and boot.img are required')
    result = {}
    for item in files:
        path = root / item['path']
        if path.is_symlink() or root not in path.resolve().parents or not path.is_file() or path.stat().st_size != item['size_bytes'] or file_digest(path) != item['sha256']:
            raise RuntimeError('Firmware content or path does not match its manifest')
        result[path.name] = (path, item)
    if not re.fullmatch(r'[a-f0-9]{64}', firmware.get('digest', '')):
        raise RuntimeError('Firmware manifest digest missing')
    tool = validate_tool(config)
    dfu = config.get('dfu', {})
    dfu_tool = Path(dfu.get('path', ''))
    if not dfu_tool.is_absolute() or dfu_tool.is_symlink() or not dfu_tool.is_file() or file_digest(dfu_tool) != dfu.get('sha256'):
        raise RuntimeError('Registered dfu-util tool integrity check failed')
    prefix = ['sudo', '-n'] if dfu.get('sudo') is True else []
    return result, tool, prefix + [str(dfu_tool)], version


def deploy(data, input_root, output, *, usb_root=Path('/sys/bus/usb/devices'), discover=inspect_device, clock=time.monotonic, sleep=time.sleep, execute=subprocess.run):
    images, ctl, dfu, version = validate_deployment(data, input_root)
    config = data['device']['config']
    output.mkdir(parents=True, exist_ok=True)

    def run(argv, name, timeout=30):
        response = execute(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, env={**os.environ, 'DFU_TIMEOUT': '120000'})
        text = response.stdout or ''
        (output / name).write_text(text, encoding='utf-8')
        return response.returncode, text

    device = discover(config)
    slot_match = re.search(r'(?:^|\s)_?([abAB])\s*$', device['firmware_version'])
    if not slot_match:
        raise RuntimeError('Cannot identify active firmware slot')
    slot = 'b' if slot_match[1].lower() == 'a' else 'a'
    physical = physical_location(device['usb_path'])
    rc, _ = run(dfu + ['--version'], 'dfu-version.log')
    if rc:
        raise RuntimeError('dfu-util or its registered permissions are unavailable')
    # Record the intended write before requesting the device to enter DFU.
    intent = {'serial': device['serial'], 'physical_location': physical, 'target_slot': slot, 'firmware_digest': data['firmware']['digest']}
    (output / 'deployment-intent.json').write_text(json.dumps(intent))
    rc, response = run([str(ctl), '--device', device['video'], 'enter-dfu', ','.join(p + '_' + slot for p in PARTS)], 'enter-dfu.log')
    if rc or 'control command send failed' in response:
        raise RuntimeError('Device did not accept entering DFU')
    deadline = clock() + 150
    while True:
        matches = [p for p in usb_root.iterdir() if (p / 'idVendor').is_file() and (p / 'idProduct').is_file() and (p / 'idVendor').read_text().strip() == '2207' and (p / 'idProduct').read_text().strip() == '0107' and physical_location(p) == physical]
        if len(matches) == 1:
            port = matches[0].name
            rc, listing = run(dfu + ['-d', '2207:0107', '-p', port, '-l'], 'dfu-list.log')
            if rc == 0 and all('name="' + p + '_' + slot + '"' in listing for p in PARTS):
                break
        if clock() >= deadline:
            raise RuntimeError('Target DFU port and opposite-slot partitions did not appear')
        sleep(2)
    records = []
    for index, part in enumerate(PARTS):
        path, item = images[part + '.img']
        if file_digest(path) != item['sha256']:
            raise RuntimeError('Firmware changed before writing')
        last = index == len(PARTS) - 1
        args = dfu + ['-d', '2207:0107', '-p', port, '-a', part + '_' + slot, '-D', str(path)] + (['-R'] if last else [])
        rc, response = run(args, 'flash-' + part + '.log', 240)
        if 'Download done.' not in response or rc not in ({0, 251} if last else {0}):
            raise RuntimeError('Partition write not confirmed: ' + part)
        records.append({'partition': part + '_' + slot, 'sha256': item['sha256'], 'exit_code': rc, 'download_confirmed': True})
        (output / 'written-partitions.json').write_text(json.dumps(records))
    deadline = clock() + 180
    while True:
        try:
            boot = discover(config)
            target_slot = re.search(r'(?:^|\s)_?([abAB])\s*$', boot['firmware_version'])
            same = boot['serial'] == device['serial'] and physical_location(boot['usb_path']) == physical
            identity = bool(re.search(r'(?<![A-Za-z0-9._+-])' + re.escape(version) + r'(?![A-Za-z0-9._+-])', boot['firmware_version']))
            if same and target_slot and target_slot[1].lower() == slot and identity:
                break
        except (RuntimeError, OSError):
            pass
        if clock() >= deadline:
            raise RuntimeError('Boot serial, physical port, target slot or expected version was not confirmed')
        sleep(2)
    result = {'deployment_schema': 'rk-deployment-result/1', 'run_id': data['run_id'], 'serial': device['serial'], 'firmware_digest': data['firmware']['digest'], 'boot_verified': True, 'target_slot': slot, 'observation': boot, 'partitions': records}
    (output / 'deployment-result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "deploy"])
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)
    try:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if args.action == 'deploy':
            if not args.output_dir:
                raise ValueError('Deployment output directory is required')
            result = deploy(data, Path(args.input).resolve().parent, Path(args.output_dir))
        else:
            result = inspect_device(data["device"]["config"])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(exc.user_message if isinstance(exc, DeviceDiscoveryError) else str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
