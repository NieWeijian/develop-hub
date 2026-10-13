from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from develop_hub.codex_runner import CodexRunner
from develop_hub.config import settings as base_settings
from develop_hub.db import SessionLocal, initialize_database
from develop_hub.events import EventBroker
from develop_hub.models import (
    AgentSession,
    Approval,
    ApprovalStatus,
    CodingRun,
    ExecutionAttempt,
    FirmwareArtifact,
    Repository,
    RunStatus,
    StageKind,
    StageRun,
    StageStatus,
    WorktreeMode,
)
from develop_hub.workflow import CodingWorkflow, WorkflowError


def make_git_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def make_fake_codex(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

if '--version' in sys.argv:
    print('codex-cli fake-test')
    raise SystemExit(0)

args = sys.argv[1:]
schema_path = args[args.index('--output-schema') + 1]
output_path = pathlib.Path(args[args.index('--output-last-message') + 1])
thread_id = output_path.parent.name
print(json.dumps({'type': 'thread.started', 'thread_id': thread_id}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'fixture complete'}}), flush=True)
if 'review-result' in schema_path:
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': 'x' * 200000}}), flush=True)
if 'plan-result' in schema_path:
    result = {'status': 'ready', 'summary': 'Plan ready', 'plan': ['edit'], 'risks': [], 'acceptance_check': ['tests pass'], 'blockers': []}
elif 'review-result' in schema_path:
    result = {'status': 'ready', 'summary': 'Review ready', 'findings': [], 'final_checks': ['tests pass'], 'blockers': []}
elif 'coding-inquiry-result' in schema_path:
    result = {'status': 'answered', 'answer': 'The implementation is bounded to README.md.', 'evidence': ['README.md was changed'], 'suggested_next_steps': ['No code change is required'], 'requires_code_change': False}
