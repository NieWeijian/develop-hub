from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .config import Settings
from .db import SessionLocal
from .events import EventBroker
from .models import (
    AgentSession,
    AttemptStatus,
    ExecutionAttempt,
    JobEvent,
    StageRun,
)


class RunnerError(RuntimeError):
    pass


READ_CHUNK_BYTES = 64 * 1024
MAX_JSONL_EVENT_BYTES = 8 * 1024 * 1024
MAX_NORMALIZED_STRING_CHARS = 16_000
MAX_NORMALIZED_LIST_ITEMS = 100
MAX_NORMALIZED_DICT_ITEMS = 200


@dataclass(frozen=True)
class RunnerResult:
    attempt_id: str
    thread_id: str | None
    result: dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _message_from_event(payload: dict[str, Any]) -> str | None:
    item = payload.get("item")
    if isinstance(item, dict):
        for key in ("text", "message", "command"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _error_message(value)
            if nested:
                return nested
    return None


def _error_message(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        return _error_message(parsed) or stripped
    if isinstance(value, dict):
        for key in ("error", "message", "detail"):
            nested = _error_message(value.get(key))
            if nested:
                return nested
    return None


def _error_message_from_event(payload: dict[str, Any]) -> str | None:
    if payload.get("type") not in {"error", "turn.failed"}:
        return None
    return _error_message(payload)


def _thread_id_from_event(payload: dict[str, Any]) -> str | None:
    for key in ("thread_id", "threadId", "session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _truncate_text(value: str, limit: int = MAX_NORMALIZED_STRING_CHARS) -> str:
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}\n… [truncated {omitted} characters; complete content is in the raw JSONL artifact]"


def _normalize_event_value(value: Any, depth: int = 0) -> Any:
    """Bound event data stored in SQLite/SSE while retaining complete raw JSONL on disk."""
    if depth >= 12:
        return "[nested value truncated; complete content is in the raw JSONL artifact]"
    if isinstance(value, str):
        return _truncate_text(value)
    if isinstance(value, list):
        normalized = [_normalize_event_value(item, depth + 1) for item in value[:MAX_NORMALIZED_LIST_ITEMS]]
        if len(value) > MAX_NORMALIZED_LIST_ITEMS:
            normalized.append(
                f"[truncated {len(value) - MAX_NORMALIZED_LIST_ITEMS} list items; complete content is in the raw JSONL artifact]"
            )
        return normalized
    if isinstance(value, dict):
        items = list(value.items())
        normalized = {
            str(key): _normalize_event_value(item, depth + 1)
            for key, item in items[:MAX_NORMALIZED_DICT_ITEMS]
        }
        if len(items) > MAX_NORMALIZED_DICT_ITEMS:
            normalized["_truncated_items"] = len(items) - MAX_NORMALIZED_DICT_ITEMS
        return normalized
    return value


class CodexRunner:
    def __init__(self, settings: Settings, broker: EventBroker) -> None:
        self.settings = settings
        self.broker = broker
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._process_lock = asyncio.Lock()

    async def version(self) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.codex_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            return stdout.decode(errors="replace").strip() or None
        except (OSError, asyncio.TimeoutError):
            return None

    async def execute(
        self,
        *,
        stage_id: str,
        session_id: str,
        prompt: str,
        output_schema: Path,
        resume: bool,
        sandbox_mode: str = "workspace-write",
    ) -> RunnerResult:
        async with self._semaphore:
            return await self._execute_locked(
                stage_id=stage_id,
                session_id=session_id,
                prompt=prompt,
                output_schema=output_schema,
                resume=resume,
                sandbox_mode=sandbox_mode,
            )

    async def _execute_locked(
        self,
        *,
        stage_id: str,
        session_id: str,
        prompt: str,
        output_schema: Path,
        resume: bool,
        sandbox_mode: str,
    ) -> RunnerResult:
        with SessionLocal() as db:
            stage = db.get(StageRun, stage_id)
            session = db.get(AgentSession, session_id)
            if not stage or not session:
                raise RunnerError("Stage or agent session no longer exists")
            if resume and not session.codex_thread_id:
                raise RunnerError("Cannot resume before a Codex thread id has been recorded")

            ordinal = db.scalar(
                select(func.count(ExecutionAttempt.id)).where(ExecutionAttempt.stage_id == stage_id)
            ) or 0
            attempt = ExecutionAttempt(
                stage_id=stage_id,
                session_id=session_id,
                ordinal=ordinal + 1,
                resumed=resume,
                status=AttemptStatus.QUEUED,
            )
            db.add(attempt)
            db.commit()
            db.refresh(attempt)
            attempt_id = attempt.id
            run_id = stage.run_id
            cwd = Path(session.cwd)
            thread_id = session.codex_thread_id

        attempt_dir = self.settings.artifact_root / run_id / attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = attempt_dir / "events.jsonl"
        stderr_path = attempt_dir / "stderr.log"
        final_path = attempt_dir / "final-message.json"
        prompt_path = attempt_dir / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        if resume:
            command = [self.settings.codex_bin, "exec", "resume", "--json"]
            command.extend(["--config", f'sandbox_mode="{sandbox_mode}"'])
            if self.settings.codex_model:
                command.extend(["--model", self.settings.codex_model])
            command.extend([
                "--output-schema", str(output_schema),
                "--output-last-message", str(final_path),
                str(thread_id),
                "-",
            ])
        else:
            command = [
                self.settings.codex_bin,
                "exec",
                "--json",
                "--color", "never",
                "--sandbox", sandbox_mode,
                "--cd", str(cwd),
            ]
            if self.settings.codex_model:
                command.extend(["--model", self.settings.codex_model])
            command.extend([
                "--output-schema", str(output_schema),
                "--output-last-message", str(final_path),
                "-",
            ])

        with SessionLocal() as db:
            attempt = db.get(ExecutionAttempt, attempt_id)
            assert attempt
            attempt.command = command
            attempt.status = AttemptStatus.RUNNING
            attempt.started_at = _now()
            db.commit()

        sequence = 0
        stderr_tail: list[str] = []
        structured_error: str | None = None
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            async with self._process_lock:
                self._processes[attempt_id] = process

            with SessionLocal() as db:
                attempt = db.get(ExecutionAttempt, attempt_id)
                assert attempt
                attempt.pid = process.pid
                db.commit()

            assert process.stdin and process.stdout and process.stderr
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            async def consume_stdout() -> None:
                nonlocal sequence, structured_error, thread_id
                pending = bytearray()
                oversized_bytes = 0

                async def persist_line(raw_line: bytes | None, original_bytes: int | None = None) -> None:
                    nonlocal sequence, structured_error, thread_id
                    if raw_line is None:
                        payload: dict[str, Any] = {
                            "type": "codex.event_too_large",
                            "original_bytes": original_bytes or 0,
                            "raw_artifact": str(jsonl_path),
                            "message": "Codex emitted an oversized JSONL event; the complete event remains in the raw artifact.",
                        }
                    else:
                        line = raw_line.decode(errors="replace").rstrip("\r")
                        if not line:
                            return
                        try:
                            parsed = json.loads(line)
                            payload = parsed if isinstance(parsed, dict) else {"value": parsed}
                        except json.JSONDecodeError:
                            payload = {"raw": line}

                    sequence += 1
                    event_type = str(payload.get("type", "codex.raw"))
                    event_message = _message_from_event(payload)
                    observed_error = _error_message_from_event(payload)
                    if observed_error:
                        structured_error = observed_error
                    observed_thread_id = _thread_id_from_event(payload)
                    if observed_thread_id and observed_thread_id != thread_id:
                        thread_id = observed_thread_id
                        with SessionLocal() as db:
                            agent_session = db.get(AgentSession, session_id)
                            if agent_session:
                                agent_session.codex_thread_id = observed_thread_id
                                agent_session.last_used_at = _now()
                                db.commit()

                    normalized = _normalize_event_value(payload)
                    assert isinstance(normalized, dict)
                    await self._persist_event(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        sequence=sequence,
                        event_type=event_type,
                        message=_truncate_text(event_message) if event_message else None,
                        payload=normalized,
                    )

                with jsonl_path.open("ab") as raw_log:
                    while chunk := await process.stdout.read(READ_CHUNK_BYTES):
                        raw_log.write(chunk)
                        raw_log.flush()
                        start = 0
                        while True:
                            newline = chunk.find(b"\n", start)
                            part = chunk[start:] if newline < 0 else chunk[start:newline]
                            if oversized_bytes:
                                oversized_bytes += len(part)
                            elif len(pending) + len(part) <= MAX_JSONL_EVENT_BYTES:
                                pending.extend(part)
                            else:
                                oversized_bytes = len(pending) + len(part)
                                pending.clear()

                            if newline < 0:
                                break
                            if oversized_bytes:
                                await persist_line(None, oversized_bytes)
                                oversized_bytes = 0
                            else:
                                await persist_line(bytes(pending))
                                pending.clear()
                            start = newline + 1

                    if oversized_bytes:
                        await persist_line(None, oversized_bytes)
                    elif pending:
                        await persist_line(bytes(pending))

            async def consume_stderr() -> None:
                tail = bytearray()
                with stderr_path.open("ab") as error_log:
                    while chunk := await process.stderr.read(READ_CHUNK_BYTES):
                        error_log.write(chunk)
                        error_log.flush()
                        tail.extend(chunk)
                        del tail[:-128_000]
                stderr_tail.extend(tail.decode(errors="replace").splitlines()[-30:])

            await asyncio.gather(consume_stdout(), consume_stderr())
            exit_code = await process.wait()
            if exit_code != 0:
                raise RunnerError(structured_error or "\n".join(stderr_tail) or f"Codex exited with code {exit_code}")

            result = self._load_final_result(final_path)
            with SessionLocal() as db:
                attempt = db.get(ExecutionAttempt, attempt_id)
                agent_session = db.get(AgentSession, session_id)
                assert attempt
                attempt.status = AttemptStatus.SUCCEEDED
                attempt.exit_code = exit_code
                attempt.final_message = result
                attempt.finished_at = _now()
                if agent_session:
                    agent_session.last_used_at = _now()
                db.commit()
            return RunnerResult(attempt_id=attempt_id, thread_id=thread_id, result=result)
        except asyncio.CancelledError:
            await self._terminate_process(process)
            self._finish_failed_attempt(attempt_id, AttemptStatus.CANCELLED, "Execution cancelled")
            raise
        except Exception as exc:
            await self._terminate_process(process)
            exit_code = process.returncode if process else None
            self._finish_failed_attempt(attempt_id, AttemptStatus.FAILED, str(exc), exit_code)
            raise
        finally:
            async with self._process_lock:
                self._processes.pop(attempt_id, None)

    async def _persist_event(
        self,
        *,
        run_id: str,
        attempt_id: str,
        sequence: int,
        event_type: str,
        message: str | None,
        payload: dict[str, Any],
    ) -> None:
        with SessionLocal() as db:
            event = JobEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                sequence=sequence,
                event_type=event_type,
                message=message,
                payload=payload,
            )
            db.add(event)
            db.commit()
            db.refresh(event)
            serialized = {
                "id": event.id,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "sequence": sequence,
                "event_type": event_type,
                "message": message,
                "payload": payload,
                "created_at": event.created_at.isoformat(),
            }
        await self.broker.publish(run_id, serialized)

    def _load_final_result(self, final_path: Path) -> dict[str, Any]:
        if not final_path.exists():
            return {"status": "completed", "summary": "Codex completed without a final message file"}
        content = final_path.read_text(encoding="utf-8").strip()
        if not content:
            return {"status": "completed", "summary": "Codex returned an empty final message"}
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"text": content}

    def _finish_failed_attempt(
        self,
        attempt_id: str,
        status: AttemptStatus,
        error: str,
        exit_code: int | None = None,
    ) -> None:
        with SessionLocal() as db:
            attempt = db.get(ExecutionAttempt, attempt_id)
            if attempt:
                attempt.status = status
                attempt.exit_code = exit_code
                attempt.error = error
                attempt.finished_at = _now()
                db.commit()

    async def cancel_run(self, run_id: str) -> int:
        with SessionLocal() as db:
            attempt_ids = list(
                db.scalars(
                    select(ExecutionAttempt.id)
                    .join(StageRun)
                    .where(StageRun.run_id == run_id, ExecutionAttempt.status == AttemptStatus.RUNNING)
                )
            )
        cancelled = 0
        async with self._process_lock:
            processes = [(attempt_id, self._processes.get(attempt_id)) for attempt_id in attempt_ids]
        for _attempt_id, process in processes:
            if process and process.returncode is None:
                await self._terminate_process(process)
                cancelled += 1
        return cancelled

    async def _terminate_process(self, process: asyncio.subprocess.Process | None) -> None:
        if not process or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGINT)
            await asyncio.wait_for(process.wait(), timeout=self.settings.cancel_grace_seconds)
        except (ProcessLookupError, asyncio.TimeoutError):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()

    @property
    def active_jobs(self) -> int:
        return len(self._processes)
