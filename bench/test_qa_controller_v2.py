import importlib.util
import json
from pathlib import Path
import shutil
import stat
import sys

BENCH = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("qa_controller_v2", BENCH / "qa_controller_v2.py")
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)

def executable(path, code):
    path.write_text("#!/usr/bin/env python3\n" + code)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

def test_demo_job_contract(tmp_path, monkeypatch):
    root = tmp_path / "bench"
    root.mkdir()
    shutil.copy2(BENCH / "demo_case_common.py", root)
    (root / "test-bins").mkdir()
    executable(root / "test-bins/uvc_host_ctl", """
import sys
print('rgb+depth' if sys.argv[-1]=='get-device-mode' else 'R3211101263200064')
""")
    executable(root / "test-bins/uvc_host_stream", """
import signal,time,sys
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print('[RGB] 5s: fps=9.00 recv=45 drops_by_seq=0(+0) total=45',flush=True)
print('[DEPTH] 5s: fps=9.00 recv=45 drops_by_seq=0(+0) total=45',flush=True)
print('[RGB] 5s: fps=9.20 recv=46 drops_by_seq=0(+0) total=91',flush=True)
print('[DEPTH] 5s: fps=9.20 recv=46 drops_by_seq=0(+0) total=91',flush=True)
time.sleep(30)
""")
    entries = []
    for ident in ("test-demo1", "test-demo2"):
        src = BENCH / "test-cases" / ident / "1.0.1"
        dest = root / "test-cases" / ident / "1.0.1"
        dest.mkdir(parents=True)
        for name in ("case.json", "run.py"):
            shutil.copy2(src / name, dest / name)
        entries.append({"id": ident, "version": "1.0.1",
                        "manifest_sha256": controller.sha(dest / "case.json"),
                        "entry_sha256": controller.sha(dest / "run.py"),
                        "parameters": {"duration_sec": 11, "min_fps": 8} if ident == "test-demo2" else {}})
    job = root / "jobs" / "TEST-DEMO-CONTRACT"
    job.mkdir(parents=True)
    plan = {"schema_version": 3, "round_id": job.name, "execution": "case_demo",
            "target": {}, "cases": entries,
            "runtime_sha256": controller.sha(root / "demo_case_common.py"),
            "tools": [{"id": ident, "sha256": controller.sha(root / "test-bins" / ident)}
                      for ident in ("uvc_host_ctl", "uvc_host_stream")]}
    controller.atomic(job / "test-run.json", plan)
    controller.atomic(job / "READY", {"config_sha256": controller.sha(job / "test-run.json")})
    monkeypatch.setattr(controller, "ROOT", root)
    monkeypatch.setattr(controller, "discover", lambda _: {"video": "/dev/video-test", "usb_path": "/sys/test", "name": "RMSL321_RGB"})
    controller.run_one(job)
    report = json.loads((job / "result.json").read_text())
    assert report["outcome"] == "passed"
    assert [item["outcome"] for item in report["cases"]] == ["passed", "passed"]
    assert report["cases"][0]["metrics"]["sn"] == "R3211101263200064"
    assert len(report["cases"][1]["metrics"]["streams"]["RGB"]) == 2
    assert report["cases"][1]["files"]
    revision = report["revision"]
    controller.run_one(job)
    assert json.loads((job / "result.json").read_text())["revision"] == revision
    (root / "test-bins/uvc_host_ctl").write_text("changed")
    another = root / "jobs" / "TEST-DEMO-TAMPER"
    shutil.copytree(job, another, ignore=shutil.ignore_patterns("result.json", "events.jsonl", ".lock", "cases", "logs"))
    plan["round_id"] = another.name
    controller.atomic(another / "test-run.json", plan)
    controller.atomic(another / "READY", {"config_sha256": controller.sha(another / "test-run.json")})
    controller.run_one(another)
    assert json.loads((another / "result.json").read_text())["outcome"] == "error"