else:
    result = {'status': 'completed', 'summary': 'Implemented', 'changed_files': ['README.md'], 'commit': {'subject': 'src: fixture: implement controlled change', 'body': ['- update the workflow test fixture']}, 'tests': [], 'qa_plugin_manifests': [], 'firmware_partitions': ['oem'], 'remaining_risks': [], 'blockers': []}
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(json.dumps(result), encoding='utf-8')
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.mark.asyncio
async def test_complete_coding_workflow(tmp_path: Path) -> None:
    initialize_database()
    repository_path = tmp_path / "repository"
    make_git_repository(repository_path)
    fake_codex = tmp_path / "fake-codex"
    make_fake_codex(fake_codex)
    runtime_settings = replace(
        base_settings,
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
        schema_root=Path(__file__).parents[1] / "schemas",
        codex_bin=str(fake_codex),
        max_repair_attempts=1,
    )
    runtime_settings.ensure_directories()
    event_broker = EventBroker()
    runner = CodexRunner(runtime_settings, event_broker)
    workflow = CodingWorkflow(runtime_settings, runner, event_broker)

    image_root = repository_path / "output" / "image"
    image_root.mkdir(parents=True)
    build_script = repository_path / "build.sh"
    build_script.write_text("#!/bin/sh\nset -eu\nmkdir -p output/image\nprintf firmware > output/image/oem.img\n", encoding="utf-8")
    build_script.chmod(build_script.stat().st_mode | stat.S_IXUSR)
    subprocess.run(["git", "add", "build.sh"], cwd=repository_path, check=True)
    subprocess.run(["git", "commit", "-m", "add controlled build"], cwd=repository_path, check=True, capture_output=True)

    with SessionLocal() as db:
        display_number = (db.scalar(select(func.max(CodingRun.display_number))) or 0) + 1
        repository = Repository(
            name="fixture-repo",
            root_path=str(repository_path),
            default_branch="main",
            validation_commands=["git diff --check"],
            build_root=str(repository_path),
            firmware_image_root=str(image_root),
        )
        db.add(repository)
        db.flush()
        run = CodingRun(
            display_number=display_number,
            repository_id=repository.id,
            title="Implement fixture",
            requirement="Implement a safe fixture change and verify the result.",
            acceptance_criteria=["git diff --check succeeds"],
            source_branch="main",
            work_branch=f"codex/run-{display_number:06d}-fixture",
            worktree_mode=WorktreeMode.EXISTING,
            worktree_path=str(repository_path),
            worktree_branch="main",
            worktree_initial_branch="main",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    await workflow.start(run_id)
    await workflow._tasks[run_id]
    with SessionLocal() as db:
        run = db.get(CodingRun, run_id)
        approval = db.scalar(select(Approval).where(Approval.run_id == run_id, Approval.kind == "plan"))
        assert run and approval
        assert run.status == RunStatus.AWAITING_PLAN_APPROVAL
        assert run.sessions[0].codex_thread_id
        plan_approval_id = approval.id

    await workflow.submit_plan_feedback(
        run_id,
        plan_approval_id,
        "Please include a rollback check and explain the compatibility boundary.",
        "tester",
    )
    await workflow._tasks[run_id]
    with SessionLocal() as db:
        run = db.get(CodingRun, run_id)
        approvals = list(
            db.scalars(
                select(Approval)
                .where(Approval.run_id == run_id, Approval.kind == "plan")
                .order_by(Approval.requested_at)
            )
        )
        plan_stages = sorted(
            (stage for stage in run.stages if stage.kind == StageKind.PLAN),
            key=lambda stage: stage.sequence,
        ) if run else []
        assert run and run.status == RunStatus.AWAITING_PLAN_APPROVAL
        assert len(approvals) == 2
        assert approvals[0].status == ApprovalStatus.REJECTED
        assert approvals[0].comment and "rollback check" in approvals[0].comment
        assert approvals[1].status == ApprovalStatus.PENDING
        assert [stage.status for stage in plan_stages] == [StageStatus.SKIPPED, StageStatus.AWAITING_APPROVAL]
        assert plan_stages[1].prompt and "compatibility boundary" in plan_stages[1].prompt
        revision_attempt = db.scalar(
            select(ExecutionAttempt).where(ExecutionAttempt.stage_id == plan_stages[1].id)
        )
        assert revision_attempt and revision_attempt.resumed is True
        revised_plan_approval_id = approvals[1].id

    await workflow.decide_approval(
        run_id,
        revised_plan_approval_id,
        True,
        "tester",
        "Keep the compatibility behavior backward-safe.",
    )
    await workflow._tasks[run_id]
    with SessionLocal() as db:
        run = db.get(CodingRun, run_id)
        approval = db.scalar(
            select(Approval).where(Approval.run_id == run_id, Approval.kind == "completion")
        )
        assert run and approval
        assert run.status == RunStatus.AWAITING_COMPLETION_APPROVAL
        build_stage = max(
            (stage for stage in run.stages if stage.kind == StageKind.BUILD),
            key=lambda stage: stage.sequence,
        )
        build_attempt = db.scalar(select(ExecutionAttempt).where(ExecutionAttempt.stage_id == build_stage.id))
        firmware = db.scalar(select(FirmwareArtifact).where(FirmwareArtifact.coding_run_id == run_id))
        review_stage = max(
            (stage for stage in run.stages if stage.kind == StageKind.REVIEW),
            key=lambda stage: stage.sequence,
        )
        assert build_attempt and build_attempt.command == ["/bin/bash", "-lc", "./build.sh"]
        assert review_stage.sequence < build_stage.sequence
        assert build_attempt.exit_code == 0
        assert firmware and firmware.partition == "oem"
        assert firmware.source == "controlled_build"
        assert Path(firmware.local_path).read_bytes() == b"firmware"
        implementation_stage = max(
            (stage for stage in run.stages if stage.kind == StageKind.IMPLEMENT),
            key=lambda stage: stage.sequence,
        )
        assert implementation_stage.prompt and "backward-safe" in implementation_stage.prompt
        completion_approval_id = approval.id

    await workflow.decide_approval(run_id, completion_approval_id, True, "tester", "looks good")
    with SessionLocal() as db:
        run = db.get(CodingRun, run_id)
        assert run and run.status == RunStatus.COMPLETED
        assert run.completed_at is not None

    await workflow.ask_coding_agent(
        run_id,
        "Which file contains the completed implementation?",
        None,
        "tester",
    )
    await workflow._tasks[run_id]
    with SessionLocal() as db:
        run = db.get(CodingRun, run_id)
        discussion = db.scalar(
            select(StageRun).where(
                StageRun.run_id == run_id,
                StageRun.kind == StageKind.DISCUSS,
            )
        )
        assert run and run.status == RunStatus.COMPLETED
        assert run.current_stage == StageKind.COMPLETE
        assert discussion and discussion.status == StageStatus.SUCCEEDED
        assert discussion.result and discussion.result["answer"].startswith("The implementation")
        inquiry_attempt = db.scalar(
            select(ExecutionAttempt).where(ExecutionAttempt.stage_id == discussion.id)
        )
        assert inquiry_attempt and inquiry_attempt.resumed is True
        assert inquiry_attempt.command[:4] == [
            str(fake_codex), "exec", "resume", "--json",
        ]
    assert 'sandbox_mode="read-only"' in inquiry_attempt.command


@pytest.mark.asyncio
async def test_operator_recovery_resumes_original_thread_and_reenters_controlled_pipeline(tmp_path: Path) -> None:
    initialize_database()
    repository_path = tmp_path / "recovery-repository"
    make_git_repository(repository_path)
    fake_codex = tmp_path / "recovery-codex"
    make_fake_codex(fake_codex)
    runtime_settings = replace(
        base_settings,
        artifact_root=tmp_path / "recovery-artifacts",
        worktree_root=tmp_path / "recovery-worktrees",
        schema_root=Path(__file__).parents[1] / "schemas",
        codex_bin=str(fake_codex),
        max_repair_attempts=1,
    )
    runtime_settings.ensure_directories()
    event_broker = EventBroker()
    runner = CodexRunner(runtime_settings, event_broker)
    workflow = CodingWorkflow(runtime_settings, runner, event_broker)

    image_root = repository_path / "output" / "image"
    image_root.mkdir(parents=True)
    build_script = repository_path / "build.sh"
    build_script.write_text(
        "#!/bin/sh\nset -eu\nmkdir -p output/image\nprintf recovered > output/image/oem.img\n",
        encoding="utf-8",
    )
    build_script.chmod(build_script.stat().st_mode | stat.S_IXUSR)
    subprocess.run(["git", "add", "build.sh"], cwd=repository_path, check=True)
    subprocess.run(["git", "commit", "-m", "add recovery build"], cwd=repository_path, check=True, capture_output=True)

    with SessionLocal() as db:
        display_number = (db.scalar(select(func.max(CodingRun.display_number))) or 0) + 1
        repository = Repository(
            name=f"recovery-repo-{display_number}",
            root_path=str(repository_path),
            default_branch="main",
            validation_commands=["git diff --check"],
            build_root=str(repository_path),
            firmware_image_root=str(image_root),
        )
        db.add(repository)
        db.flush()
        run = CodingRun(
            display_number=display_number,
            repository_id=repository.id,
            title="Recover blocked implementation",
            requirement="Finish the work after an external kernel prerequisite is applied.",
            acceptance_criteria=["controlled build and validation pass"],
            source_branch="main",
            work_branch=f"existing/recovery-{display_number}",
            worktree_mode=WorktreeMode.EXISTING,
            worktree_path=str(repository_path),
            worktree_branch="main",
            worktree_initial_branch="main",
            status=RunStatus.FAILED,
            current_stage=StageKind.REPAIR,
            last_error="Kernel prerequisite is outside the authorized worktree",
        )
        db.add(run)
        db.flush()
        db.add(
            AgentSession(
                run_id=run.id,
                node_id="175-coding",
                role="coding",
                cwd=str(repository_path),
                codex_thread_id="original-coding-thread",
            )
        )
        db.add(
            StageRun(
                run_id=run.id,
                kind=StageKind.REPAIR,
                sequence=1,
                status=StageStatus.FAILED,
                result={"error": run.last_error},
            )
        )
        db.commit()
        run_id = run.id

    await workflow.resume_with_operator_input(
        run_id,
        "I applied the required kernel patch; verify it and continue the controlled workflow.",
        "tester",
    )
    await workflow._tasks[run_id]

    with SessionLocal() as db:
        run = db.get(CodingRun, run_id)
        recovery_stage = db.scalar(
            select(StageRun)
            .where(StageRun.run_id == run_id, StageRun.kind == StageKind.REPAIR)
            .order_by(StageRun.sequence.desc())
        )
        assert run and run.status == RunStatus.AWAITING_COMPLETION_APPROVAL, run.last_error if run else "missing run"
        assert recovery_stage and recovery_stage.status == StageStatus.SUCCEEDED
        assert recovery_stage.result and recovery_stage.result["interaction"] == "operator_recovery"
        assert "applied the required kernel patch" in recovery_stage.result["operator_recovery"]["message"]
        assert recovery_stage.prompt and "previous authoritative workflow failure" in recovery_stage.prompt.lower()
        recovery_attempt = db.scalar(
            select(ExecutionAttempt).where(ExecutionAttempt.stage_id == recovery_stage.id)
        )
        assert recovery_attempt and recovery_attempt.resumed is True
        assert recovery_attempt.command[:5] == [
            str(fake_codex), "exec", "resume", "--json", "--config",
        ]
        assert 'sandbox_mode="workspace-write"' in recovery_attempt.command
        assert "original-coding-thread" in recovery_attempt.command
        build_stage = db.scalar(
            select(StageRun)
            .where(StageRun.run_id == run_id, StageRun.kind == StageKind.BUILD)
            .order_by(StageRun.sequence.desc())
        )
        assert build_stage and build_stage.status == StageStatus.SUCCEEDED
        assert recovery_stage.sequence < build_stage.sequence
        review_stage = db.scalar(
            select(StageRun)
            .where(StageRun.run_id == run_id, StageRun.kind == StageKind.REVIEW)
            .order_by(StageRun.sequence.desc())
        )
        assert review_stage and recovery_stage.sequence < review_stage.sequence < build_stage.sequence
        build_attempt = db.scalar(select(ExecutionAttempt).where(ExecutionAttempt.stage_id == build_stage.id))
        assert build_attempt and build_attempt.command == ["/bin/bash", "-lc", "./build.sh"]
        firmware = db.scalar(select(FirmwareArtifact).where(FirmwareArtifact.coding_run_id == run_id))
        assert firmware and Path(firmware.local_path).read_bytes() == b"recovered"


@pytest.mark.asyncio
async def test_controlled_build_is_not_started_before_review_is_ready(tmp_path: Path) -> None:
    initialize_database()
    repository_path = tmp_path / "review-first-repository"
    make_git_repository(repository_path)
    fake_codex = tmp_path / "review-first-codex"
    make_fake_codex(fake_codex)
    fake_codex.write_text(
        fake_codex.read_text(encoding="utf-8").replace(
            "result = {'status': 'ready', 'summary': 'Review ready', 'findings': [], 'final_checks': ['tests pass'], 'blockers': []}",
            "result = {'status': 'changes_required', 'summary': 'Fix before build', 'findings': ['defect'], 'final_checks': [], 'blockers': []}",
        ),
        encoding="utf-8",
    )
    runtime_settings = replace(
        base_settings,
        artifact_root=tmp_path / "review-first-artifacts",
        worktree_root=tmp_path / "review-first-worktrees",
        schema_root=Path(__file__).parents[1] / "schemas",
        codex_bin=str(fake_codex),
        max_repair_attempts=0,
    )
    runtime_settings.ensure_directories()
    event_broker = EventBroker()
    workflow = CodingWorkflow(runtime_settings, CodexRunner(runtime_settings, event_broker), event_broker)
    image_root = repository_path / "output" / "image"
    image_root.mkdir(parents=True)
    build_marker = repository_path / "full-build-ran"
    build_script = repository_path / "build.sh"
    build_script.write_text("#!/bin/sh\ntouch full-build-ran\n", encoding="utf-8")
    build_script.chmod(build_script.stat().st_mode | stat.S_IXUSR)

    with SessionLocal() as db:
        display_number = (db.scalar(select(func.max(CodingRun.display_number))) or 0) + 1
        repository = Repository(
            name=f"review-first-repo-{display_number}",
            root_path=str(repository_path),
            default_branch="main",
            validation_commands=[],
            build_root=str(repository_path),
            firmware_image_root=str(image_root),
        )
        db.add(repository)
        db.flush()
        run = CodingRun(
            display_number=display_number,
            repository_id=repository.id,
            title="Review before full build",
            requirement="Do not spend time on a full build while review findings remain.",
            acceptance_criteria=["review precedes build"],
            source_branch="main",
            work_branch=f"existing/review-first-{display_number}",
            worktree_mode=WorktreeMode.EXISTING,
            worktree_path=str(repository_path),
            worktree_branch="main",
            worktree_initial_branch="main",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    with pytest.raises(WorkflowError, match="Review still requires changes"):
        await workflow._validate_review_build_pipeline(
            run_id,
            {"status": "completed", "firmware_partitions": ["oem"]},
        )

    with SessionLocal() as db:
        build_count = db.scalar(
            select(func.count(StageRun.id)).where(
                StageRun.run_id == run_id,
                StageRun.kind == StageKind.BUILD,
            )
        )
        review = db.scalar(
            select(StageRun).where(StageRun.run_id == run_id, StageRun.kind == StageKind.REVIEW)
        )
        assert review and review.status == StageStatus.FAILED
        assert build_count == 0
        assert not build_marker.exists()


@pytest.mark.asyncio
async def test_repeated_reviews_resume_one_independent_reviewer_thread(tmp_path: Path) -> None:
    initialize_database()
    repository_path = tmp_path / "single-reviewer-repository"
    make_git_repository(repository_path)
    fake_codex = tmp_path / "single-reviewer-codex"
    make_fake_codex(fake_codex)
    runtime_settings = replace(
        base_settings,
        artifact_root=tmp_path / "single-reviewer-artifacts",
        worktree_root=tmp_path / "single-reviewer-worktrees",
        schema_root=Path(__file__).parents[1] / "schemas",
        codex_bin=str(fake_codex),
    )
    runtime_settings.ensure_directories()
    event_broker = EventBroker()
    workflow = CodingWorkflow(runtime_settings, CodexRunner(runtime_settings, event_broker), event_broker)

    with SessionLocal() as db:
        display_number = (db.scalar(select(func.max(CodingRun.display_number))) or 0) + 1
        repository = Repository(
            name=f"single-reviewer-repo-{display_number}",
            root_path=str(repository_path),
            default_branch="main",
            validation_commands=[],
        )
        db.add(repository)
        db.flush()
        run = CodingRun(
            display_number=display_number,
            repository_id=repository.id,
            title="Reuse one reviewer",
            requirement="Review subsequent repairs without creating another reviewer thread.",
            acceptance_criteria=["one reviewer session is retained"],
            source_branch="main",
            work_branch=f"existing/single-reviewer-{display_number}",
            worktree_mode=WorktreeMode.EXISTING,
            worktree_path=str(repository_path),
            worktree_branch="main",
            worktree_initial_branch="main",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    first_result = await workflow._review(run_id)
    assert first_result["status"] == "ready"
    with SessionLocal() as db:
        first_session = db.scalar(
            select(AgentSession).where(AgentSession.run_id == run_id, AgentSession.role == "review")
        )
        assert first_session and first_session.codex_thread_id
        first_thread_id = first_session.codex_thread_id

    second_result = await workflow._review(run_id)
    assert second_result["status"] == "ready"

    with SessionLocal() as db:
        sessions = list(
            db.scalars(
                select(AgentSession).where(AgentSession.run_id == run_id, AgentSession.role == "review")
            )
        )
        review_stages = list(
            db.scalars(
                select(StageRun)
                .where(StageRun.run_id == run_id, StageRun.kind == StageKind.REVIEW)
                .order_by(StageRun.sequence)
            )
        )
        assert len(sessions) == 1
        assert len(review_stages) == 2
        attempts = [
            db.scalar(select(ExecutionAttempt).where(ExecutionAttempt.stage_id == review_stage.id))
            for review_stage in review_stages
        ]
        assert attempts[0] and attempts[0].resumed is False
        assert attempts[1] and attempts[1].resumed is True
        assert attempts[0].session_id == attempts[1].session_id == sessions[0].id
        assert attempts[1].command[:4] == [str(fake_codex), "exec", "resume", "--json"]
        assert first_thread_id in attempts[1].command
        assert review_stages[1].prompt and "Continue the same independent reviewer thread" in review_stages[1].prompt


@pytest.mark.asyncio
async def test_review_reuses_latest_legacy_session_without_deleting_audit_history(tmp_path: Path) -> None:
    initialize_database()
    repository_path = tmp_path / "legacy-reviewer-repository"
    make_git_repository(repository_path)
    fake_codex = tmp_path / "legacy-reviewer-codex"
    make_fake_codex(fake_codex)
    runtime_settings = replace(
        base_settings,
        artifact_root=tmp_path / "legacy-reviewer-artifacts",
        worktree_root=tmp_path / "legacy-reviewer-worktrees",
        schema_root=Path(__file__).parents[1] / "schemas",
        codex_bin=str(fake_codex),
    )
    runtime_settings.ensure_directories()
    event_broker = EventBroker()
    workflow = CodingWorkflow(runtime_settings, CodexRunner(runtime_settings, event_broker), event_broker)

    with SessionLocal() as db:
        display_number = (db.scalar(select(func.max(CodingRun.display_number))) or 0) + 1
        repository = Repository(
            name=f"legacy-reviewer-repo-{display_number}",
            root_path=str(repository_path),
            default_branch="main",
            validation_commands=[],
        )
        db.add(repository)
        db.flush()
        run = CodingRun(
            display_number=display_number,
            repository_id=repository.id,
            title="Reuse latest legacy reviewer",
            requirement="Preserve old reviewer audit records while selecting one continuation thread.",
            acceptance_criteria=["no additional reviewer session is created"],
            source_branch="main",
            work_branch=f"existing/legacy-reviewer-{display_number}",
            worktree_mode=WorktreeMode.EXISTING,
            worktree_path=str(repository_path),
            worktree_branch="main",
            worktree_initial_branch="main",
        )
        db.add(run)
        db.flush()
        old_session = AgentSession(
            run_id=run.id,
            node_id="175-coding",
            role="review",
            cwd=str(repository_path),
            codex_thread_id=f"legacy-old-{display_number}",
            last_used_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        latest_session = AgentSession(
            run_id=run.id,
            node_id="175-coding",
            role="review",
            cwd=str(repository_path),
            codex_thread_id=f"legacy-latest-{display_number}",
            last_used_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        db.add_all([old_session, latest_session])
        db.commit()
        run_id = run.id
        old_session_id = old_session.id
        latest_session_id = latest_session.id
        latest_thread_id = latest_session.codex_thread_id

    await workflow._review(run_id)

    with SessionLocal() as db:
        sessions = list(
            db.scalars(
                select(AgentSession)
                .where(AgentSession.run_id == run_id, AgentSession.role == "review")
                .order_by(AgentSession.created_at)
            )
        )
        review_stage = db.scalar(
            select(StageRun).where(StageRun.run_id == run_id, StageRun.kind == StageKind.REVIEW)
        )
        assert len(sessions) == 2
        assert db.get(AgentSession, old_session_id).active is False
        assert db.get(AgentSession, latest_session_id).active is True
        assert review_stage
        attempt = db.scalar(select(ExecutionAttempt).where(ExecutionAttempt.stage_id == review_stage.id))
        assert attempt and attempt.session_id == latest_session_id
        assert attempt.resumed is True
        assert latest_thread_id in attempt.command


@pytest.mark.asyncio
async def test_retry_infrastructure_failed_review_without_reimplementing(tmp_path: Path) -> None:
    initialize_database()
    repository_path = tmp_path / "retry-review-repository"
    make_git_repository(repository_path)
    fake_codex = tmp_path / "retry-review-codex"
    make_fake_codex(fake_codex)
    runtime_settings = replace(
        base_settings,
        artifact_root=tmp_path / "retry-artifacts",
        worktree_root=tmp_path / "retry-worktrees",
        schema_root=Path(__file__).parents[1] / "schemas",
        codex_bin=str(fake_codex),
    )
    runtime_settings.ensure_directories()
    event_broker = EventBroker()
    runner = CodexRunner(runtime_settings, event_broker)
    workflow = CodingWorkflow(runtime_settings, runner, event_broker)

    with SessionLocal() as db:
        display_number = (db.scalar(select(func.max(CodingRun.display_number))) or 0) + 1
        repository = Repository(
            name=f"retry-review-repo-{display_number}",
            root_path=str(repository_path),
            default_branch="main",
            validation_commands=[],
        )
        db.add(repository)
        db.flush()
        run = CodingRun(
            display_number=display_number,
            repository_id=repository.id,
            title="Retry failed review",
            requirement="Retry only the independent review after a runner parsing failure.",
            acceptance_criteria=["review reaches the completion gate"],
            source_branch="main",
            work_branch=f"existing/retry-{display_number}",
            worktree_mode=WorktreeMode.EXISTING,
            worktree_branch="main",
            worktree_initial_branch="main",
            worktree_path=str(repository_path),
            status=RunStatus.FAILED,
            current_stage=StageKind.REVIEW,
            last_error="Separator is not found, and chunk exceed the limit",
        )
        db.add(run)
        db.flush()
        db.add_all([
            StageRun(run_id=run.id, kind=StageKind.IMPLEMENT, sequence=1, status=StageStatus.SUCCEEDED, result={"status": "completed"}),
            StageRun(run_id=run.id, kind=StageKind.VALIDATE, sequence=2, status=StageStatus.SUCCEEDED, result={"passed": True}),
            StageRun(
                run_id=run.id,
                kind=StageKind.REVIEW,
                sequence=3,
                status=StageStatus.FAILED,
                result={"error": "Separator is not found, and chunk exceed the limit"},
            ),
        ])
        db.commit()
        run_id = run.id

    await workflow.retry_review(run_id)
    await workflow._tasks[run_id]

    with SessionLocal() as db:
        run = db.get(CodingRun, run_id)
        reviews = list(
            db.scalars(
                select(StageRun)
                .where(StageRun.run_id == run_id, StageRun.kind == StageKind.REVIEW)
                .order_by(StageRun.sequence)
            )
        )
        implementation_count = db.scalar(
            select(func.count(StageRun.id)).where(
                StageRun.run_id == run_id,
                StageRun.kind == StageKind.IMPLEMENT,
            )
        )
        assert run and run.status == RunStatus.AWAITING_COMPLETION_APPROVAL, run.last_error if run else "missing run"
        assert implementation_count == 1
        assert [stage.status for stage in reviews] == [StageStatus.FAILED, StageStatus.SUCCEEDED]
        assert reviews[-1].result and reviews[-1].result["status"] == "ready"
