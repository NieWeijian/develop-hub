from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import get_db
from .events import broker
from .git_workspace import WorkspaceError
from .models import Approval, CodingRun, ControlSequence, JobEvent, QaRun, QaRunStatus, Repository, RunStatus, WorktreeMode, new_id
from .schemas import (
    ApprovalDecision,
    ApprovalFeedback,
    ApprovalView,
    CodingRunCreate,
    CodingRunView,
    CodingInquiryCreate,
    EventView,
    FirmwareArtifactImport,
    HealthView,
    MergeRequest,
    MergeReadinessView,
    OperatorRecoveryCreate,
    RepositoryBranchesView,
    RepositoryCreate,
    RepositoryUpdate,
    RepositoryView,
    RepositoryWorktreeView,
    RepositoryWorktreesView,
)
from .workflow import CodingWorkflow, WorkflowError


router = APIRouter(prefix="/api")


def get_workflow(request: Request) -> CodingWorkflow:
    return request.app.state.workflow


def _run_query():  # type: ignore[no-untyped-def]
    return select(CodingRun).options(
        selectinload(CodingRun.stages),
        selectinload(CodingRun.sessions),
        selectinload(CodingRun.approvals),
        selectinload(CodingRun.firmware_artifacts),
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return normalized[:48] or "task"


def _validated_build_paths(build_root_value: str | None, image_root_value: str | None) -> tuple[str | None, str | None]:
    if not build_root_value and not image_root_value:
        return None, None
    if not build_root_value or not image_root_value:
        raise HTTPException(status_code=422, detail="Build root and firmware image root must be configured together")
    build_root = Path(build_root_value).expanduser().resolve()
    image_root = Path(image_root_value).expanduser().resolve()
    if not build_root.is_dir() or not (build_root / "build.sh").is_file():
        raise HTTPException(status_code=422, detail=f"Build root does not contain ./build.sh: {build_root}")
    if not image_root.is_dir():
        raise HTTPException(status_code=422, detail=f"Firmware image root is not a directory: {image_root}")
    if image_root != build_root and build_root not in image_root.parents:
        raise HTTPException(status_code=422, detail="Firmware image root must stay inside the build root")
    return str(build_root), str(image_root)


def _next_run_display_number(db: Session) -> int:
    next_value = db.scalar(
        update(ControlSequence)
        .where(ControlSequence.name == "coding_run_display")
        .values(next_value=ControlSequence.next_value + 1)
        .returning(ControlSequence.next_value)
    )
    if next_value is not None:
        return next_value - 1

    display_number = (db.scalar(select(func.max(CodingRun.display_number))) or 0) + 1
    db.add(ControlSequence(name="coding_run_display", next_value=display_number + 1))
    db.flush()
    return display_number


@router.get("/health", response_model=HealthView)
async def health(request: Request) -> HealthView:
    workflow: CodingWorkflow = request.app.state.workflow
    version = await workflow.runner.version()
    database = "sqlite" if settings.database_url.startswith("sqlite") else "postgresql"
    return HealthView(
        status="ok",
        node_id="175-coding",
        codex_version=version,
        active_jobs=workflow.runner.active_jobs,
        database=database,
    )


@router.post("/repositories", response_model=RepositoryView, status_code=status.HTTP_201_CREATED)
async def create_repository(
    payload: RepositoryCreate,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> Repository:
    try:
        root = await workflow.workspaces.validate_repository(payload.root_path)
        await workflow.workspaces.validate_branch(str(root), payload.default_branch)
        build_root, image_root = _validated_build_paths(payload.build_root, payload.firmware_image_root)
        repository = Repository(
            name=payload.name,
            root_path=str(root),
            default_branch=payload.default_branch.strip(),
            validation_commands=payload.validation_commands,
            build_root=build_root,
            firmware_image_root=image_root,
        )
        db.add(repository)
        db.commit()
        db.refresh(repository)
        return repository
    except WorkspaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Repository name or path is already registered") from exc


@router.get("/repositories", response_model=list[RepositoryView])
def list_repositories(db: Session = Depends(get_db)) -> list[Repository]:
    return list(db.scalars(select(Repository).order_by(Repository.name)))


@router.get("/repositories/{repository_id}/branches", response_model=RepositoryBranchesView)
async def list_repository_branches(
    repository_id: str,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> RepositoryBranchesView:
    repository = db.get(Repository, repository_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    try:
        branches = await workflow.workspaces.list_branches(repository.root_path)
    except WorkspaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RepositoryBranchesView(
        repository_id=repository.id,
        default_branch=repository.default_branch,
        branches=branches,
    )


@router.get("/repositories/{repository_id}/worktrees", response_model=RepositoryWorktreesView)
async def list_repository_worktrees(
    repository_id: str,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> RepositoryWorktreesView:
    repository = db.get(Repository, repository_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    try:
        worktrees = await workflow.workspaces.list_worktrees(repository.root_path)
    except WorkspaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RepositoryWorktreesView(
        repository_id=repository.id,
        worktrees=[
            RepositoryWorktreeView(
                path=str(worktree.path),
                branch=worktree.branch,
                commit=worktree.commit,
                is_main=worktree.is_main,
            )
            for worktree in worktrees
        ],
    )


@router.put("/repositories/{repository_id}", response_model=RepositoryView)
async def update_repository(
    repository_id: str,
    payload: RepositoryUpdate,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> Repository:
    repository = db.get(Repository, repository_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    try:
        root = await workflow.workspaces.validate_repository(payload.root_path)
        await workflow.workspaces.validate_branch(str(root), payload.default_branch)
        build_root, image_root = _validated_build_paths(payload.build_root, payload.firmware_image_root)
        repository.name = payload.name
        repository.root_path = str(root)
        repository.default_branch = payload.default_branch.strip()
        repository.validation_commands = payload.validation_commands
        repository.build_root = build_root
        repository.firmware_image_root = image_root
        repository.enabled = payload.enabled
        db.commit()
        db.refresh(repository)
        return repository
    except WorkspaceError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Repository name or path is already registered") from exc


@router.delete("/repositories/{repository_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_repository(repository_id: str, db: Session = Depends(get_db)) -> Response:
    repository = db.get(Repository, repository_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    run_count = db.scalar(
        select(func.count(CodingRun.id)).where(CodingRun.repository_id == repository_id)
    ) or 0
    if run_count:
        raise HTTPException(
            status_code=409,
            detail=f"仓库已有 {run_count} 条运行记录，为保留审计不能删除；请将仓库停用。",
        )
    db.delete(repository)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/runs", response_model=CodingRunView, status_code=status.HTTP_201_CREATED)
async def create_run(
    payload: CodingRunCreate,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    repository = db.get(Repository, payload.repository_id)
    if not repository or not repository.enabled:
        raise HTTPException(status_code=404, detail="Repository not found or disabled")
    parent_run: CodingRun | None = None
    source_qa_run: QaRun | None = None
    if payload.parent_run_id:
        parent_run = db.get(CodingRun, payload.parent_run_id)
        if not parent_run or parent_run.repository_id != repository.id:
            raise HTTPException(status_code=422, detail="Parent coding run must belong to the selected repository")
    if payload.source_qa_run_id:
        source_qa_run = db.get(QaRun, payload.source_qa_run_id)
        if not source_qa_run or not parent_run or source_qa_run.coding_run_id != parent_run.id:
            raise HTTPException(status_code=422, detail="Source QA run must belong to the parent coding run")
    display_number = _next_run_display_number(db)
    run_id = new_id()
    worktree_initial_branch: str | None = None
    if payload.worktree_mode in {WorktreeMode.EXISTING, WorktreeMode.REUSE_NEW_BRANCH}:
        if not payload.existing_worktree_path:
            raise HTTPException(status_code=422, detail="Select an existing worktree")
        try:
            selected_worktree = await workflow.workspaces.validate_existing_worktree(
                repository.root_path,
                payload.existing_worktree_path,
            )
            conflicting_run = db.scalar(
                select(CodingRun).where(
                    CodingRun.worktree_path == str(selected_worktree.path),
                    CodingRun.status.notin_(
                        {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
                    ),
                )
            )
            if conflicting_run:
                raise WorkspaceError(
                    f"Selected worktree is already used by active RUN-{conflicting_run.display_number:06d}"
                )
        except WorkspaceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        assert selected_worktree.branch
        worktree_initial_branch = selected_worktree.branch
        worktree_path = str(selected_worktree.path)
        if payload.worktree_mode == WorktreeMode.EXISTING:
            source_branch = selected_worktree.branch
            worktree_branch = selected_worktree.branch
            work_branch = f"existing/{run_id}"
            source_commit = selected_worktree.commit
        else:
            source_branch = (payload.source_branch or repository.default_branch).strip()
            work_branch = payload.work_branch or f"codex/run-{display_number:06d}-{_slug(payload.title)}-{run_id[:8]}"
            worktree_branch = work_branch
            try:
                await workflow.workspaces.validate_switchable_worktree(repository.root_path, worktree_path)
                source_commit = await workflow.workspaces.validate_branch(repository.root_path, source_branch)
            except WorkspaceError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        source_branch = (payload.source_branch or repository.default_branch).strip()
        work_branch = payload.work_branch or f"codex/run-{display_number:06d}-{_slug(payload.title)}-{run_id[:8]}"
        worktree_branch = work_branch
        worktree_path = None
        try:
            source_commit = await workflow.workspaces.validate_branch(repository.root_path, source_branch)
        except WorkspaceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = CodingRun(
        id=run_id,
        display_number=display_number,
        repository_id=repository.id,
        parent_run_id=parent_run.id if parent_run else None,
        source_qa_run_id=source_qa_run.id if source_qa_run else None,
        title=payload.title,
        requirement=payload.requirement,
        acceptance_criteria=payload.acceptance_criteria,
        source_branch=source_branch,
        work_branch=work_branch,
        worktree_mode=payload.worktree_mode,
        worktree_branch=worktree_branch,
        worktree_initial_branch=worktree_initial_branch,
        worktree_path=worktree_path,
    )
    try:
        db.add(run)
        db.commit()
        run_id = run.id
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="The requested work branch already has a run") from exc

    await workflow.emit(
        run_id,
        "workflow.created",
        "Coding Run 已创建",
        {
            "auto_start": payload.auto_start,
            "source_branch": source_branch,
            "source_commit": source_commit,
            "worktree_mode": payload.worktree_mode.value,
            "worktree_path": worktree_path,
            "worktree_initial_branch": worktree_initial_branch,
        },
    )
    if payload.auto_start:
        try:
            await workflow.start(run_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    created = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert created
    return created


@router.get("/runs", response_model=list[CodingRunView])
def list_runs(
    run_status: RunStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[CodingRun]:
    query = _run_query().order_by(CodingRun.created_at.desc()).limit(limit)
    if run_status:
        query = query.where(CodingRun.status == run_status)
    return list(db.scalars(query).unique())


@router.get("/runs/{run_id}", response_model=CodingRunView)
def get_run(run_id: str, db: Session = Depends(get_db)) -> CodingRun:
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    if not run:
        raise HTTPException(status_code=404, detail="Coding run not found")
    return run


@router.post("/runs/{run_id}/start", response_model=CodingRunView)
async def start_run(
    run_id: str,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    run = db.get(CodingRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Coding run not found")
    if run.status != RunStatus.CREATED:
        raise HTTPException(status_code=409, detail=f"Run cannot start from {run.status.value}")
    try:
        await workflow.start(run_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    refreshed = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert refreshed
    return refreshed


@router.post("/runs/{run_id}/approvals/{approval_id}", response_model=CodingRunView)
async def decide_approval(
    run_id: str,
    approval_id: str,
    payload: ApprovalDecision,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    try:
        await workflow.decide_approval(
            run_id=run_id,
            approval_id=approval_id,
            approved=payload.approved,
            decided_by=payload.decided_by,
            comment=payload.comment,
        )
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.expire_all()
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert run
    return run


@router.post("/runs/{run_id}/approvals/{approval_id}/feedback", response_model=CodingRunView)
async def submit_approval_feedback(
    run_id: str,
    approval_id: str,
    payload: ApprovalFeedback,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    try:
        await workflow.submit_plan_feedback(
            run_id=run_id,
            approval_id=approval_id,
            message=payload.message,
            submitted_by=payload.submitted_by,
        )
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.expire_all()
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert run
    return run


@router.post("/runs/{run_id}/cancel", response_model=CodingRunView)
async def cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    try:
        await workflow.cancel(run_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.expire_all()
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert run
    return run


@router.post("/runs/{run_id}/retry-review", response_model=CodingRunView)
async def retry_review(
    run_id: str,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    try:
        await workflow.retry_review(run_id)
    except WorkflowError as exc:
        message = str(exc)
        code = 404 if message == "Coding run not found" else 409
        raise HTTPException(status_code=code, detail=message) from exc
    db.expire_all()
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert run
    return run


@router.post("/runs/{run_id}/firmware-artifacts/import", response_model=CodingRunView)
async def import_firmware_artifacts(
    run_id: str,
    payload: FirmwareArtifactImport,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    try:
        await workflow.import_firmware_artifacts(run_id, payload.partitions)
    except WorkflowError as exc:
        message = str(exc)
        code = 404 if message == "Coding run not found" else 409
        raise HTTPException(status_code=code, detail=message) from exc
    db.expire_all()
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert run
    return run


@router.post("/runs/{run_id}/inquiries", response_model=CodingRunView, status_code=status.HTTP_202_ACCEPTED)
async def ask_coding_agent(
    run_id: str,
    payload: CodingInquiryCreate,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    try:
        await workflow.ask_coding_agent(
            run_id=run_id,
            message=payload.message,
            qa_run_id=payload.qa_run_id,
            submitted_by=payload.submitted_by,
        )
    except WorkflowError as exc:
        message = str(exc)
        code = 404 if message in {"Coding run not found", "QA run not found"} else 409
        raise HTTPException(status_code=code, detail=message) from exc
    db.expire_all()
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert run
    return run


@router.post("/runs/{run_id}/recover", response_model=CodingRunView, status_code=status.HTTP_202_ACCEPTED)
async def recover_coding_run(
    run_id: str,
    payload: OperatorRecoveryCreate,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    try:
        await workflow.resume_with_operator_input(
            run_id=run_id,
            message=payload.message,
            submitted_by=payload.submitted_by,
        )
    except WorkflowError as exc:
        message = str(exc)
        code = 404 if message == "Coding run not found" else 409
        raise HTTPException(status_code=code, detail=message) from exc
    db.expire_all()
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert run
    return run


@router.post("/runs/{run_id}/merge", response_model=CodingRunView, status_code=status.HTTP_202_ACCEPTED)
async def merge_coding_run(
    run_id: str,
    payload: MergeRequest,
    db: Session = Depends(get_db),
    workflow: CodingWorkflow = Depends(get_workflow),
) -> CodingRun:
    if not payload.confirmed:
        raise HTTPException(status_code=422, detail="Explicit merge confirmation is required")
    try:
        await workflow.request_merge(
            run_id=run_id,
            qa_run_id=payload.qa_run_id,
            target_branch=payload.target_branch,
            approved_by=payload.approved_by,
        )
    except WorkflowError as exc:
        message = str(exc)
        code = 404 if message in {"Coding run not found", "QA run not found"} else 409
        raise HTTPException(status_code=code, detail=message) from exc
    db.expire_all()
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    assert run
    return run


@router.get("/runs/{run_id}/merge-readiness", response_model=MergeReadinessView)
def merge_readiness(run_id: str, db: Session = Depends(get_db)) -> MergeReadinessView:
    run = db.scalar(_run_query().where(CodingRun.id == run_id))
    if not run:
        raise HTTPException(status_code=404, detail="Coding run not found")
    repository = db.get(Repository, run.repository_id)
    assert repository
    passed_qa = db.scalar(
        select(QaRun)
        .where(QaRun.coding_run_id == run.id, QaRun.status == QaRunStatus.PASSED)
        .order_by(QaRun.completed_at.desc(), QaRun.created_at.desc())
    )
    merge_stages = sorted(
        (stage for stage in run.stages if stage.kind.value == "merge"),
        key=lambda stage: stage.sequence,
    )
    latest = merge_stages[-1] if merge_stages else None
    latest_result = latest.result if latest and latest.result else {}
    source_branch = str(latest_result.get("source_branch") or run.worktree_branch)
    target_branch = repository.default_branch
    if latest and latest.status.value == "succeeded":
        return MergeReadinessView(
            eligible=False, status="succeeded", reason="已完成本地主分支合并，尚未推送远端。",
            qa_run_id=latest_result.get("qa_run_id"), source_branch=source_branch,
            target_branch=target_branch, merge_result=latest.result,
        )
    if latest and latest.status.value == "running":
        return MergeReadinessView(
            eligible=False, status="running", reason="Server 正在执行受控合并。",
            qa_run_id=latest_result.get("qa_run_id"), source_branch=source_branch,
            target_branch=target_branch, merge_result=latest.result,
        )
    if run.status != RunStatus.COMPLETED:
        return MergeReadinessView(
            eligible=False, status="blocked", reason="Coding Run 尚未完成。", qa_run_id=None,
            source_branch=source_branch, target_branch=target_branch,
        )
    if not passed_qa:
        return MergeReadinessView(
            eligible=False, status="no_qa", reason="等待关联 QA Run 经人工批准通过。", qa_run_id=None,
            source_branch=source_branch, target_branch=target_branch,
        )
    if run.worktree_branch == target_branch:
        return MergeReadinessView(
            eligible=False, status="already_default", reason="Coding Run 当前已位于仓库默认分支，无需再次合并。",
            qa_run_id=passed_qa.id, source_branch=source_branch, target_branch=target_branch,
        )
    return MergeReadinessView(
        eligible=True,
        status="failed" if latest and latest.status.value == "failed" else "ready",
        reason=str(latest_result.get("error") or "Coding 与 QA 门禁均已通过，可以执行本地主分支合并。"),
        qa_run_id=passed_qa.id,
        source_branch=source_branch,
        target_branch=target_branch,
        merge_result=latest_result or None,
    )


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: str,
    workflow: CodingWorkflow = Depends(get_workflow),
) -> Response:
    try:
        await workflow.delete_record(run_id)
    except WorkflowError as exc:
        message = str(exc)
        code = 404 if message == "Coding run not found" else 409
        raise HTTPException(status_code=code, detail=message) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/runs/{run_id}/record-only", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run_record_only(
    run_id: str,
    workflow: CodingWorkflow = Depends(get_workflow),
) -> Response:
    try:
        await workflow.delete_record_only(run_id)
    except WorkflowError as exc:
        message = str(exc)
        code = 404 if message == "Coding run not found" else 409
        raise HTTPException(status_code=code, detail=message) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/runs/{run_id}/events", response_model=list[EventView])
def list_events(
    run_id: str,
    after: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> list[JobEvent]:
    if not db.get(CodingRun, run_id):
        raise HTTPException(status_code=404, detail="Coding run not found")
    query = select(JobEvent).where(JobEvent.run_id == run_id).order_by(JobEvent.created_at, JobEvent.id).limit(limit)
    if after:
        cursor = db.get(JobEvent, after)
        if cursor:
            query = query.where(JobEvent.created_at >= cursor.created_at, JobEvent.id != cursor.id)
    return list(db.scalars(query))


@router.get("/runs/{run_id}/stream")
async def stream_events(run_id: str, request: Request, db: Session = Depends(get_db)) -> StreamingResponse:
    if not db.get(CodingRun, run_id):
        raise HTTPException(status_code=404, detail="Coding run not found")

    async def stream() -> AsyncIterator[str]:
        yield "retry: 2000\n\n"
        iterator = broker.subscribe(run_id).__aiter__()
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=15)
                yield f"id: {event['id']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
            except StopAsyncIteration:
                return

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
