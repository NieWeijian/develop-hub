"""Shared result/evidence contract for QA demo cases."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import time

def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def atomic(path, value):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
    temp.replace(path)

class Case:
    def __init__(self, data, out):
        self.data, self.out = data, out
        out.mkdir(parents=True, exist_ok=True)
        self.result = {"schema_version": 1, "id": data["case_id"],
                       "version": data["definition"]["version"], "state": "running",
                       "outcome": None, "started_at": stamp(), "assertions": [], "steps": [],
                       "metrics": {}, "files": []}
        self.save()
    def save(self):
        atomic(self.out / "result.json", self.result)
    def check(self, name, expected, actual, passed, unit=None):
        assertion = {"name": name, "expected": expected, "actual": actual, "passed": bool(passed)}
        if unit:
            assertion["unit"] = unit
        self.result["assertions"].append(assertion)
        self.save()
        if not passed:
            raise AssertionError(name)
    def call(self, name, argv, timeout=30):
        begin = time.monotonic()
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        (self.out / (name + ".stdout.log")).write_text(stdout)
        (self.out / (name + ".stderr.log")).write_text(stderr)
        self.result["steps"].append({"name": name, "argv": argv, "exit_code": proc.returncode,
                                      "elapsed_ms": round((time.monotonic() - begin) * 1000, 2)})
        self.save()
        if proc.returncode != 0:
            raise RuntimeError(name + " exited " + str(proc.returncode) + ": " + stderr[-500:])
        return stdout.strip()
    def finish(self, outcome, message=""):
        files = []
        for path in sorted(self.out.iterdir()):
            if path.is_file() and path.name not in ("result.json", "case-input.json"):
                files.append({"path": path.name, "size_bytes": path.stat().st_size,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.result["files"] = files
        self.result.update(state="finished", outcome=outcome, message=message, completed_at=stamp())
        self.save()
        return 0 if outcome == "passed" else 1

def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    return json.loads(args.input.read_text()), args.output_dir
