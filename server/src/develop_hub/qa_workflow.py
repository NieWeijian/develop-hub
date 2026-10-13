from __future__ import annotations

import asyncio
import json
import shlex
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .config import Settings
from .db import SessionLocal
from .models import FirmwareArtifact, QaCaseExecution, QaCaseStatus, QaRun, QaRunStatus, TestCaseRisk
from .qa_runner import QaRunnerError, RemoteQaRunner


def _now() -> datetime:
    return datetime.now(timezone.utc)


class QaWorkflowError(RuntimeError):
    pass


class QaWorkflow:
    def __init__(self, settings: Settings, runner: RemoteQaRunner) -> None:
        self.settings = settings
        self.runner = runner
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def recover_interrupted_runs(self) -> None:
        transient = {QaRunStatus.PREPARING, QaRunStatus.TESTING, QaRunStatus.REPORTING}
        with SessionLocal() as db:
            runs = list(db.scalars(select(QaRun).where(QaRun.status.in_(transient))))
            for run in runs:
                run.status = QaRunStatus.BLOCKED
                run.last_error = "175 Server restarted during remote QA execution; inspect evidence before retrying."
            db.commit()

    async def start(self, qa_run_id: str) -> None:
        async with self._lock:
            current = self._tasks.get(qa_run_id)
            if current and not current.done():
                raise QaWorkflowError("This QA run already has an active task")
            task = asyncio.create_task(self._pipeline(qa_run_id), name=f"qa-run-{qa_run_id}")
            self._tasks[qa_run_id] = task
            task.add_done_callback(lambda _task: self._tasks.pop(qa_run_id, None))

    async def _pipeline(self, qa_run_id: str) -> None:
        try:
            with SessionLocal() as db:
                run = db.get(QaRun, qa_run_id)
                if not run or run.status != QaRunStatus.CREATED:
                    raise QaWorkflowError("QA run is not ready to start")
                run.status = QaRunStatus.PREPARING
                run.started_at = _now()
                run.last_error = None
                handoff = run.handoff
                db.commit()

            workspace = await self.runner.provision(qa_run_id, handoff)
            remote_firmware_path = await self._transfer_firmware_if_requested(qa_run_id)
            with SessionLocal() as db:
                run = db.get(QaRun, qa_run_id)
                assert run
                run.remote_workspace = workspace
                if remote_firmware_path:
                    run.firmware_path = remote_firmware_path
                    updated_handoff = dict(run.handoff)
                    firmware_handoff = dict(updated_handoff.get("firmware") or {})
                    firmware_handoff["remote_image_directory"] = remote_firmware_path
                    updated_handoff["firmware"] = firmware_handoff
                    run.handoff = updated_handoff
                run.status = QaRunStatus.TESTING
                db.commit()

            ota_result: dict[str, Any] | None = await self._run_ota_if_requested(qa_run_id, workspace)
            if ota_result is not None and not ota_result.get("passed"):
                raise QaWorkflowError("3588 OTA command failed; test cases were not started")
            case_results = await self._run_cases(qa_run_id, workspace)

            with SessionLocal() as db:
                run = db.get(QaRun, qa_run_id)
                assert run
                run.status = QaRunStatus.REPORTING
                db.commit()
                prompt = self._report_prompt(run.handoff, ota_result, case_results)

            thread_id, report, raw_jsonl = await self.runner.generate_report(
                qa_run_id,
                prompt,
                self.settings.schema_root / "qa-report.schema.json",
            )
            commands_passed = all(item["passed"] for item in case_results) and (
                ota_result is None or ota_result.get("passed") is True
            )
            report["control_result"] = {
                "commands_passed": commands_passed,
                "ota_executed": ota_result is not None,
                "case_count": len(case_results),
            }
            artifact_dir = self.settings.artifact_root / "qa" / qa_run_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "agent-events.jsonl").write_text(raw_jsonl, encoding="utf-8")
            (artifact_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

            with SessionLocal() as db:
                run = db.get(QaRun, qa_run_id)
                assert run
                run.codex_thread_id = thread_id
                run.report = report
                run.status = QaRunStatus.AWAITING_APPROVAL
                db.commit()
        except Exception as exc:
            with SessionLocal() as db:
                run = db.get(QaRun, qa_run_id)
                if run:
                    run.status = QaRunStatus.BLOCKED
                    run.last_error = str(exc)
                    db.commit()

    async def _transfer_firmware_if_requested(self, qa_run_id: str) -> str | None:
        with SessionLocal() as db:
            run = db.get(QaRun, qa_run_id)
            assert run
            if not run.perform_ota:
                return None
            artifact_ids = list(run.firmware_artifact_ids)
            artifacts_by_id = {
                item.id: item
                for item in db.scalars(
                    select(FirmwareArtifact).where(
                        FirmwareArtifact.id.in_(artifact_ids),
                        FirmwareArtifact.coding_run_id == run.coding_run_id,
                    )
                )
            }
            if len(artifacts_by_id) != len(artifact_ids):
                raise QaWorkflowError("One or more selected firmware artifacts are unavailable")
            artifacts = [artifacts_by_id[item] for item in artifact_ids]
            payload = [
                {
                    "partition": artifact.partition,
                    "local_path": artifact.local_path,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in artifacts
            ]
        return await self.runner.upload_firmware(qa_run_id, payload)

    async def _run_ota_if_requested(self, qa_run_id: str, workspace: str) -> dict[str, Any] | None:
        with SessionLocal() as db:
            run = db.scalar(
                select(QaRun)
                .options(selectinload(QaRun.case_executions))
                .where(QaRun.id == qa_run_id)
            )
            assert run
            if not run.perform_ota:
                return None
            if not run.firmware_path or not run.ota_partitions:
                raise QaWorkflowError("OTA requires a remote firmware image directory and partitions")
            command = " ".join(
                [
                    shlex.quote(self.settings.qa_ota_tool),
                    "--image-dir", shlex.quote(run.firmware_path),
                    "--parts", shlex.quote(",".join(run.ota_partitions)),
                    "--json",
                ]
            )
        result = await self.runner.run_test_case(workspace, command, 600)
        return {
            "passed": result.exit_code == 0,
            "exit_code": result.exit_code,
            "stdout_tail": result.stdout[-12000:],
            "stderr_tail": result.stderr[-12000:],
        }

    async def _run_cases(self, qa_run_id: str, workspace: str) -> list[dict[str, Any]]:
        with SessionLocal() as db:
            run = db.scalar(
                select(QaRun)
                .options(selectinload(QaRun.case_executions).selectinload(QaCaseExecution.test_case))
                .where(QaRun.id == qa_run_id)
            )
            assert run
            execution_ids = [item.id for item in sorted(run.case_executions, key=lambda item: item.ordinal)]

        results: list[dict[str, Any]] = []
        for execution_id in execution_ids:
            with SessionLocal() as db:
                execution = db.scalar(
                    select(QaCaseExecution)
                    .options(selectinload(QaCaseExecution.test_case))
                    .where(QaCaseExecution.id == execution_id)
                )
                assert execution
                execution.status = QaCaseStatus.RUNNING
                execution.started_at = _now()
                command = execution.command
                timeout = execution.timeout_seconds
                expected = execution.expected_exit_code
                risk_level = execution.risk_level
                name = execution.case_name
                code = execution.case_code
                db.commit()
            if risk_level == TestCaseRisk.FIRMWARE_WRITE:
                with SessionLocal() as db:
                    qa_run = db.get(QaRun, qa_run_id)
                    assert qa_run
                    if not qa_run.perform_ota:
                        raise QaWorkflowError("Firmware-write test case lacks explicit OTA approval")
            try:
                result = await self.runner.run_test_case(workspace, command, timeout)
                passed = result.exit_code == expected
                exit_code = result.exit_code
                stdout_tail = result.stdout[-12000:]
                stderr_tail = result.stderr[-12000:]
            except QaRunnerError as exc:
                passed = False
                exit_code = None
                stdout_tail = ""
                stderr_tail = str(exc)
            with SessionLocal() as db:
                execution = db.get(QaCaseExecution, execution_id)
                assert execution
                execution.status = QaCaseStatus.PASSED if passed else QaCaseStatus.FAILED
                execution.exit_code = exit_code
                execution.stdout_tail = stdout_tail
                execution.stderr_tail = stderr_tail
                execution.finished_at = _now()
                db.commit()
            results.append(
                {
                    "code": code,
                    "name": name,
                    "command": command,
                    "passed": passed,
                    "exit_code": exit_code,
                    "stdout_tail": stdout_tail,
                    "stderr_tail": stderr_tail,
                }
            )
        return results

    @staticmethod
    def _report_prompt(
        handoff: dict[str, Any],
        ota_result: dict[str, Any] | None,
        case_results: list[dict[str, Any]],
    ) -> str:
        return f"""You are the QA reporting agent on the 3588 execution node.

Coding handoff:
{json.dumps(handoff, ensure_ascii=False, indent=2)}

Controlled OTA result (null means OTA was intentionally skipped):
{json.dumps(ota_result, ensure_ascii=False, indent=2)}

Controlled test-case results:
{json.dumps(case_results, ensure_ascii=False, indent=2)}

Analyze the supplied evidence and write a concise QA report. Do not claim that a failed command passed. Do not run firmware update commands or repeat the test commands; execution has already been controlled by the 175 Server. Return only the JSON object required by the output schema.
"""

    def decide(self, qa_run_id: str, approved: bool, comment: str | None) -> None:
        with SessionLocal() as db:
            run = db.get(QaRun, qa_run_id)
            if not run:
                raise QaWorkflowError("QA run not found")
            if run.status != QaRunStatus.AWAITING_APPROVAL:
                raise QaWorkflowError("QA run is not awaiting approval")
            commands_passed = bool((run.report or {}).get("control_result", {}).get("commands_passed"))
            if approved and not commands_passed:
                raise QaWorkflowError("Cannot approve QA while controlled test commands are failing")
            if approved and (run.report or {}).get("status") != "ready":
                raise QaWorkflowError("Cannot approve QA while the QA report is blocked")
            run.status = QaRunStatus.PASSED if approved else QaRunStatus.FAILED
            run.completed_at = _now()
            if comment:
                report = dict(run.report or {})
                report["operator_comment"] = comment
                run.report = report
            db.commit()
