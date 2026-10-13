#!/usr/bin/env python3
"""Dispatch one immutable QA demo job from 175 to the 3588 controller."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time

BENCH = Path(__file__).resolve().parent
HOST = "linaro@172.16.38.137"
REMOTE = "/home/linaro/develop-hub-qa"
KEY = BENCH.parent / "var/ssh/qa-3588-ed25519"
SSH = ["ssh", "-i", str(KEY), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", HOST]
SCP = ["scp", "-i", str(KEY), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def remote(command):
    proc = subprocess.run([*SSH, command], capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise RuntimeError("3588 SSH failed: " + proc.stderr[-500:])
    return proc.stdout

def copy(local, target):
    proc = subprocess.run([*SCP, str(local), HOST + ":" + target], capture_output=True, text=True, timeout=180)
    if proc.returncode:
        raise RuntimeError("3588 transfer failed: " + proc.stderr[-500:])

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--usb-path", default="", help="Optional exact /sys USB location")
    parser.add_argument("--expected-sn", default="")
    parser.add_argument("--duration", type=int, default=12)
    parser.add_argument("--min-fps", type=float, default=1.0)
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    if args.expected_sn and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", args.expected_sn):
        parser.error("invalid expected SN")
    if not 11 <= args.duration <= 120 or not 0 < args.min_fps <= 30:
        parser.error("invalid duration or minimum fps")
    job_id = "TEST-DEMO-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + str(time.time_ns())[-6:]
    job = REMOTE + "/demo-jobs/" + job_id
    binary_hashes = {}
    for tool in ("uvc_host_ctl", "uvc_host_stream"):
        output = remote("sha256sum " + shlex.quote(REMOTE + "/test-bins/" + tool))
        binary_hashes[tool] = output.split()[0]
    cases = []
    for ident in ("test-demo1", "test-demo2"):
        folder = BENCH / "test-cases" / ident / "1.0.1"
        params = {"expected_sn": args.expected_sn} if ident == "test-demo1" and args.expected_sn else (
            {"duration_sec": args.duration, "min_fps": args.min_fps} if ident == "test-demo2" else {})
        cases.append({"id": ident, "version": "1.0.1", "manifest_sha256": sha(folder / "case.json"),
                      "entry_sha256": sha(folder / "run.py"), "parameters": params})
    plan = {"schema_version": 3, "round_id": job_id,
            "execution": "case_demo",
            "target": {"usb_path": args.usb_path}, "cases": cases,
            "runtime_sha256": sha(BENCH / "demo_case_common.py"),
            "tools": [{"id": key, "sha256": value} for key, value in binary_hashes.items()]}
    with tempfile.TemporaryDirectory(prefix="qa-demo-") as temp:
        config = Path(temp) / "test-run.json"
        config.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
        digest = sha(config)
        remote("mkdir -m 700 -p " + shlex.quote(job))
        copy(config, job + "/test-run.json")
        check = remote("sha256sum " + shlex.quote(job + "/test-run.json"))
        hashes = [line.split()[0] for line in check.splitlines()]
        if hashes != [digest]:
            raise RuntimeError("Remote task digest differs from source")
        marker = Path(temp) / "READY"
        marker.write_text(json.dumps({"config_sha256": digest}))
        copy(marker, job + "/READY.tmp")
        remote("mv " + shlex.quote(job + "/READY.tmp") + " " + shlex.quote(job + "/READY"))
    print(json.dumps({"round_id": job_id, "remote_path": job, "config_sha256": digest,
                      "execution": "case_demo"}, ensure_ascii=False), flush=True)
    if args.wait:
        while True:
            try:
                payload = remote("if test -f " + shlex.quote(job + "/result.json") + "; then cat " + shlex.quote(job + "/result.json") + "; fi")
                if not payload:
                    time.sleep(2)
                    continue
                report = json.loads(payload)
                if report.get("round_id") != job_id or report.get("config_sha256") != digest:
                    raise RuntimeError("Controller result identity mismatch")
                if report.get("state") in ("finished", "blocked", "interrupted", "rejected"):
                    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
                    return 0 if report.get("outcome") == "passed" else 1
            except RuntimeError as exc:
                if "No such file" not in str(exc):
                    raise
            time.sleep(2)

if __name__ == "__main__":
    raise SystemExit(main())
