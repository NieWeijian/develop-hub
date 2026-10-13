from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .codex_runner import CodexRunner, RunnerError
from .config import Settings
from .db import SessionLocal
from .events import EventBroker
from .git_workspace import GitWorkspaceManager, WorkspaceError
from .models import (
    AgentSession,
    Approval,
    ApprovalStatus,
    AttemptStatus,
    CodingRun,
    ExecutionAttempt,
    FirmwareArtifact,
    JobEvent,
    QaPluginRelease,
    QaRun,
    QaRunStatus,
    Repository,
    RunStatus,
    StageKind,
    StageRun,
    StageStatus,
    WorktreeMode,
)
from .qa_plugins import QaPluginError, QaPluginRegistry


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowError(RuntimeError):
    pass


class CodingWorkflow:
    def __init__(
        self,
        settings: Settings,
        runner: CodexRunner,
        broker: EventBroker,
        qa_plugins: QaPluginRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.broker = broker
        self.workspaces = GitWorkspaceManager(settings.worktree_root)
        self.qa_plugins = qa_plugins
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_lock = asyncio.Lock()

    async def recover_interrupted_runs(self) -> None:
        transient = {
            RunStatus.PREPARING,
            RunStatus.PLANNING,
            RunStatus.IMPLEMENTING,
            RunStatus.BUILDING,
            RunStatus.VALIDATING,
            RunStatus.REPAIRING,
            RunStatus.REVIEWING,
            RunStatus.CANCELLING,
        }
        recovered: list[str] = []
        with SessionLocal() as db:
            runs = list(db.scalars(select(CodingRun).where(CodingRun.status.in_(transient))))
            for run in runs:
                run.status = RunStatus.BLOCKED
                run.last_error = "Server restarted during an active stage; inspect the worktree and resume explicitly."
                recovered.append(run.id)
            attempts = list(db.scalars(select(ExecutionAttempt).where(ExecutionAttempt.status == AttemptStatus.RUNNING)))
            for attempt in attempts:
                attempt.status = AttemptStatus.FAILED
                attempt.error = "Server restarted while this process was active"
                attempt.finished_at = _now()
            stages = list(db.scalars(select(StageRun).where(StageRun.status == StageStatus.RUNNING)))
            for stage in stages:
                stage.status = StageStatus.FAILED
                stage.finished_at = _now()
                if stage.kind == StageKind.DISCUSS:
                    previous = stage.result or {}
                    stage.result = {**previous, "status": "failed", "error": "Server restarted during inquiry"}
                elif stage.kind == StageKind.MERGE:
                    previous = stage.result or {}
                    stage.result = {**previous, "status": "failed", "error": "Server restarted during merge; inspect Git state before retrying"}
            review_sessions = list(
                db.scalars(
                    select(AgentSession)
                    .where(AgentSession.role == "review")
                    .order_by(
                        AgentSession.run_id,
                        AgentSession.last_used_at.desc(),
                        AgentSession.created_at.desc(),
                    )
                )
            )
            active_review_runs: set[str] = set()
            for session in review_sessions:
                session.active = session.run_id not in active_review_runs
                active_review_runs.add(session.run_id)
            db.commit()
        for run_id in recovered:
            await self.emit(run_id, "workflow.recovery_required", "服务重启后需要人工确认并恢复流程")

    async def start(self, run_id: str) -> None:
        await self._schedule(run_id, self._start_pipeline(run_id))

    async def _schedule(self, run_id: str, coroutine) -> None:  # type: ignore[no-untyped-def]
        async with self._task_lock:
            current = self._tasks.get(run_id)
            if current and not current.done():
                coroutine.close()
                raise WorkflowError("This run already has an active workflow task")
            task = asyncio.create_task(coroutine, name=f"coding-run-{run_id}")
            self._tasks[run_id] = task

            def cleanup(_task: asyncio.Task[None]) -> None:
                self._tasks.pop(run_id, None)

            task.add_done_callback(cleanup)

    async def _start_pipeline(self, run_id: str) -> None:
        try:
            await self._prepare(run_id)
            await self._plan(run_id)
        except asyncio.CancelledError:
            await self._mark_cancelled(run_id)
            raise
        except Exception as exc:
            await self._fail(run_id, str(exc))

    async def _prepare(self, run_id: str) -> None:
        stage = self._new_stage(run_id, StageKind.PREPARE)
        self._mark_stage_running(run_id, stage.id, RunStatus.PREPARING)
        await self.emit(run_id, "stage.started", "正在准备隔离 worktree", {"stage": StageKind.PREPARE.value})

        with SessionLocal() as db:
            run = db.scalar(
                select(CodingRun)
                .options(selectinload(CodingRun.repository))
                .where(CodingRun.id == run_id)
            )
            if not run:
                raise WorkflowError("Coding run not found")
            repository_path = run.repository.root_path
            source_branch = run.source_branch
            work_branch = run.work_branch
            worktree_mode = run.worktree_mode
            existing_worktree_path = run.worktree_path
            worktree_branch = run.worktree_branch
            worktree_initial_branch = run.worktree_initial_branch

        if worktree_mode == WorktreeMode.EXISTING:
            if not existing_worktree_path:
                raise WorkflowError("Existing worktree path is missing")
            selected = await self.workspaces.validate_existing_worktree(
                repository_path,
                existing_worktree_path,
                expected_branch=worktree_branch,
            )
            destination = selected.path
            ready_message = "已确认使用现有 worktree"
        elif worktree_mode == WorktreeMode.REUSE_NEW_BRANCH:
            if not existing_worktree_path or not worktree_initial_branch:
                raise WorkflowError("Reusable worktree path or initial branch is missing")
            destination = await self.workspaces.create_branch_in_existing_worktree(
                repository_path=repository_path,
                worktree_path=existing_worktree_path,
                initial_branch=worktree_initial_branch,
                source_branch=source_branch,
                work_branch=worktree_branch,
            )
            ready_message = "已在现有 worktree 创建并切换到任务分支"
        else:
            destination = await self.workspaces.ensure_worktree(
                repository_path=repository_path,
                source_branch=source_branch,
                work_branch=work_branch,
                run_id=run_id,
            )
            ready_message = "隔离 worktree 已准备完成"

        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            persisted_stage = db.get(StageRun, stage.id)
            assert run and persisted_stage
            run.worktree_path = str(destination)
            persisted_stage.status = StageStatus.SUCCEEDED
            persisted_stage.result = {
                "worktree_path": str(destination),
                "worktree_branch": worktree_branch,
                "worktree_mode": worktree_mode.value,
            }
            persisted_stage.finished_at = _now()
            db.commit()
        await self.emit(
            run_id,
            "workspace.ready",
            ready_message,
            {"path": str(destination), "branch": worktree_branch, "mode": worktree_mode.value},
        )

    async def _plan(self, run_id: str) -> None:
        stage = self._new_stage(run_id, StageKind.PLAN)
        self._mark_stage_running(run_id, stage.id, RunStatus.PLANNING)
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            assert run and run.worktree_path
            session = AgentSession(run_id=run_id, node_id="175-coding", role="coding", cwd=run.worktree_path)
            db.add(session)
            db.commit()
            db.refresh(session)
            session_id = session.id
            prompt = self._plan_prompt(run)
            stage = db.get(StageRun, stage.id)
            assert stage
            stage.prompt = prompt
            db.commit()

        await self.emit(run_id, "stage.started", "Codex 正在调查需求并形成设计方案", {"stage": StageKind.PLAN.value})
        result = await self.runner.execute(
            stage_id=stage.id,
            session_id=session_id,
            prompt=prompt,
            output_schema=self.settings.schema_root / "plan-result.schema.json",
            resume=False,
            sandbox_mode="workspace-write",
        )
        await self._store_plan_for_approval(run_id, stage.id, result.result)

    async def _revise_plan_pipeline(self, run_id: str, previous_stage_id: str, feedback: str) -> None:
        try:
            await self._revise_plan(run_id, previous_stage_id, feedback)
        except asyncio.CancelledError:
            await self._mark_cancelled(run_id)
            raise
        except Exception as exc:
            await self._fail(run_id, str(exc))

    async def _revise_plan(self, run_id: str, previous_stage_id: str, feedback: str) -> None:
        stage = self._new_stage(run_id, StageKind.PLAN)
        self._mark_stage_running(run_id, stage.id, RunStatus.PLANNING)
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            previous_stage = db.get(StageRun, previous_stage_id)
            session = db.scalar(
                select(AgentSession).where(AgentSession.run_id == run_id, AgentSession.role == "coding")
            )
            assert run and previous_stage and session and run.worktree_path
            prompt = self._plan_revision_prompt(run, previous_stage.result or {}, feedback)
            persisted_stage = db.get(StageRun, stage.id)
            assert persisted_stage
            persisted_stage.prompt = prompt
            db.commit()
            session_id = session.id

        await self.emit(
            run_id,
            "plan.revision_started",
            "Codex 正在根据用户反馈修订设计方案",
            {"stage": StageKind.PLAN.value, "previous_stage_id": previous_stage_id},
        )
        result = await self.runner.execute(
            stage_id=stage.id,
            session_id=session_id,
            prompt=prompt,
            output_schema=self.settings.schema_root / "plan-result.schema.json",
            resume=True,
            sandbox_mode="workspace-write",
        )
        await self._store_plan_for_approval(run_id, stage.id, result.result)

    async def _store_plan_for_approval(
        self,
        run_id: str,
        stage_id: str,
        plan_result: dict[str, Any],
    ) -> None:
        summary = str(plan_result.get("summary", "设计方案已准备完成"))
        needs_information = plan_result.get("status") == "blocked"

        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            persisted_stage = db.get(StageRun, stage_id)
            assert run and persisted_stage
            persisted_stage.result = plan_result
            persisted_stage.status = StageStatus.AWAITING_APPROVAL
            persisted_stage.finished_at = _now()
            run.status = RunStatus.AWAITING_PLAN_APPROVAL
            run.current_stage = StageKind.PLAN
            run.last_error = None
            approval = Approval(
                run_id=run_id,
                stage_id=stage_id,
                kind="plan",
                summary=summary,
            )
            db.add(approval)
            db.commit()
        await self.emit(
            run_id,
            "approval.requested",
            "设计方案需要补充信息" if needs_information else "设计方案等待批准",
            {"kind": "plan", "summary": summary, "needs_information": needs_information},
        )

    async def decide_approval(
        self,
        run_id: str,
        approval_id: str,
        approved: bool,
        decided_by: str,
        comment: str | None,
    ) -> None:
        plugin_release_ids: list[str] = []
        if approved:
            with SessionLocal() as db:
                pending = db.get(Approval, approval_id)
                if pending and pending.run_id == run_id and pending.kind == "completion":
                    artifact_count = db.scalar(
                        select(func.count(FirmwareArtifact.id)).where(FirmwareArtifact.coding_run_id == run_id)
                    ) or 0
                    if artifact_count == 0:
                        raise WorkflowError(
                            "Completion approval requires at least one Server-verified firmware artifact"
                        )
        if approved and self.qa_plugins:
            with SessionLocal() as db:
                pending = db.get(Approval, approval_id)
                if pending and pending.run_id == run_id and pending.kind == "completion":
                    try:
                        plugin_release_ids = self.qa_plugins.discover_from_coding_run(run_id)
                    except QaPluginError as exc:
                        raise WorkflowError(f"QA plugin registration failed: {exc}") from exc
        with SessionLocal() as db:
            approval = db.get(Approval, approval_id)
            run = db.get(CodingRun, run_id)
            if not approval or approval.run_id != run_id or not run:
                raise WorkflowError("Approval not found")
            if approval.status != ApprovalStatus.PENDING:
                raise WorkflowError("Approval has already been decided")
            approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            approval.decided_at = _now()
            approval.decided_by = decided_by
            approval.comment = comment
            stage = db.get(StageRun, approval.stage_id)
            assert stage
            kind = approval.kind
            if not approved:
                stage.status = StageStatus.FAILED
                run.status = RunStatus.BLOCKED
                run.last_error = comment or f"{kind} approval rejected"
            elif kind == "plan":
                stage.status = StageStatus.SUCCEEDED
            elif kind == "completion":
                stage.status = StageStatus.SUCCEEDED
                run.status = RunStatus.COMPLETED
                run.current_stage = StageKind.COMPLETE
                run.completed_at = _now()
                completion = self._create_stage_in_session(db, run_id, StageKind.COMPLETE)
                completion.status = StageStatus.SUCCEEDED
                completion.started_at = _now()
                completion.finished_at = _now()
                completion.result = {"approved_by": decided_by, "comment": comment}
            db.commit()

        await self.emit(
            run_id,
            "approval.decided",
            "审批已通过" if approved else "审批已拒绝，流程暂停",
            {"kind": kind, "approved": approved, "comment_supplied": bool(comment)},
        )
        if approved and kind == "plan":
            await self._schedule(run_id, self._implementation_pipeline(run_id, comment))
        elif approved and kind == "completion":
            if plugin_release_ids:
                await self.emit(
                    run_id,
                    "qa_plugins.registered",
                    f"已登记 {len(plugin_release_ids)} 个待安装 QA 测试插件",
                    {"release_ids": plugin_release_ids},
                )
            await self.emit(run_id, "workflow.completed", "Coding Run 已完成并归档")

    async def submit_plan_feedback(
        self,
        run_id: str,
        approval_id: str,
        message: str,
        submitted_by: str,
    ) -> None:
        feedback = message.strip()
        if not feedback:
            raise WorkflowError("Feedback must not be blank")
        with SessionLocal() as db:
            approval = db.get(Approval, approval_id)
            run = db.get(CodingRun, run_id)
            if not approval or approval.run_id != run_id or not run:
                raise WorkflowError("Approval not found")
            if approval.kind != "plan":
                raise WorkflowError("Feedback revisions are only supported at the plan gate")
            if approval.status != ApprovalStatus.PENDING:
                raise WorkflowError("Approval has already been decided")
            if run.status != RunStatus.AWAITING_PLAN_APPROVAL:
                raise WorkflowError(f"Plan feedback cannot be submitted from {run.status.value}")
            approval.status = ApprovalStatus.REJECTED
            approval.decided_at = _now()
            approval.decided_by = submitted_by
            approval.comment = feedback
            stage = db.get(StageRun, approval.stage_id)
            assert stage
            stage.status = StageStatus.SKIPPED
            run.status = RunStatus.PLANNING
            run.current_stage = StageKind.PLAN
            run.last_error = None
            previous_stage_id = stage.id
            db.commit()

        await self.emit(
            run_id,
            "approval.feedback_submitted",
            "用户已提交方案补充信息，准备重新生成方案",
            {
                "kind": "plan",
                "approval_id": approval_id,
                "feedback_length": len(feedback),
                "submitted_by": submitted_by,
            },
        )
        await self._schedule(run_id, self._revise_plan_pipeline(run_id, previous_stage_id, feedback))

    async def _implementation_pipeline(self, run_id: str, operator_comment: str | None = None) -> None:
        try:
            implementation = await self._implement(run_id, operator_comment)
            await self._validate_review_build_pipeline(run_id, implementation)
        except asyncio.CancelledError:
            await self._mark_cancelled(run_id)
            raise
        except Exception as exc:
            await self._fail(run_id, str(exc))

    async def _validate_review_build_pipeline(
        self,
        run_id: str,
        delivery: dict[str, Any],
    ) -> None:
        latest_delivery = delivery
        while True:
            validation = await self._validate(run_id)
            if not validation["passed"]:
                if not await self._can_repair(run_id):
                    raise WorkflowError("Validation failed after the configured repair attempts")
                latest_delivery = await self._repair(run_id, "validation", validation)
                continue

            review = await self._review(run_id)
            if review.get("status") == "ready":
                break
            if review.get("status") == "blocked":
                raise WorkflowError(self._blocker_message(review, "Review was blocked"))
            if not await self._can_repair(run_id):
                raise WorkflowError("Review still requires changes after the configured repair attempts")
            latest_delivery = await self._repair(run_id, "review", review)

        await self._build(run_id, self._declared_firmware_partitions(latest_delivery, run_id))
        await self._request_completion_approval(run_id)

    async def resume_with_operator_input(
        self,
        run_id: str,
        message: str,
        submitted_by: str,
    ) -> None:
        operator_message = message.strip()
        if not operator_message:
            raise WorkflowError("Recovery message must not be blank")
        async with self._task_lock:
            current = self._tasks.get(run_id)
            if current and not current.done():
                raise WorkflowError("This run already has an active workflow or Coding Agent interaction")

        with SessionLocal() as db:
            run = db.scalar(
                select(CodingRun)
                .options(selectinload(CodingRun.repository))
                .where(CodingRun.id == run_id)
            )
            if not run:
                raise WorkflowError("Coding run not found")
            if run.status not in {RunStatus.FAILED, RunStatus.BLOCKED}:
                raise WorkflowError(f"Operator recovery cannot start from {run.status.value}")
            if not run.worktree_path:
                raise WorkflowError("Operator recovery requires an existing worktree")
            pending_approval = db.scalar(
                select(Approval).where(
                    Approval.run_id == run_id,
                    Approval.status == ApprovalStatus.PENDING,
                )
            )
            if pending_approval:
                raise WorkflowError("Resolve the pending workflow approval before recovery")
            session = db.scalar(
                select(AgentSession).where(
                    AgentSession.run_id == run_id,
                    AgentSession.role == "coding",
                )
            )
            if not session or not session.codex_thread_id:
                raise WorkflowError("Operator recovery requires the original coding thread")
            repository_path = run.repository.root_path
            worktree_path = run.worktree_path
            worktree_branch = run.worktree_branch
            previous_error = run.last_error or "No previous workflow error was recorded."

        try:
            await self.workspaces.validate_existing_worktree(
                repository_path,
                worktree_path,
                expected_branch=worktree_branch,
            )
        except WorkspaceError as exc:
            raise WorkflowError(f"Coding Agent worktree is no longer valid: {exc}") from exc

        await self.emit(
            run_id,
            "recovery.requested",
            "用户已提交处理信息，准备恢复原 Coding Agent 和受控流程",
            {
                "submitted_by": submitted_by,
                "message_length": len(operator_message),
                "previous_error": previous_error,
            },
        )
        await self._schedule(
            run_id,
            self._operator_recovery_pipeline(
                run_id=run_id,
                message=operator_message,
                submitted_by=submitted_by,
                previous_error=previous_error,
            ),
        )

    async def _operator_recovery_pipeline(
        self,
        *,
        run_id: str,
        message: str,
        submitted_by: str,
        previous_error: str,
    ) -> None:
        try:
            recovery = await self._operator_repair(
                run_id=run_id,
                message=message,
                submitted_by=submitted_by,
                previous_error=previous_error,
            )
            await self._validate_review_build_pipeline(run_id, recovery)
        except asyncio.CancelledError:
            await self._mark_cancelled(run_id)
            raise
        except Exception as exc:
            await self._fail(run_id, str(exc))

    async def ask_coding_agent(
        self,
        run_id: str,
        message: str,
        qa_run_id: str | None,
        submitted_by: str,
    ) -> None:
        question = message.strip()
        if not question:
            raise WorkflowError("Inquiry must not be blank")
        async with self._task_lock:
            current = self._tasks.get(run_id)
            if current and not current.done():
                raise WorkflowError("This run already has an active workflow or Coding Agent inquiry")

        with SessionLocal() as db:
            run = db.scalar(
                select(CodingRun)
                .options(selectinload(CodingRun.repository))
                .where(CodingRun.id == run_id)
            )
            if not run:
                raise WorkflowError("Coding run not found")
            if run.status not in {
                RunStatus.AWAITING_COMPLETION_APPROVAL,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.BLOCKED,
            }:
                raise WorkflowError(f"Coding Agent inquiry cannot start from {run.status.value}")
            if not run.worktree_path:
                raise WorkflowError("Coding Agent inquiry requires an existing worktree")
            session = db.scalar(
                select(AgentSession).where(
                    AgentSession.run_id == run_id,
                    AgentSession.role == "coding",
                )
            )
            if not session or not session.codex_thread_id:
                raise WorkflowError("Coding Agent inquiry requires the original coding thread")

            qa_context: dict[str, Any] | None = None
            if qa_run_id:
                qa_run = db.scalar(
                    select(QaRun)
                    .options(selectinload(QaRun.case_executions))
                    .where(QaRun.id == qa_run_id)
                )
                if not qa_run:
                    raise WorkflowError("QA run not found")
                if qa_run.coding_run_id != run_id:
                    raise WorkflowError("QA run does not belong to this coding run")
                qa_context = {
                    "id": qa_run.id,
                    "display_number": qa_run.display_number,
                    "status": qa_run.status.value,
                    "last_error": qa_run.last_error,
                    "report": qa_run.report,
                    "case_results": [
                        {
                            "code": item.case_code,
                            "name": item.case_name,
                            "status": item.status.value,
                            "exit_code": item.exit_code,
                            "stdout_tail": item.stdout_tail,
                            "stderr_tail": item.stderr_tail,
                        }
                        for item in sorted(qa_run.case_executions, key=lambda item: item.ordinal)
                    ],
                }
            repository_path = run.repository.root_path
            worktree_path = run.worktree_path
            worktree_branch = run.worktree_branch
            session_id = session.id
            prompt = self._inquiry_prompt(run, question, qa_context)

        try:
            await self.workspaces.validate_existing_worktree(
                repository_path,
                worktree_path,
                expected_branch=worktree_branch,
            )
        except WorkspaceError as exc:
            raise WorkflowError(f"Coding Agent worktree is no longer valid: {exc}") from exc

        stage = self._new_stage(run_id, StageKind.DISCUSS)
        with SessionLocal() as db:
            persisted_stage = db.get(StageRun, stage.id)
            assert persisted_stage
            persisted_stage.prompt = prompt
            persisted_stage.status = StageStatus.RUNNING
            persisted_stage.started_at = _now()
            persisted_stage.result = {
                "interaction": "inquiry",
                "question": question,
                "answer": None,
                "qa_run_id": qa_run_id,
                "submitted_by": submitted_by,
                "status": "running",
            }
            db.commit()

        await self.emit(
            run_id,
            "inquiry.started",
            "用户正在咨询原 Coding Agent",
            {"stage_id": stage.id, "qa_run_id": qa_run_id, "submitted_by": submitted_by},
        )
        try:
            await self._schedule(
                run_id,
                self._coding_inquiry_pipeline(
                    run_id=run_id,
                    stage_id=stage.id,
                    session_id=session_id,
                    prompt=prompt,
                    question=question,
                    qa_run_id=qa_run_id,
                    submitted_by=submitted_by,
                ),
            )
        except Exception:
            self._finish_stage(
                stage.id,
                {
                    "interaction": "inquiry",
                    "question": question,
                    "answer": None,
                    "qa_run_id": qa_run_id,
                    "submitted_by": submitted_by,
                    "status": "failed",
                    "error": "Unable to schedule Coding Agent inquiry",
                },
                succeeded=False,
            )
            raise

    async def _coding_inquiry_pipeline(
        self,
        *,
        run_id: str,
        stage_id: str,
        session_id: str,
        prompt: str,
        question: str,
        qa_run_id: str | None,
        submitted_by: str,
    ) -> None:
        try:
            result = await self.runner.execute(
                stage_id=stage_id,
                session_id=session_id,
                prompt=prompt,
                output_schema=self.settings.schema_root / "coding-inquiry-result.schema.json",
                resume=True,
                sandbox_mode="read-only",
            )
            payload = {
                **result.result,
                "interaction": "inquiry",
                "question": question,
                "qa_run_id": qa_run_id,
                "submitted_by": submitted_by,
            }
            self._finish_stage(stage_id, payload, succeeded=result.result.get("status") == "answered")
            await self.emit(
                run_id,
                "inquiry.completed",
                "Coding Agent 已回答用户问题",
                {"stage_id": stage_id, "qa_run_id": qa_run_id},
            )
        except asyncio.CancelledError:
            self._finish_stage(
                stage_id,
                {
                    "interaction": "inquiry",
                    "question": question,
                    "answer": None,
                    "qa_run_id": qa_run_id,
                    "submitted_by": submitted_by,
                    "status": "cancelled",
                },
                succeeded=False,
            )
            raise
        except Exception as exc:
            self._finish_stage(
                stage_id,
                {
                    "interaction": "inquiry",
                    "question": question,
                    "answer": None,
                    "qa_run_id": qa_run_id,
                    "submitted_by": submitted_by,
                    "status": "failed",
                    "error": str(exc),
                },
                succeeded=False,
            )
            await self.emit(
                run_id,
                "inquiry.failed",
                "Coding Agent 咨询失败",
                {"stage_id": stage_id, "qa_run_id": qa_run_id, "error": str(exc)},
            )

    async def request_merge(
        self,
        *,
        run_id: str,
        qa_run_id: str,
        target_branch: str | None,
        approved_by: str,
    ) -> None:
        async with self._task_lock:
            current = self._tasks.get(run_id)
            if current and not current.done():
                raise WorkflowError("This run already has an active workflow or Coding Agent inquiry")

        with SessionLocal() as db:
            run = db.scalar(
                select(CodingRun)
                .options(selectinload(CodingRun.repository), selectinload(CodingRun.stages))
                .where(CodingRun.id == run_id)
            )
            if not run:
                raise WorkflowError("Coding run not found")
            if run.status != RunStatus.COMPLETED:
                raise WorkflowError("Only a completed Coding Run can be merged")
            qa_run = db.get(QaRun, qa_run_id)
            if not qa_run:
                raise WorkflowError("QA run not found")
            if qa_run.coding_run_id != run.id or qa_run.status != QaRunStatus.PASSED:
                raise WorkflowError("Merge requires an approved, passed QA run linked to this Coding Run")
            if not run.worktree_path:
                raise WorkflowError("Merge requires the Coding Run worktree")
            destination = (target_branch or run.repository.default_branch).strip()
            if destination != run.repository.default_branch:
                raise WorkflowError("The controlled merge target must be the repository default branch")
            if run.worktree_branch == destination:
                raise WorkflowError("Coding Run is already on the repository default branch")
            successful_merge = next(
                (
                    stage for stage in run.stages
                    if stage.kind == StageKind.MERGE and stage.status == StageStatus.SUCCEEDED
                ),
                None,
            )
            if successful_merge:
                raise WorkflowError("This Coding Run has already been merged")
            declared_files: list[str] = []
            for stage in sorted(run.stages, key=lambda item: item.sequence):
                if stage.kind not in {StageKind.IMPLEMENT, StageKind.REPAIR} or stage.status != StageStatus.SUCCEEDED:
                    continue
                changed_files = (stage.result or {}).get("changed_files", [])
                if isinstance(changed_files, list):
                    for path in changed_files:
                        if isinstance(path, str) and path not in declared_files:
                            declared_files.append(path)
            repository_path = run.repository.root_path
            worktree_path = run.worktree_path
            source_branch = run.worktree_branch
            commit_subject, commit_body = self._delivery_commit_message(run)

        try:
            await self.workspaces.validate_existing_worktree(
                repository_path, worktree_path, expected_branch=source_branch
            )
        except WorkspaceError as exc:
            raise WorkflowError(str(exc)) from exc

        stage = self._new_stage(run_id, StageKind.MERGE)
        with SessionLocal() as db:
            persisted = db.get(StageRun, stage.id)
            assert persisted
            persisted.status = StageStatus.RUNNING
            persisted.started_at = _now()
            persisted.result = {
                "status": "running",
                "qa_run_id": qa_run_id,
                "source_branch": source_branch,
                "target_branch": destination,
                "approved_by": approved_by,
                "declared_files": declared_files,
            }
            db.commit()
        await self.emit(
            run_id,
            "merge.started",
            f"已批准将 {source_branch} 合并到 {destination}",
            {"stage_id": stage.id, "qa_run_id": qa_run_id, "approved_by": approved_by},
        )
        try:
            await self._schedule(
                run_id,
                self._merge_pipeline(
                    run_id=run_id,
                    stage_id=stage.id,
                    qa_run_id=qa_run_id,
                    repository_path=repository_path,
                    worktree_path=worktree_path,
                    source_branch=source_branch,
                    target_branch=destination,
                    declared_files=declared_files,
                    approved_by=approved_by,
                    commit_subject=commit_subject,
                    commit_body=commit_body,
                ),
            )
        except Exception:
            self._finish_stage(stage.id, {**(stage.result or {}), "status": "failed", "error": "Unable to schedule merge"}, succeeded=False)
            raise

    async def _merge_pipeline(
        self,
        *,
        run_id: str,
        stage_id: str,
        qa_run_id: str,
        repository_path: str,
        worktree_path: str,
        source_branch: str,
        target_branch: str,
        declared_files: list[str],
        approved_by: str,
        commit_subject: str,
        commit_body: list[str],
    ) -> None:
        try:
            merged = await self.workspaces.merge_completed_branch(
                repository_path=repository_path,
                source_worktree_path=worktree_path,
                source_branch=source_branch,
                target_branch=target_branch,
                declared_files=declared_files,
                commit_subject=commit_subject,
                commit_body=commit_body,
            )
            payload = {
                "status": "succeeded",
                "qa_run_id": qa_run_id,
                "approved_by": approved_by,
                "source_branch": merged.source_branch,
                "target_branch": merged.target_branch,
                "source_commit": merged.source_commit,
                "target_commit_before": merged.target_commit_before,
                "merge_commit": merged.merge_commit,
                "committed_files": merged.committed_files,
                    "commit_created": merged.commit_created,
                    "commit_subject": commit_subject,
                    "commit_body": commit_body,
                "integration_worktree": merged.integration_worktree,
                "output": merged.output,
                "pushed": False,
            }
            self._finish_stage(stage_id, payload, succeeded=True)
            if merged.source_worktree_branch_after != source_branch:
                with SessionLocal() as db:
                    run = db.get(CodingRun, run_id)
                    assert run
                    run.worktree_branch = merged.source_worktree_branch_after
                    run.closed_at = _now()
                    db.commit()
            else:
                with SessionLocal() as db:
                    run = db.get(CodingRun, run_id)
                    assert run
                    run.closed_at = _now()
                    db.commit()
            await self.emit(
                run_id,
                "merge.completed",
                f"已合并到 {target_branch}，未推送远端",
                {"stage_id": stage_id, "merge_commit": merged.merge_commit, "pushed": False},
            )
        except Exception as exc:
            self._finish_stage(
                stage_id,
                {
                    "status": "failed",
                    "qa_run_id": qa_run_id,
                    "approved_by": approved_by,
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "error": str(exc),
                    "pushed": False,
                },
                succeeded=False,
            )
            await self.emit(
                run_id,
                "merge.failed",
                "主分支合并失败",
                {"stage_id": stage_id, "error": str(exc)},
            )

    @staticmethod
    def _delivery_commit_message(run: CodingRun) -> tuple[str, list[str]]:
        stages = sorted(
            (
                stage for stage in run.stages
                if stage.kind in {StageKind.IMPLEMENT, StageKind.REPAIR}
                and stage.status == StageStatus.SUCCEEDED
            ),
            key=lambda stage: stage.sequence,
            reverse=True,
        )
        proposal: dict[str, Any] | None = None
        for stage in stages:
            value = (stage.result or {}).get("commit")
            if isinstance(value, dict):
                proposal = value
                break
        if proposal is None:
            return (
                "src: update controlled delivery",
                [
                    "- apply files declared by the Coding Agent",
                    "- satisfy the approved implementation and QA gates",
                ],
            )
        subject = proposal.get("subject")
        body = proposal.get("body")
        subject_pattern = r"^[a-z0-9][a-z0-9_./-]*(?:: [a-z0-9][a-z0-9_./-]*)*: [a-z0-9][ -~]*$"
        if (
            not isinstance(subject, str)
            or len(subject) > 72
            or not subject.isascii()
            or not re.fullmatch(subject_pattern, subject)
        ):
            raise WorkflowError(
                "Coding Agent commit subject must be English ASCII and use '<file-area>: <change>' style"
            )
        if (
            not isinstance(body, list)
            or not 1 <= len(body) <= 6
            or any(
                not isinstance(item, str)
                or not item.isascii()
                or not item.startswith("- ")
                or len(item) > 100
                for item in body
            )
        ):
            raise WorkflowError("Coding Agent commit body must contain 1-6 concise English '- ' bullets")
        forbidden = ("change-id:", "signed-off-by:")
        if any(token in subject.lower() for token in forbidden) or any(
            token in item.lower() for item in body for token in forbidden
        ):
            raise WorkflowError("Commit metadata must not include Change-Id or Signed-off-by; Git hooks add them")
        return subject, list(body)

    async def retry_review(self, run_id: str) -> None:
        """Retry an infrastructure-failed review without repeating implementation."""
        async with self._task_lock:
            current = self._tasks.get(run_id)
            if current and not current.done():
                raise WorkflowError("This run already has an active workflow task")

        with SessionLocal() as db:
            run = db.scalar(
                select(CodingRun)
                .options(selectinload(CodingRun.repository), selectinload(CodingRun.stages))
                .where(CodingRun.id == run_id)
            )
            if not run:
                raise WorkflowError("Coding run not found")
            if run.status != RunStatus.FAILED:
                raise WorkflowError(f"Review can only be retried from failed; current status is {run.status.value}")
            if not run.worktree_path:
                raise WorkflowError("Review cannot be retried because the worktree path is missing")

            implementation_ok = any(
                stage.kind == StageKind.IMPLEMENT and stage.status == StageStatus.SUCCEEDED
                for stage in run.stages
            )
            validation_ok = any(
                stage.kind == StageKind.VALIDATE and stage.status == StageStatus.SUCCEEDED
                for stage in run.stages
            )
            reviews = sorted(
                (stage for stage in run.stages if stage.kind == StageKind.REVIEW),
                key=lambda stage: stage.sequence,
            )
            latest_review = reviews[-1] if reviews else None
            review_error = latest_review.result.get("error") if latest_review and latest_review.result else None
            if not implementation_ok or not validation_ok:
                raise WorkflowError("Review retry requires a completed implementation and successful validation")
            if not latest_review or latest_review.status != StageStatus.FAILED or not isinstance(review_error, str):
                raise WorkflowError("The latest review did not fail because of an execution error")

            active_attempts = db.scalar(
                select(func.count(ExecutionAttempt.id))
                .join(StageRun)
                .where(
                    StageRun.run_id == run_id,
                    ExecutionAttempt.status.in_({AttemptStatus.QUEUED, AttemptStatus.RUNNING}),
                )
            ) or 0
            pending_approvals = db.scalar(
                select(func.count(Approval.id)).where(
                    Approval.run_id == run_id,
                    Approval.status == ApprovalStatus.PENDING,
                )
            ) or 0
            if active_attempts or pending_approvals:
                raise WorkflowError("Review cannot be retried while an attempt or approval is active")
            repository_path = run.repository.root_path
            worktree_path = run.worktree_path
            worktree_branch = run.worktree_branch

        try:
            await self.workspaces.validate_existing_worktree(
                repository_path,
                worktree_path,
                expected_branch=worktree_branch,
            )
        except WorkspaceError as exc:
            raise WorkflowError(f"Review worktree is no longer valid: {exc}") from exc

        await self.emit(
            run_id,
            "review.retry_requested",
            "用户已请求重试独立代码审查",
            {"previous_error": review_error},
        )
        await self._schedule(run_id, self._retry_review_pipeline(run_id))

    async def _retry_review_pipeline(self, run_id: str) -> None:
        try:
            while True:
                review = await self._review(run_id)
                if review.get("status") == "ready":
                    break
                if review.get("status") == "blocked":
                    raise WorkflowError(self._blocker_message(review, "Review was blocked"))
                if not await self._can_repair(run_id):
                    raise WorkflowError("Review still requires changes after the configured repair attempts")
                repair = await self._repair(run_id, "review", review)
                await self._build(run_id, self._declared_firmware_partitions(repair, run_id))

                while True:
                    validation = await self._validate(run_id)
                    if validation["passed"]:
                        break
                    if not await self._can_repair(run_id):
                        raise WorkflowError("Validation failed after the configured repair attempts")
                    repair = await self._repair(run_id, "validation", validation)
                    await self._build(run_id, self._declared_firmware_partitions(repair, run_id))

            await self._request_completion_approval(run_id)
        except asyncio.CancelledError:
            await self._mark_cancelled(run_id)
            raise
        except Exception as exc:
            await self._fail(run_id, str(exc))

    async def _implement(self, run_id: str, operator_comment: str | None = None) -> dict[str, Any]:
        stage = self._new_stage(run_id, StageKind.IMPLEMENT)
        self._mark_stage_running(run_id, stage.id, RunStatus.IMPLEMENTING)
        with SessionLocal() as db:
            run = db.scalar(select(CodingRun).options(selectinload(CodingRun.stages)).where(CodingRun.id == run_id))
            session = db.scalar(select(AgentSession).where(AgentSession.run_id == run_id, AgentSession.role == "coding"))
            assert run and session
            plan = max((item for item in run.stages if item.kind == StageKind.PLAN), key=lambda item: item.sequence)
            prompt = self._implementation_prompt(run, plan.result or {}, operator_comment)
            persisted_stage = db.get(StageRun, stage.id)
            assert persisted_stage
            persisted_stage.prompt = prompt
            db.commit()
            session_id = session.id

        await self.emit(run_id, "stage.started", "Codex 已恢复原线程并开始编码", {"stage": StageKind.IMPLEMENT.value})
        result = await self.runner.execute(
            stage_id=stage.id,
            session_id=session_id,
            prompt=prompt,
            output_schema=self.settings.schema_root / "implementation-result.schema.json",
            resume=True,
        )
        if result.result.get("status") == "blocked":
            raise WorkflowError(self._blocker_message(result.result, "Implementation was blocked"))
        self._finish_stage(stage.id, result.result)
        await self.emit(run_id, "stage.completed", "编码阶段完成", {"stage": StageKind.IMPLEMENT.value})
        return result.result

    async def _build(self, run_id: str, partitions: list[str]) -> dict[str, Any]:
        stage = self._new_stage(run_id, StageKind.BUILD)
        self._mark_stage_running(run_id, stage.id, RunStatus.BUILDING)
        with SessionLocal() as db:
            run = db.scalar(
                select(CodingRun)
                .options(selectinload(CodingRun.repository))
                .where(CodingRun.id == run_id)
            )
            assert run and run.worktree_path
            repository = run.repository
            if not repository.build_root or not repository.firmware_image_root:
                raise WorkflowError("Repository must configure build root and firmware image root before controlled build")
            build_root = Path(repository.build_root).resolve()
            image_root = Path(repository.firmware_image_root).resolve()
            repository_root = Path(repository.root_path).resolve()
            worktree = Path(run.worktree_path).resolve()

        if worktree != repository_root:
            if build_root == repository_root:
                relative_image_root = image_root.relative_to(build_root)
                build_root = worktree
                image_root = worktree / relative_image_root
            else:
                raise WorkflowError(
                    "Controlled ./build.sh requires an existing SDK source worktree because this Git repository "
                    "is nested below the configured SDK build root"
                )
        if not build_root.is_dir() or not (build_root / "build.sh").is_file():
            raise WorkflowError(f"Configured build root does not contain ./build.sh: {build_root}")
        if image_root != build_root and build_root not in image_root.parents:
            raise WorkflowError("Firmware image root must stay inside the configured build root")
        if not partitions:
            raise WorkflowError("Coding Agent did not declare any firmware partitions for QA delivery")

        await self.emit(
            run_id,
            "build.started",
            "Server 正在执行固定构建命令 ./build.sh",
            {"cwd": str(build_root), "command": "./build.sh", "partitions": partitions},
        )
        build_started_at = _now().timestamp()
        result = await self._run_controlled_build_command(run_id, stage.id, build_root)
        if not result["passed"]:
            self._finish_stage(stage.id, result, succeeded=False)
            raise WorkflowError("Controlled ./build.sh failed; inspect the build artifact logs")

        artifacts = self._record_firmware_artifacts(
            run_id,
            partitions,
            image_root,
            source="controlled_build",
            minimum_mtime=build_started_at,
        )
        build_result = {**result, "artifacts": artifacts}
        self._finish_stage(stage.id, build_result)
        await self.emit(run_id, "build.completed", "固定构建通过，固件镜像已校验", build_result)
        return build_result

    async def _run_controlled_build_command(
        self,
        run_id: str,
        stage_id: str,
        build_root: Path,
    ) -> dict[str, Any]:
        with SessionLocal() as db:
            attempt = ExecutionAttempt(
                stage_id=stage_id,
                ordinal=1,
                executor="shell",
                status=AttemptStatus.RUNNING,
                command=["/bin/bash", "-lc", "./build.sh"],
                started_at=_now(),
            )
            db.add(attempt)
            db.commit()
            db.refresh(attempt)
            attempt_id = attempt.id

        artifact_dir = self.settings.artifact_root / run_id / attempt_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", "./build.sh",
            cwd=str(build_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.validation_timeout_seconds
            )
            timed_out = False
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            timed_out = True
        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")
        (artifact_dir / "stdout.log").write_text(stdout_text, encoding="utf-8")
        (artifact_dir / "stderr.log").write_text(stderr_text, encoding="utf-8")
        passed = process.returncode == 0 and not timed_out
        result = {
            "command": "./build.sh",
            "cwd": str(build_root),
            "passed": passed,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout_tail": stdout_text[-12_000:],
            "stderr_tail": stderr_text[-12_000:],
        }
        with SessionLocal() as db:
            attempt = db.get(ExecutionAttempt, attempt_id)
            assert attempt
            attempt.status = AttemptStatus.SUCCEEDED if passed else AttemptStatus.FAILED
            attempt.exit_code = process.returncode
            attempt.error = (
                "Controlled build timed out" if timed_out else (stderr_text[-4000:] or "Controlled build failed")
            ) if not passed else None
            attempt.final_message = result
            attempt.finished_at = _now()
            db.commit()
        return result

    def _record_firmware_artifacts(
        self,
        run_id: str,
        partitions: list[str],
        image_root: Path,
        *,
        source: str,
        minimum_mtime: float | None = None,
    ) -> list[dict[str, Any]]:
        normalized = list(dict.fromkeys(item.strip().lower() for item in partitions))
        records: list[dict[str, Any]] = []
        for partition in normalized:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", partition):
                raise WorkflowError(f"Invalid firmware partition: {partition}")
            link_path = image_root / f"{partition}.img"
            try:
                resolved = link_path.resolve(strict=True)
            except FileNotFoundError as exc:
                raise WorkflowError(f"Firmware image not found for partition {partition}: {link_path}") from exc
            if image_root != resolved and image_root not in resolved.parents:
                raise WorkflowError(f"Firmware image escapes the configured image root: {link_path}")
            if not resolved.is_file():
                raise WorkflowError(f"Firmware artifact is not a regular file: {resolved}")
            if minimum_mtime and resolved.stat().st_mtime < minimum_mtime - 1:
                raise WorkflowError(
                    f"Firmware image {link_path.name} was not refreshed by this ./build.sh execution"
                )
            digest = self._sha256_file(resolved)
            records.append(
                {
                    "partition": partition,
                    "filename": f"{partition}.img",
                    "local_path": str(resolved),
                    "sha256": digest,
                    "size_bytes": resolved.stat().st_size,
                    "source": source,
                    "verified_at": _now(),
                }
            )

        with SessionLocal() as db:
            existing = list(db.scalars(select(FirmwareArtifact).where(FirmwareArtifact.coding_run_id == run_id)))
            for artifact in existing:
                db.delete(artifact)
            db.flush()
            for record in records:
                db.add(FirmwareArtifact(coding_run_id=run_id, **record))
            db.commit()
        return [
            {**record, "verified_at": record["verified_at"].isoformat()}
            for record in records
        ]

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as artifact:
            while chunk := artifact.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    async def import_firmware_artifacts(self, run_id: str, partitions: list[str]) -> None:
        with SessionLocal() as db:
            run = db.scalar(
                select(CodingRun)
                .options(selectinload(CodingRun.repository))
                .where(CodingRun.id == run_id)
            )
            if not run:
                raise WorkflowError("Coding run not found")
            if run.status not in {RunStatus.AWAITING_COMPLETION_APPROVAL, RunStatus.COMPLETED}:
                raise WorkflowError("Firmware can only be imported at or after the completion gate")
            if not run.repository.firmware_image_root:
                raise WorkflowError("Repository firmware image root is not configured")
            image_root = Path(run.repository.firmware_image_root).resolve()
        artifacts = self._record_firmware_artifacts(
            run_id,
            partitions,
            image_root,
            source="operator_import",
        )
        await self.emit(
            run_id,
            "firmware.imported",
            f"用户已登记 {len(artifacts)} 个现有固件镜像",
            {"artifacts": artifacts},
        )

    def _declared_firmware_partitions(
        self,
        result: dict[str, Any],
        run_id: str | None = None,
    ) -> list[str]:
        value = result.get("firmware_partitions")
        if isinstance(value, list) and all(isinstance(item, str) for item in value) and value:
            return list(value)
        if run_id:
            with SessionLocal() as db:
                return list(
                    db.scalars(
                        select(FirmwareArtifact.partition)
                        .where(FirmwareArtifact.coding_run_id == run_id)
                        .order_by(FirmwareArtifact.partition)
                    )
                )
        return []

    async def _validate(self, run_id: str) -> dict[str, Any]:
        stage = self._new_stage(run_id, StageKind.VALIDATE)
        self._mark_stage_running(run_id, stage.id, RunStatus.VALIDATING)
        with SessionLocal() as db:
            run = db.scalar(
                select(CodingRun)
                .options(selectinload(CodingRun.repository))
                .where(CodingRun.id == run_id)
            )
            assert run and run.worktree_path
            commands = run.repository.validation_commands
            cwd = Path(run.worktree_path)

        if not commands:
            result = {
                "passed": True,
                "checks": [],
                "warning": "No repository validation commands are configured; completion requires human approval.",
            }
            self._finish_stage(stage.id, result)
            await self.emit(run_id, "validation.skipped", "仓库未配置自动验证命令，已记录风险", result)
            return result

        checks: list[dict[str, Any]] = []
        for command in commands:
            check = await self._run_validation_command(run_id, stage.id, cwd, command)
            checks.append(check)
            if not check["passed"]:
                break
        result = {"passed": all(item["passed"] for item in checks), "checks": checks}
        self._finish_stage(stage.id, result, succeeded=result["passed"])
        await self.emit(
            run_id,
            "validation.completed",
            "自动验证通过" if result["passed"] else "自动验证失败",
            result,
        )
        return result

    async def _run_validation_command(
        self,
        run_id: str,
        stage_id: str,
        cwd: Path,
        command: str,
    ) -> dict[str, Any]:
        with SessionLocal() as db:
            ordinal = db.scalar(
                select(func.count(ExecutionAttempt.id)).where(ExecutionAttempt.stage_id == stage_id)
            ) or 0
            attempt = ExecutionAttempt(
                stage_id=stage_id,
                ordinal=ordinal + 1,
                executor="shell",
                status=AttemptStatus.RUNNING,
                command=["/bin/bash", "-lc", command],
                started_at=_now(),
            )
            db.add(attempt)
            db.commit()
            db.refresh(attempt)
            attempt_id = attempt.id

        artifact_dir = self.settings.artifact_root / run_id / attempt_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.validation_timeout_seconds
            )
            timed_out = False
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            timed_out = True
        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")
        (artifact_dir / "stdout.log").write_text(stdout_text, encoding="utf-8")
        (artifact_dir / "stderr.log").write_text(stderr_text, encoding="utf-8")
        passed = process.returncode == 0 and not timed_out
        payload = {
            "command": command,
            "passed": passed,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout_tail": stdout_text[-12000:],
            "stderr_tail": stderr_text[-12000:],
        }
        with SessionLocal() as db:
            attempt = db.get(ExecutionAttempt, attempt_id)
            assert attempt
            attempt.status = AttemptStatus.SUCCEEDED if passed else AttemptStatus.FAILED
            attempt.exit_code = process.returncode
            attempt.error = "Validation command timed out" if timed_out else (stderr_text[-4000:] or None if not passed else None)
            attempt.final_message = payload
            attempt.finished_at = _now()
            db.commit()
        await self.emit(run_id, "validation.command", f"验证命令{'通过' if passed else '失败'}：{command}", payload, attempt_id)
        return payload

    async def _review(self, run_id: str) -> dict[str, Any]:
        stage = self._new_stage(run_id, StageKind.REVIEW)
        self._mark_stage_running(run_id, stage.id, RunStatus.REVIEWING)
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            assert run and run.worktree_path
            review_sessions = list(
                db.scalars(
                    select(AgentSession)
                    .where(
                        AgentSession.run_id == run_id,
                        AgentSession.role == "review",
                    )
                    .order_by(AgentSession.last_used_at.desc(), AgentSession.created_at.desc())
                )
            )
            if review_sessions:
                session = review_sessions[0]
                session.cwd = run.worktree_path
                session.active = True
                for legacy_session in review_sessions[1:]:
                    legacy_session.active = False
            else:
                session = AgentSession(run_id=run_id, node_id="175-coding", role="review", cwd=run.worktree_path)
                db.add(session)
            resume_review = bool(session.codex_thread_id)
            prompt = self._review_prompt(run, resumed=resume_review)
            persisted_stage = db.get(StageRun, stage.id)
            assert persisted_stage
            persisted_stage.prompt = prompt
            db.commit()
            db.refresh(session)
            session_id = session.id

        await self.emit(
            run_id,
            "stage.started",
            "独立 Reviewer 正在继续审查变更" if resume_review else "独立 Reviewer 已建立并开始审查变更",
            {"stage": StageKind.REVIEW.value, "resumed": resume_review, "session_id": session_id},
        )
        result = await self.runner.execute(
            stage_id=stage.id,
            session_id=session_id,
            prompt=prompt,
            output_schema=self.settings.schema_root / "review-result.schema.json",
            resume=resume_review,
            sandbox_mode="read-only",
        )
        self._finish_stage(stage.id, result.result, succeeded=result.result.get("status") == "ready")
        await self.emit(run_id, "review.completed", "独立代码审查完成", result.result)
        return result.result

    async def _can_repair(self, run_id: str) -> bool:
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            assert run
            if run.repair_attempts >= self.settings.max_repair_attempts:
                return False
            run.repair_attempts += 1
            db.commit()
            return True

    async def _repair(self, run_id: str, source: str, evidence: dict[str, Any]) -> dict[str, Any]:
        stage = self._new_stage(run_id, StageKind.REPAIR)
        self._mark_stage_running(run_id, stage.id, RunStatus.REPAIRING)
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            session = db.scalar(select(AgentSession).where(AgentSession.run_id == run_id, AgentSession.role == "coding"))
            assert run and session
            prompt = (
                f"The {source} stage found problems. Inspect the evidence below, fix the implementation in the "
                "current worktree, run focused checks, and return the required structured result. Refresh the "
                "commit object so it describes the complete final delivery in English repository style, not only "
                "the latest repair. Do not include Change-Id or Signed-off-by.\n\n"
                f"Evidence:\n{json.dumps(evidence, ensure_ascii=False, indent=2)}"
            )
            persisted_stage = db.get(StageRun, stage.id)
            assert persisted_stage
            persisted_stage.prompt = prompt
            db.commit()
            session_id = session.id
        await self.emit(run_id, "repair.started", f"Codex 正在修复 {source} 阶段发现的问题", {"source": source})
        result = await self.runner.execute(
            stage_id=stage.id,
            session_id=session_id,
            prompt=prompt,
            output_schema=self.settings.schema_root / "implementation-result.schema.json",
            resume=True,
        )
        if result.result.get("status") == "blocked":
            raise WorkflowError(self._blocker_message(result.result, "Repair was blocked"))
        self._finish_stage(stage.id, result.result)
        await self.emit(run_id, "repair.completed", "修复完成，将重新执行验证", result.result)
        return result.result

    async def _operator_repair(
        self,
        *,
        run_id: str,
        message: str,
        submitted_by: str,
        previous_error: str,
    ) -> dict[str, Any]:
        stage = self._new_stage(run_id, StageKind.REPAIR)
        self._mark_stage_running(run_id, stage.id, RunStatus.REPAIRING)
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            session = db.scalar(
                select(AgentSession).where(
                    AgentSession.run_id == run_id,
                    AgentSession.role == "coding",
                )
            )
            assert run and session
            prompt = self._operator_recovery_prompt(run, previous_error, message)
            persisted_stage = db.get(StageRun, stage.id)
            assert persisted_stage
            persisted_stage.prompt = prompt
            persisted_stage.result = {
                "interaction": "operator_recovery",
                "operator_recovery": {
                    "message": message,
                    "submitted_by": submitted_by,
                    "previous_error": previous_error,
                },
                "status": "running",
            }
            db.commit()
            session_id = session.id

        await self.emit(
            run_id,
            "recovery.started",
            "原 Coding Agent 正在核验人工处理结果并继续修复",
            {"stage_id": stage.id, "submitted_by": submitted_by},
        )
        result = await self.runner.execute(
            stage_id=stage.id,
            session_id=session_id,
            prompt=prompt,
            output_schema=self.settings.schema_root / "implementation-result.schema.json",
            resume=True,
        )
        payload = {
            **result.result,
            "interaction": "operator_recovery",
            "operator_recovery": {
                "message": message,
                "submitted_by": submitted_by,
                "previous_error": previous_error,
            },
        }
        if result.result.get("status") == "blocked":
            self._finish_stage(stage.id, payload, succeeded=False)
            raise WorkflowError(self._blocker_message(result.result, "Operator recovery was blocked"))
        self._finish_stage(stage.id, payload)
        await self.emit(run_id, "recovery.completed", "人工处理已核验，继续执行受控构建", {"stage_id": stage.id})
        return payload

    async def _request_completion_approval(self, run_id: str) -> None:
        stage = self._new_stage(run_id, StageKind.COMPLETE)
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            persisted_stage = db.get(StageRun, stage.id)
            assert run and persisted_stage
            run.status = RunStatus.AWAITING_COMPLETION_APPROVAL
            run.current_stage = StageKind.COMPLETE
            persisted_stage.status = StageStatus.AWAITING_APPROVAL
            persisted_stage.started_at = _now()
            approval = Approval(
                run_id=run_id,
                stage_id=stage.id,
                kind="completion",
                summary="编码、自动验证、独立代码审查、Server 控制的 ./build.sh 和固件镜像校验均已完成，等待最终确认。",
            )
            db.add(approval)
            db.commit()
        await self.emit(run_id, "approval.requested", "Coding Run 等待最终确认", {"kind": "completion"})

    async def cancel(self, run_id: str) -> None:
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            if not run:
                raise WorkflowError("Coding run not found")
            if run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED}:
                raise WorkflowError(f"Cannot cancel a run in status {run.status.value}")
            run.status = RunStatus.CANCELLING
            db.commit()
        await self.emit(run_id, "workflow.cancelling", "正在取消运行中的任务")
        await self.runner.cancel_run(run_id)
        async with self._task_lock:
            task = self._tasks.get(run_id)
            if task and not task.done():
                task.cancel()
        await self._mark_cancelled(run_id)

    async def delete_record(self, run_id: str) -> None:
        """Delete a terminal run and its controlled worktree/artifacts."""
        async with self._task_lock:
            task = self._tasks.get(run_id)
            if task and not task.done():
                raise WorkflowError("Coding run is still finalizing and cannot be deleted yet")
            with SessionLocal() as db:
                run = db.scalar(
                    select(CodingRun)
                    .options(selectinload(CodingRun.repository))
                    .where(CodingRun.id == run_id)
                )
                if not run:
                    raise WorkflowError("Coding run not found")
                if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                    raise WorkflowError(
                        f"Only completed, failed, or cancelled runs can be deleted; current status is {run.status.value}"
                    )
                downstream_qa = db.scalar(select(func.count(QaRun.id)).where(QaRun.coding_run_id == run_id)) or 0
                plugin_releases = db.scalar(
                    select(func.count(QaPluginRelease.id)).where(QaPluginRelease.coding_run_id == run_id)
                ) or 0
                if downstream_qa or plugin_releases:
                    raise WorkflowError(
                        "Run has downstream QA runs or registered QA plugins; delete is blocked to preserve audit records"
                    )
                repository_path = run.repository.root_path
                worktree_path = run.worktree_path
                work_branch = run.work_branch
                source_branch = run.source_branch
                worktree_mode = run.worktree_mode

            if worktree_path and worktree_mode == WorktreeMode.NEW:
                try:
                    await self.workspaces.remove_worktree(
                        repository_path=repository_path,
                        worktree_path=worktree_path,
                        work_branch=work_branch,
                        source_branch=source_branch,
                    )
                except WorkspaceError as exc:
                    raise WorkflowError(str(exc)) from exc

            artifact_path = (self.settings.artifact_root / run_id).resolve()
            if artifact_path == self.settings.artifact_root or self.settings.artifact_root not in artifact_path.parents:
                raise WorkflowError("Refusing to remove artifacts outside the configured artifact root")
            if artifact_path.exists():
                shutil.rmtree(artifact_path)

            with SessionLocal() as db:
                run = db.get(CodingRun, run_id)
                if not run:
                    raise WorkflowError("Coding run not found")
                db.delete(run)
                db.commit()

    async def delete_record_only(self, run_id: str) -> None:
        """Delete control-plane records without touching worktrees, branches, or artifacts."""
        deletable_statuses = {
            RunStatus.CREATED,
            RunStatus.AWAITING_PLAN_APPROVAL,
            RunStatus.AWAITING_COMPLETION_APPROVAL,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.CANCELLED,
        }
        async with self._task_lock:
            task = self._tasks.get(run_id)
            if task and not task.done():
                raise WorkflowError("Coding run is still active and cannot be deleted")
            with SessionLocal() as db:
                run = db.get(CodingRun, run_id)
                if not run:
                    raise WorkflowError("Coding run not found")
                if run.status not in deletable_statuses:
                    raise WorkflowError(
                        f"Running records cannot be deleted; current status is {run.status.value}"
                    )
                db.delete(run)
                db.commit()

    async def emit(
        self,
        run_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
        attempt_id: str | None = None,
    ) -> None:
        with SessionLocal() as db:
            sequence = db.scalar(
                select(func.max(JobEvent.sequence)).where(
                    JobEvent.run_id == run_id,
                    JobEvent.attempt_id.is_(None) if attempt_id is None else JobEvent.attempt_id == attempt_id,
                )
            ) or 0
            event = JobEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                sequence=sequence + 1,
                event_type=event_type,
                message=message,
                payload=payload or {},
            )
            db.add(event)
            db.commit()
            db.refresh(event)
            serialized = {
                "id": event.id,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "sequence": event.sequence,
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
                "created_at": event.created_at.isoformat(),
            }
        await self.broker.publish(run_id, serialized)

    def _new_stage(self, run_id: str, kind: StageKind) -> StageRun:
        with SessionLocal() as db:
            stage = self._create_stage_in_session(db, run_id, kind)
            db.commit()
            db.refresh(stage)
            return stage

    def _create_stage_in_session(self, db, run_id: str, kind: StageKind) -> StageRun:  # type: ignore[no-untyped-def]
        sequence = db.scalar(select(func.max(StageRun.sequence)).where(StageRun.run_id == run_id)) or 0
        stage = StageRun(run_id=run_id, kind=kind, sequence=sequence + 1)
        db.add(stage)
        return stage

    def _mark_stage_running(self, run_id: str, stage_id: str, run_status: RunStatus) -> None:
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            stage = db.get(StageRun, stage_id)
            assert run and stage
            run.status = run_status
            run.current_stage = stage.kind
            run.last_error = None
            stage.status = StageStatus.RUNNING
            stage.started_at = _now()
            db.commit()

    def _finish_stage(self, stage_id: str, result: dict[str, Any], succeeded: bool = True) -> None:
        with SessionLocal() as db:
            stage = db.get(StageRun, stage_id)
            assert stage
            stage.status = StageStatus.SUCCEEDED if succeeded else StageStatus.FAILED
            stage.result = result
            stage.finished_at = _now()
            db.commit()

    async def _fail(self, run_id: str, error: str) -> None:
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            if not run:
                return
            run.status = RunStatus.FAILED
            run.last_error = error
            active_stage = db.scalar(
                select(StageRun)
                .where(StageRun.run_id == run_id, StageRun.status == StageStatus.RUNNING)
                .order_by(StageRun.sequence.desc())
            )
            if active_stage:
                active_stage.status = StageStatus.FAILED
                active_stage.finished_at = _now()
                previous_result = active_stage.result or {}
                active_stage.result = {**previous_result, "status": "failed", "error": error}
            db.commit()
        await self.emit(run_id, "workflow.failed", "Coding Run 执行失败", {"error": error})

    async def _mark_cancelled(self, run_id: str) -> None:
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            if not run:
                return
            run.status = RunStatus.CANCELLED
            active_stages = list(
                db.scalars(select(StageRun).where(StageRun.run_id == run_id, StageRun.status == StageStatus.RUNNING))
            )
            for stage in active_stages:
                stage.status = StageStatus.CANCELLED
                stage.finished_at = _now()
            db.commit()
        await self.emit(run_id, "workflow.cancelled", "Coding Run 已取消")

    @staticmethod
    def _blocker_message(result: dict[str, Any], fallback: str) -> str:
        blockers = result.get("blockers")
        if isinstance(blockers, list) and blockers:
            return "; ".join(str(item) for item in blockers)
        return fallback

    @staticmethod
    def _plan_prompt(run: CodingRun) -> str:
        acceptance = "\n".join(f"- {item}" for item in run.acceptance_criteria) or "- No explicit criteria supplied; derive measurable checks."
        return f"""You are the planning agent for a controlled software-development workflow.

Task title: {run.title}
Requirement:
{run.requirement}

Acceptance criteria:
{acceptance}

Inspect the repository and its durable instructions. Do not modify tracked files in this stage. Investigate the relevant code, identify affected components, propose a concrete implementation and verification plan, and call out risks or missing information. Return only the JSON object required by the supplied output schema. Set status to blocked only when safe implementation truly cannot proceed.
"""

    @staticmethod
    def _plan_revision_prompt(run: CodingRun, previous_plan: dict[str, Any], feedback: str) -> str:
        acceptance = "\n".join(f"- {item}" for item in run.acceptance_criteria) or "- No explicit criteria supplied; derive measurable checks."
        return f"""Continue as the planning agent for this controlled software-development workflow. The operator reviewed your previous plan and supplied clarification or requested changes.

Task title: {run.title}
Requirement:
{run.requirement}

Acceptance criteria:
{acceptance}

Previous plan:
{json.dumps(previous_plan, ensure_ascii=False, indent=2)}

Operator feedback:
{feedback}

Reinspect the repository where necessary and produce a revised, self-contained implementation and verification plan. Treat the operator feedback as authoritative clarification. Do not modify tracked files. Return only the JSON object required by the supplied output schema. If essential information is still missing, set status to blocked and list precise questions in blockers so the operator can respond at the next gate.
"""

    @staticmethod
    def _implementation_prompt(
        run: CodingRun,
        plan: dict[str, Any],
        operator_comment: str | None = None,
    ) -> str:
        guidance = operator_comment.strip() if operator_comment and operator_comment.strip() else "No additional guidance was supplied."
        return f"""The design plan has been approved. Implement the requirement in the current isolated worktree.

Requirement:
{run.requirement}

Approved plan:
{json.dumps(plan, ensure_ascii=False, indent=2)}

Operator guidance supplied at the approval gate:
{guidance}

Follow repository instructions, keep changes scoped, add or update tests where appropriate, and run focused checks before finishing. Do not commit, merge, publish, or modify files outside the worktree. Return only the JSON object required by the supplied output schema.

Propose the final Git commit text in the `commit` object, but do not create the commit yourself. The subject must be English ASCII, at most 72 characters, and follow the repository style `<primary file area>: <concise update>`; use a second area when useful, for example `src: camera: populate frame exposure metadata`. The body must contain 1-6 concise English bullet strings beginning with `- `. Do not include `Change-Id` or `Signed-off-by`; the Server's commit hook and `git commit --signoff` add those trailers at the merge gate.

The 175 Server owns the authoritative firmware build and will run exactly `./build.sh` from the configured SDK root after this coding result. Do not claim compilation success based only on prose. In `firmware_partitions`, declare every partition image QA must receive based on the changed components: application-only changes normally require `["oem"]`; kernel changes may require `boot`; bootloader changes may require `uboot`; include any other affected partition explicitly. This firmware repository requires a non-empty list.

If this change needs a new or updated RK3588 QA test, deliver it as a versioned plugin under tools/qa-plugins/<plugin-id>/ in the worktree. The directory must contain an executable entrypoint and manifest.json with this strict shape:
{{
  "schema_version": 1,
  "plugin_id": "lowercase-stable-id",
  "version": "version-string",
  "name": "operator-facing plugin name",
  "summary": "what changed and how QA should use it",
  "cases": [{{
    "code": "QA-STABLE-CODE",
    "name": "operator-facing case name",
    "description": "preconditions, action, and pass criteria",
    "entrypoint": "relative/path/to/executable",
    "args": ["fixed", "arguments"],
    "expected_exit_code": 0,
    "timeout_seconds": 300,
    "risk_level": "read_only",
    "sudo": false
  }}]
}}
Allowed risk_level values are read_only, state_change, and firmware_write. Entrypoints must be executable, self-contained for the 3588 aarch64 environment, and must not rely on an active worktree path. List every delivered manifest path in qa_plugin_manifests; use an empty list when no QA plugin is needed.
"""

    @staticmethod
    def _operator_recovery_prompt(run: CodingRun, previous_error: str, operator_message: str) -> str:
        return f"""Continue as the original Coding Agent after an explicit operator recovery action.

Original requirement:
{run.requirement}

Previous authoritative workflow failure:
{previous_error}

Operator update:
{operator_message}

Treat the operator update as new evidence, not as proof that the failure is resolved. Verify the relevant repository and system state with read-only inspection before relying on it. You may modify files only inside the registered worktree. Do not modify external paths, create commits, merge, publish, transfer firmware, flash devices, or bypass Server-controlled gates. If an external prerequisite is still unmet, return status blocked and state the exact remaining action in blockers.

Complete any remaining in-worktree implementation, run focused checks, and return only the JSON object required by the supplied implementation schema. The result must describe the complete final delivery, including all changed_files and every firmware partition that the Server must build for QA. Application-only changes normally require `oem`; kernel changes require `boot`; bootloader changes require `uboot`.

Refresh the proposed Git commit text in the `commit` object. Its subject must be English ASCII, at most 72 characters, and follow `<primary file area>: <concise update>`. The body must contain 1-6 concise English bullet strings beginning with `- `. Do not include Change-Id or Signed-off-by; the Server adds those trailers at the merge gate.

If the delivery adds or changes RK3588 QA coverage, keep it as a versioned plugin under tools/qa-plugins/<plugin-id>/ and list its manifest in qa_plugin_manifests. Do not mutate an already-published plugin version.
"""

    @staticmethod
    def _review_prompt(run: CodingRun, resumed: bool = False) -> str:
        continuity = (
            "Continue the same independent reviewer thread. Recheck your previous findings against the current "
            "worktree, inspect the changes made since your last review, and report only findings that still apply "
            "or are newly introduced."
            if resumed
            else
            "Act as an independent code reviewer. Establish a review baseline for this Coding Run."
        )
        return f"""{continuity}

Review the uncommitted changes in this worktree against the requirement below and the repository's rules.

Requirement:
{run.requirement}

Inspect the diff and relevant surrounding code. Do not edit files. Focus on correctness, regressions, missing tests, unsafe behavior, and whether the requirement is actually satisfied. If tools/qa-plugins changed, verify that every manifest follows the versioned QA plugin contract, entrypoints are executable and self-contained for 3588 aarch64, risk levels are accurate, and an existing plugin version was not mutated without a version bump. Set status to changes_required for actionable defects, blocked only if the review cannot be completed, otherwise ready. Return only the JSON object required by the supplied output schema.
"""

    @staticmethod
    def _inquiry_prompt(
        run: CodingRun,
        question: str,
        qa_context: dict[str, Any] | None,
    ) -> str:
        context = json.dumps(qa_context, ensure_ascii=False, indent=2) if qa_context else "No QA run was attached."
        return f"""Continue as the original Coding Agent for this completed or paused controlled development run.

This turn is an operator inquiry, not authorization to implement changes. The Server has placed you in a read-only sandbox. Do not modify files, create commits, switch branches, install software, or run commands that alter repository or device state. Inspect existing code and read-only evidence only when needed.

Original requirement:
{run.requirement}

Attached QA context:
{context}

Operator question:
{question}

Answer directly and concretely. Distinguish verified evidence from inference. If a code change is advisable, explain it and set requires_code_change=true, but do not implement it in this inquiry. Return only the JSON object required by the supplied output schema.
"""
