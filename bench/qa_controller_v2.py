#!/usr/bin/env python3
"""Develop Hub QA v2: durable job receiver and versioned case executor."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

FINAL = {"finished", "blocked", "interrupted", "rejected"}

class PreflightBlocked(RuntimeError):
    pass
ROOT = Path(__file__).resolve().parent

def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def atomic(path, value):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)

def child_of(path, parent):
    return path == parent or parent in path.parents

def usb_parent(video):
    node = (Path("/sys/class/video4linux") / video.name / "device").resolve()
    for item in (node, *node.parents):
        if (item / "idVendor").exists() and (item / "idProduct").exists():
            return item
    return None

def discover(expected_path=""):
    found = []
    for node in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            name = (node / "name").read_text().strip()
            if not re.search(r"RMSL32[12]_RGB$", name):
                continue
            if (node / "index").exists() and (node / "index").read_text().strip() != "0":
                continue
            usb = usb_parent(node)
            if not usb or (usb / "idVendor").read_text().strip().lower() != "2207":
                continue
            location = str(usb)
            if expected_path and location != expected_path:
                continue
            found.append({"video": "/dev/" + node.name, "usb_path": location,
                          "name": name, "pid": (usb / "idProduct").read_text().strip(),
                          "serial": (usb / "serial").read_text().strip() if (usb / "serial").exists() else ""})
        except (OSError, ValueError):
            continue
    if len(found) != 1:
        raise PreflightBlocked("Expected exactly one matching UVC module, found " + str(len(found)))
    return found[0]

class Job:
    def __init__(self, path):
        self.path = path
        self.result = {"schema_version": 3, "round_id": path.name, "state": "running",
                       "phase": "validating", "revision": 0, "cases": [], "started_at": stamp()}
        (path / "logs").mkdir(exist_ok=True)
    def save(self, phase=None, **changes):
        if phase:
            self.result["phase"] = phase
        self.result.update(changes)
        self.result["revision"] += 1
        self.result["updated_at"] = self.result["heartbeat_at"] = stamp()
        atomic(self.path / "result.json", self.result)
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({key: self.result.get(key) for key in
                ("revision", "updated_at", "phase", "state", "outcome", "message")},
                ensure_ascii=False) + "\n")
    def validate(self):
        marker, config_path = self.path / "READY", self.path / "test-run.json"
        if marker.is_symlink() or config_path.is_symlink():
            raise ValueError("Symlink job input rejected")
        ready = json.loads(marker.read_text())
        if sha(config_path) != ready.get("config_sha256"):
            raise ValueError("Task configuration SHA-256 mismatch")
        plan = json.loads(config_path.read_text())
        if plan.get("schema_version") != 3 or plan.get("round_id") != self.path.name:
            raise ValueError("Unsupported or mismatched task")
        if plan.get("execution") != "case_demo":
            raise ValueError("Unsupported execution mode")
        cases = plan.get("cases")
        if not isinstance(cases, list) or not cases or len(cases) != len({x.get("id") for x in cases}):
            raise ValueError("Case list missing or duplicate")
        self.result["config_sha256"] = ready["config_sha256"]
        self.result["cases"] = [{"id": item["id"], "version": item["version"], "state": "queued"} for item in cases]
        tools = {}
        for item in plan.get("tools", []):
            ident = item.get("id")
            if ident not in ("uvc_host_ctl", "uvc_host_stream") or ident in tools:
                raise ValueError("Unknown or duplicate tool")
            binary = (ROOT / "test-bins" / ident).resolve()
            if not child_of(binary, ROOT / "test-bins") or not binary.is_file() or sha(binary) != item.get("sha256"):
                raise ValueError("Tool package missing or SHA-256 mismatch: " + str(ident))
            tools[ident] = str(binary)
        if set(tools) != {"uvc_host_ctl", "uvc_host_stream"}:
            raise ValueError("Demo requires ctl and stream tools")
        runtime = ROOT / "demo_case_common.py"
        if not runtime.is_file() or sha(runtime) != plan.get("runtime_sha256"):
            raise ValueError("Case runtime SHA-256 mismatch")
        packages = {}
        for item in cases:
            ident, version = item["id"], item["version"]
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", ident) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise ValueError("Unsafe Case identity")
            folder = (ROOT / "test-cases" / ident / version).resolve()
            if not child_of(folder, ROOT / "test-cases") or not folder.is_dir():
                raise ValueError("Case package missing")
            manifest, entry = folder / "case.json", folder / "run.py"
            if sha(manifest) != item.get("manifest_sha256") or sha(entry) != item.get("entry_sha256"):
                raise ValueError("Case package SHA-256 mismatch: " + ident)
            definition = json.loads(manifest.read_text())
            if definition.get("id") != ident or definition.get("version") != version:
                raise ValueError("Case identity mismatch")
            packages[ident] = (definition, entry)
        return plan, tools, packages
    def execute(self):
        plan, tools, packages = self.validate()
        self.save("preflight", message="Checking bench and module")
        if not all(os.access(path, os.X_OK) for path in tools.values()):
            raise ValueError("Tool executable permission missing")
        device = discover(plan.get("target", {}).get("usb_path", ""))
        self.result["device"] = device
        self.save("running_cases", message="Running demo cases")
        all_pass = True
        for index, item in enumerate(plan["cases"]):
            ident = item["id"]
            definition, entry = packages[ident]
            out = self.path / "cases" / ident / "attempt-001"
            out.mkdir(parents=True, exist_ok=False)
            data = {"round_id": self.path.name, "case_id": ident, "definition": definition,
                    "device": device, "tools": tools, "parameters": item.get("parameters", {})}
            atomic(out / "case-input.json", data)
            self.result["cases"][index]["state"] = "running"
            self.save(message="Running " + ident, current_case=ident)
            log = self.path / "logs" / (ident + ".log")
            try:
                with log.open("wb") as output:
                    proc = subprocess.run([sys.executable, str(entry), "--input", str(out / "case-input.json"),
                                           "--output-dir", str(out)], stdout=output, stderr=subprocess.STDOUT,
                                          timeout=definition["timeout_seconds"], cwd=str(folder_of(entry)))
                result = json.loads((out / "result.json").read_text())
                valid = result.get("id") == ident and result.get("state") == "finished" and isinstance(result.get("assertions"), list)
                outcome = result.get("outcome") if valid and proc.returncode == (0 if result.get("outcome") == "passed" else 1) else "error"
                if outcome not in ("passed", "failed", "error", "blocked", "inconclusive"):
                    outcome = "error"
                summary = {"id": ident, "version": item["version"], "state": "finished",
                           "outcome": outcome, "assertions": result.get("assertions", []) if valid else [],
                           "metrics": result.get("metrics", {}) if valid else {},
                           "files": result.get("files", []) if valid else [],
                           "message": result.get("message", "") if valid else "Malformed Case result"}
            except Exception as exc:
                summary = {"id": ident, "version": item["version"], "state": "finished",
                           "outcome": "error", "assertions": [], "metrics": {}, "message": str(exc)}
            self.result["cases"][index] = summary
            all_pass = all_pass and summary["outcome"] == "passed" and bool(summary["assertions"]) and all(
                a.get("passed") is True for a in summary["assertions"])
            self.save(message="Finished " + ident, current_case=None)
        self.save("complete", state="finished", outcome="passed" if all_pass else "failed",
                  message="Demo cases completed", completed_at=stamp())

def folder_of(path):
    return path.parent

def run_one(path):
    path = path.resolve()
    with (path / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        old_path = path / "result.json"
        if old_path.exists():
            old = json.loads(old_path.read_text())
            if old.get("state") in FINAL:
                return
            old.update(state="interrupted", outcome="error", phase="interrupted",
                       message="Controller restarted during execution; no automatic retry",
                       completed_at=stamp(), revision=old.get("revision", 0) + 1)
            atomic(old_path, old)
            return
        job = Job(path)
        try:
            job.save(message="Validating task package")
            job.execute()
        except PreflightBlocked as exc:
            for item in job.result["cases"]:
                if item["state"] == "queued":
                    item.update(state="blocked", outcome="blocked")
            job.save("complete", state="blocked", outcome="blocked", message=str(exc), completed_at=stamp())
        except Exception as exc:
            for item in job.result["cases"]:
                if item["state"] == "queued":
                    item.update(state="blocked", outcome="blocked")
                elif item["state"] == "running":
                    item.update(state="finished", outcome="error")
            job.save("complete", state="finished", outcome="error", message=str(exc), completed_at=stamp())

def serve(jobs):
    jobs = jobs.resolve()
    with (jobs / ".controller-v2.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            for path in sorted(jobs.iterdir()):
                if path.is_symlink() or not path.is_dir() or not (path / "READY").is_file():
                    continue
                result = path / "result.json"
                if not result.exists():
                    run_one(path)
                else:
                    try:
                        if json.loads(result.read_text()).get("state") not in FINAL:
                            run_one(path)
                    except (OSError, json.JSONDecodeError):
                        pass
            time.sleep(2)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "run"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    (serve if args.mode == "serve" else run_one)(args.path)
