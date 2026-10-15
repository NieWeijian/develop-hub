#!/usr/bin/env python3
"""Read-only version assertion using a node-registered and digest-pinned tool."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def now():
    return datetime.now(timezone.utc).isoformat()


def usb_identity(video: str, usb_path: str) -> None:
    if not re.fullmatch(r"/dev/video[0-9]+", video):
        raise RuntimeError("Inspection did not supply a valid video node")
    node = Path("/sys/class/video4linux") / Path(video).name
    if "RMSL321_RGB" not in (node / "name").read_text() or (node / "index").read_text().strip() != "0":
        raise RuntimeError("Video node no longer identifies the RGB interface")
    resolved = (node / "device").resolve(strict=True)
    usb = next((parent for parent in (resolved, *resolved.parents) if (parent / "idVendor").exists()), None)
    if usb is None or str(usb.resolve()) != str(Path(usb_path).resolve()):
        raise RuntimeError("Physical USB identity changed after inspection")
    if (usb / "idVendor").read_text().strip().lower() != "2207" or (usb / "idProduct").read_text().strip().lower() != "001a":
        raise RuntimeError("Unexpected USB product at the selected node")


def execute(data: dict, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    result = {"result_schema": "rk-case-result/1", "run_id": data.get("run_id"),
              "binding_id": data.get("binding_id"), "case_revision_id": data.get("case_revision_id"),
              "state": "completed", "verdict": "error", "started_at": now(), "assertions": [],
              "recovery": {"state": "not_required"}, "attachments": [],
              "identity_level": "device_reported_version", "simulation": False}

    def assertion(ident, name, expected, actual, passed):
        result["assertions"].append({"id": ident, "name": name, "expected": expected, "actual": actual, "passed": bool(passed)})

    try:
        if data.get("input_schema") != "rk-case-input/1" or any(not isinstance(data.get(key), str) or not data[key] for key in ("run_id", "binding_id", "case_revision_id")):
            raise RuntimeError("Unsupported or incomplete input identity")
        device = data["device"]
        config, observed = device["config"], device["observation"]
        serial, video, physical = config["serial"], observed["video"], observed["usb_path"]
        if not isinstance(serial, str) or not re.fullmatch(r"[0-9a-fA-F]{8,64}", serial) or not int(serial, 16):
            raise RuntimeError("Invalid registered CPU serial")
        if observed.get("simulation") is True or observed.get("serial", "").lower() != serial.lower():
            raise RuntimeError("Inspection identity does not match the registered real module")
        if config.get("usb_path") and str(Path(config["usb_path"]).resolve()) != str(Path(physical).resolve()):
            raise RuntimeError("Inspection USB path differs from the registered module")
        expected = data["parameters"]["expected_version"]
        if not isinstance(expected, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,119}", expected):
            raise RuntimeError("Expected version must be a complete version token without whitespace")
        tool = Path(config["ctl"]["path"])
        frozen_hash = config["ctl"]["sha256"]
        if not tool.is_absolute() or tool.is_symlink() or not tool.is_file() or not re.fullmatch(r"[0-9a-fA-F]{64}", frozen_hash):
            raise RuntimeError("Register an absolute regular control tool and SHA-256")
        valid = hashlib.sha256(tool.read_bytes()).hexdigest() == frozen_hash.lower()
        assertion("tool-integrity", "测试工具完整性", "与登记的工具一致", "校验通过" if valid else "校验失败", valid)
        if not valid:
            raise RuntimeError("Registered tool integrity check failed")

        def call(command):
            usb_identity(video, physical)
            proc = subprocess.run([str(tool), "--device", video, command], capture_output=True, text=True, timeout=15,
                                  env={**os.environ, "RMSL_XU_TRACE": str(output / "protocol.jsonl"), "RMSL_TRACE_STEP": command})
            for name, text in (("stdout.log", proc.stdout), ("stderr.log", proc.stderr)):
                with (output / name).open("a", encoding="utf-8") as stream:
                    stream.write(f"[{command}]\n{text}\n")
            if proc.returncode or not proc.stdout.strip() or len(proc.stdout) > 4096:
                raise RuntimeError(f"{command} failed or returned invalid data (exit {proc.returncode})")
            return proc.stdout.strip()

        before = call("get-cpu-serial")
        if before.lower() != serial.lower():
            assertion("device-identity", "被测模组身份", serial, before, False)
            raise RuntimeError("Target module serial differs before version read")
        version = call("get-version")
        after = call("get-cpu-serial")
        usb_identity(video, physical)
        identity_matches = after.lower() == serial.lower()
        assertion("device-identity", "被测模组身份", serial, after, identity_matches)
        if not identity_matches:
            raise RuntimeError("Target module identity changed during version read")
        matched = re.search(r"(?<![A-Za-z0-9._+\-])" + re.escape(expected) + r"(?![A-Za-z0-9._+\-])", version) is not None
        assertion("firmware-version", "设备自报版本", expected, version, matched)
        result.update(verdict="passed" if matched else "failed", firmware_version=version)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result.update(verdict="error", message=str(exc))
    result["completed_at"] = now()
    for name in ("stdout.log", "stderr.log", "protocol.jsonl"):
        if (output / name).is_file():
            result["attachments"].append({"path": name, "name": name})
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    result = execute(data, Path(args.output_dir).resolve())
    return 0 if result["verdict"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
