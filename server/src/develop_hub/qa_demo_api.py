"""API for first QA middleware demo rounds on the registered 3588 bench."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, get_db
from .models import QaDemoRun, TestBench, new_id

router = APIRouter(prefix="/api/qa/demo-runs", tags=["qa-demo"])
BENCH_ROOT = "/home/linaro/develop-hub-qa"
BENCH_HOST = "172.16.38.137"
CASE_CODES = {"test-demo1", "test-demo2"}

def utcnow():
    return datetime.now(timezone.utc)

def payload(row):
    return {"id": row.id, "bench_id": row.bench_id, "remote_job_id": row.remote_job_id,
            "status": row.status, "request_snapshot": row.request_snapshot,
            "dispatch_result": row.dispatch_result, "controller_result": row.controller_result,
            "last_error": row.last_error, "created_at": row.created_at,
            "completed_at": row.completed_at}

class DemoRunCreate(BaseModel):
    bench_id: str
    expected_sn: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,64}$")
    duration_sec: int = Field(default=12, ge=11, le=120)
    min_fps: float = Field(default=1.0, gt=0, le=30)

async def remote(command):
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-i", str(settings.qa_ssh_key), "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
        "linaro@" + BENCH_HOST, command,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("3588 SSH timed out")
    if proc.returncode:
        raise RuntimeError("3588 SSH failed: " + err.decode(errors="replace")[-300:])
    return out.decode(errors="replace")

async def refresh(row):
    if not row.remote_job_id or row.status in ("finished", "failed", "blocked", "error"):
        return
    if not re.fullmatch(r"TEST-DEMO-[A-Za-z0-9-]+", row.remote_job_id):
        raise RuntimeError("Invalid remote job ID")
    path = BENCH_ROOT + "/demo-jobs/" + row.remote_job_id + "/result.json"
    text = await remote("if test -f " + shlex.quote(path) + "; then cat " + shlex.quote(path) + "; fi")
    if not text:
        return
    report = json.loads(text)
    dispatch = row.dispatch_result or {}
    if report.get("round_id") != row.remote_job_id or report.get("config_sha256") != dispatch.get("config_sha256"):
        raise RuntimeError("Controller result identity mismatch")
    row.controller_result = report
    if report.get("state") in ("finished", "blocked", "interrupted", "rejected"):
        outcome = report.get("outcome")
        row.status = {"passed": "finished", "failed": "failed", "blocked": "blocked"}.get(outcome, "error")
        row.completed_at = utcnow()
        row.last_error = None if row.status == "finished" else report.get("message", "Controller reported failure")

async def dispatch(run_id):
    with SessionLocal() as db:
        row = db.get(QaDemoRun, run_id)
        request = dict(row.request_snapshot)
        row.status = "running"
        db.commit()
    script = Path(__file__).resolve().parents[3] / "bench" / "demo_dispatch.py"
    command = ["python3", str(script), "--wait", "--duration", str(request["duration_sec"]),
               "--min-fps", str(request["min_fps"])]
    if request["expected_sn"]:
        command.extend(["--expected-sn", request["expected_sn"]])
    try:
        proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
        delivery = json.loads(line)
        if not re.fullmatch(r"TEST-DEMO-[A-Za-z0-9-]+", delivery.get("round_id", "")):
            raise RuntimeError("Dispatcher returned invalid job ID")
        with SessionLocal() as db:
            row = db.get(QaDemoRun, run_id)
            row.remote_job_id = delivery["round_id"]
            row.dispatch_result = delivery
            db.commit()
        await proc.communicate()
        with SessionLocal() as db:
            row = db.get(QaDemoRun, run_id)
            await refresh(row)
            if row.status == "running" and proc.returncode:
                row.status = "error"
                row.last_error = "Dispatcher exited before Controller result"
                row.completed_at = utcnow()
            db.commit()
    except Exception as exc:
        with SessionLocal() as db:
            row = db.get(QaDemoRun, run_id)
            row.status = "error"
            row.last_error = str(exc)
            row.completed_at = utcnow()
            db.commit()

@router.post("")
async def create_demo_run(data: DemoRunCreate):
    raise HTTPException(status_code=410, detail='架构联调入口已停用；请从产品创建可追溯的测试轮次')

@router.get("")
async def list_demo_runs(db: Session = Depends(get_db)):
    rows = list(db.scalars(select(QaDemoRun).order_by(QaDemoRun.created_at.desc()).limit(30)))
    for row in rows:
        if row.status == "running" and row.remote_job_id:
            try:
                await refresh(row)
            except Exception as exc:
                row.last_error = str(exc)
    db.commit()
    return [payload(row) for row in rows]

@router.get("/{run_id}")
async def get_demo_run(run_id: str, db: Session = Depends(get_db)):
    row = db.get(QaDemoRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Demo run not found")
    if row.status == "running" and row.remote_job_id:
        try:
            await refresh(row)
        except Exception as exc:
            row.last_error = str(exc)
        db.commit()
    return payload(row)

@router.get("/{run_id}/cases/{code}")
async def get_demo_case(run_id: str, code: str, db: Session = Depends(get_db)):
    row = db.get(QaDemoRun, run_id)
    if not row or not row.remote_job_id or code not in CASE_CODES:
        raise HTTPException(status_code=404, detail="Case evidence not found")
    path = BENCH_ROOT + "/demo-jobs/" + row.remote_job_id + "/cases/" + code + "/attempt-001/result.json"
    text = await remote("if test -f " + shlex.quote(path) + "; then cat " + shlex.quote(path) + "; fi")
    if not text:
        raise HTTPException(status_code=404, detail="Case has not run")
    if len(text) > 1024 * 1024:
        raise HTTPException(status_code=413, detail="Case result too large")
    result = json.loads(text)
    if result.get("id") != code:
        raise HTTPException(status_code=422, detail="Case result identity mismatch")
    return result
