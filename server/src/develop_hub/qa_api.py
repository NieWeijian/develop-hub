from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .db import get_db
from .models import (
    CodingRun,
    ControlSequence,
    FirmwareArtifact,
    QaCaseExecution,
    QaRun,
    QaPluginRelease,
    QaRunStatus,
    RunStatus,
    StageKind,
    TestCase,
    TestCaseRisk,
    new_id,
)
from .qa_workflow import QaWorkflow, QaWorkflowError
from .schemas import (
    QaApprovalDecision,
    QaNodeHealthView,
    QaRunCreate,
    QaRunView,
    TestCaseCreate,
    TestCaseView,
    QaPluginReleaseView,
)
from .qa_plugins import QaPluginError, QaPluginRegistry


router = APIRouter(prefix="/api/qa", tags=["qa"])


def get_qa_workflow(request: Request) -> QaWorkflow:
    return request.app.state.qa_workflow


def get_qa_plugins(request: Request) -> QaPluginRegistry:
    return request.app.state.qa_plugins


def _qa_run_query():  # type: ignore[no-untyped-def]
    return select(QaRun).options(
        selectinload(QaRun.case_executions).selectinload(QaCaseExecution.test_case),
    )


def _next_display_number(db: Session) -> int:
    next_value = db.scalar(
        update(ControlSequence)
        .where(ControlSequence.name == "qa_run_display")
        .values(next_value=ControlSequence.next_value + 1)
        .returning(ControlSequence.next_value)
    )
    if next_value is not None:
        return next_value - 1
    display_number = (db.scalar(select(func.max(QaRun.display_number))) or 0) + 1
    db.add(ControlSequence(name="qa_run_display", next_value=display_number + 1))
    db.flush()
    return display_number


def _stage_result(run: CodingRun, kind: StageKind) -> dict[str, Any] | None:
    candidates = [stage for stage in run.stages if stage.kind == kind and stage.result]
    if not candidates:
        return None
    return max(candidates, key=lambda stage: stage.sequence).result


def _handoff(
    run: CodingRun,
    test_cases: list[TestCase],
    payload: QaRunCreate,
    firmware_artifacts: list[FirmwareArtifact],
) -> dict[str, Any]:
    return {
        "coding_run": {
            "id": run.id,
            "display_number": run.display_number,
            "title": run.title,
            "requirement": run.requirement,
            "acceptance_criteria": run.acceptance_criteria,
            "source_branch": run.source_branch,
            "work_branch": run.worktree_branch,
            "worktree_path": run.worktree_path,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        },
        "delivery": {
            "plan": _stage_result(run, StageKind.PLAN),
            "implementation": _stage_result(run, StageKind.IMPLEMENT),
            "validation": _stage_result(run, StageKind.VALIDATE),
            "review": _stage_result(run, StageKind.REVIEW),
        },
        "firmware": {
            "remote_image_directory": None,
            "artifacts": [
                {
                    "id": artifact.id,
                    "partition": artifact.partition,
                    "filename": artifact.filename,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in firmware_artifacts
            ],
            "ota_partitions": [artifact.partition for artifact in firmware_artifacts],
            "ota_requested": payload.perform_ota,
        },
        "requested_tests": [
            {
                "code": case.code,
                "name": case.name,
                "description": case.description,
                "risk_level": case.risk_level.value,
                "plugin": (
                    {
                        "release_id": case.plugin_release_id,
                        "plugin_id": case.plugin_id,
                        "version": case.plugin_version,
                    }
                    if case.plugin_release_id
                    else None
                ),
            }
            for case in test_cases
        ],
    }


@router.get("/health", response_model=QaNodeHealthView)
async def qa_health(workflow: QaWorkflow = Depends(get_qa_workflow)) -> QaNodeHealthView:
    health = await workflow.runner.health()
    return QaNodeHealthView(node_id="3588-qa", **health)


@router.get("/test-cases", response_model=list[TestCaseView])
def list_test_cases(
    include_disabled: bool = False,
    db: Session = Depends(get_db),
) -> list[TestCase]:
    query = select(TestCase).order_by(TestCase.code)
    if not include_disabled:
        query = query.where(TestCase.enabled.is_(True))
    return list(db.scalars(query))


@router.post("/test-cases", response_model=TestCaseView, status_code=status.HTTP_201_CREATED)
def create_test_case(payload: TestCaseCreate, db: Session = Depends(get_db)) -> TestCase:
    if db.scalar(select(TestCase).where(TestCase.code == payload.code, TestCase.plugin_release_id.is_(None))):
        raise HTTPException(status_code=409, detail="Test case code already exists")
    test_case = TestCase(**payload.model_dump())
    try:
        db.add(test_case)
        db.commit()
        db.refresh(test_case)
        return test_case
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Test case code already exists") from exc


@router.put("/test-cases/{test_case_id}", response_model=TestCaseView)
def update_test_case(
    test_case_id: str,
    payload: TestCaseCreate,
    db: Session = Depends(get_db),
) -> TestCase:
    test_case = db.get(TestCase, test_case_id)
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")
    if test_case.plugin_release_id:
        raise HTTPException(status_code=409, detail="Installed plugin test cases are immutable")
    duplicate = db.scalar(
        select(TestCase).where(
            TestCase.code == payload.code,
            TestCase.plugin_release_id.is_(None),
            TestCase.id != test_case_id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="Test case code already exists")
    for key, value in payload.model_dump().items():
        setattr(test_case, key, value)
    try:
        db.commit()
        db.refresh(test_case)
        return test_case
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Test case code already exists") from exc


@router.post("/runs", response_model=QaRunView, status_code=status.HTTP_201_CREATED)
async def create_qa_run(
    payload: QaRunCreate,
    db: Session = Depends(get_db),
    workflow: QaWorkflow = Depends(get_qa_workflow),
) -> QaRun:
    coding_run = db.scalar(
        select(CodingRun)
        .options(selectinload(CodingRun.stages))
        .where(CodingRun.id == payload.coding_run_id)
    )
    if not coding_run:
        raise HTTPException(status_code=404, detail="Coding run not found")
    if coding_run.status != RunStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Only a completed coding run can be handed to QA")
    if len(payload.test_case_ids) != len(set(payload.test_case_ids)):
        raise HTTPException(status_code=422, detail="Duplicate test cases are not allowed")
    test_cases_by_id = {
        item.id: item
        for item in db.scalars(
            select(TestCase).where(TestCase.id.in_(payload.test_case_ids), TestCase.enabled.is_(True))
        )
    }
    if len(test_cases_by_id) != len(payload.test_case_ids):
        raise HTTPException(status_code=422, detail="One or more test cases are missing or disabled")
    test_cases = [test_cases_by_id[item] for item in payload.test_case_ids]
    from .special_catalog import CODES as GATED_CODES
    if any(case.code in GATED_CODES for case in test_cases):
        raise HTTPException(422,'人工门控用例须从特殊场景或发布清单入口执行，不能派发自动执行器')
    if len(payload.firmware_artifact_ids) != len(set(payload.firmware_artifact_ids)):
        raise HTTPException(status_code=422, detail="Duplicate firmware artifacts are not allowed")
    artifacts_by_id = {
        item.id: item
        for item in db.scalars(
            select(FirmwareArtifact).where(
                FirmwareArtifact.id.in_(payload.firmware_artifact_ids),
                FirmwareArtifact.coding_run_id == coding_run.id,
            )
        )
    }
    if len(artifacts_by_id) != len(payload.firmware_artifact_ids):
        raise HTTPException(status_code=422, detail="One or more firmware artifacts do not belong to this coding run")
    firmware_artifacts = [artifacts_by_id[item] for item in payload.firmware_artifact_ids]
    has_firmware_write_case = any(case.risk_level == TestCaseRisk.FIRMWARE_WRITE for case in test_cases)
    if has_firmware_write_case and not payload.perform_ota:
        raise HTTPException(
            status_code=422,
            detail="Firmware-write test cases require explicit OTA approval",
        )
    if payload.perform_ota and not firmware_artifacts:
        raise HTTPException(status_code=422, detail="OTA requires one or more verified coding-run firmware artifacts")
    if firmware_artifacts and not payload.perform_ota:
        raise HTTPException(status_code=422, detail="Selecting firmware artifacts requires explicit OTA approval")
    selected_partitions = [artifact.partition for artifact in firmware_artifacts]
    if payload.ota_partitions and payload.ota_partitions != selected_partitions:
        raise HTTPException(status_code=422, detail="OTA partitions must match the selected firmware artifacts")
    if payload.firmware_path:
        raise HTTPException(status_code=422, detail="Remote firmware paths are assigned by the 175 Server")

    qa_run = QaRun(
        id=new_id(),
        display_number=_next_display_number(db),
        coding_run_id=coding_run.id,
        handoff=_handoff(coding_run, test_cases, payload, firmware_artifacts),
        firmware_path=None,
        firmware_sha256=firmware_artifacts[0].sha256 if len(firmware_artifacts) == 1 else None,
        firmware_artifact_ids=[artifact.id for artifact in firmware_artifacts],
        ota_partitions=selected_partitions,
        perform_ota=payload.perform_ota,
    )
    for ordinal, test_case in enumerate(test_cases, start=1):
        qa_run.case_executions.append(
            QaCaseExecution(
                test_case_id=test_case.id,
                ordinal=ordinal,
                case_code=test_case.code,
                case_name=test_case.name,
                case_description=test_case.description,
                command=test_case.command,
                expected_exit_code=test_case.expected_exit_code,
                timeout_seconds=test_case.timeout_seconds,
                risk_level=test_case.risk_level,
                plugin_release_id=test_case.plugin_release_id,
                plugin_id=test_case.plugin_id,
                plugin_version=test_case.plugin_version,
            )
        )
    db.add(qa_run)
    db.commit()
    qa_run_id = qa_run.id
    if payload.auto_start:
        try:
            await workflow.start(qa_run_id)
        except QaWorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    created = db.scalar(_qa_run_query().where(QaRun.id == qa_run_id))
    assert created
    return created


@router.get("/runs", response_model=list[QaRunView])
def list_qa_runs(
    run_status: QaRunStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[QaRun]:
    query = _qa_run_query().order_by(QaRun.created_at.desc()).limit(limit)
    if run_status:
        query = query.where(QaRun.status == run_status)
    return list(db.scalars(query).unique())


@router.get("/runs/{qa_run_id}", response_model=QaRunView)
def get_qa_run(qa_run_id: str, db: Session = Depends(get_db)) -> QaRun:
    qa_run = db.scalar(_qa_run_query().where(QaRun.id == qa_run_id))
    if not qa_run:
        raise HTTPException(status_code=404, detail="QA run not found")
    return qa_run


@router.get("/plugins", response_model=list[QaPluginReleaseView])
def list_qa_plugins(db: Session = Depends(get_db)) -> list[QaPluginRelease]:
    return list(db.scalars(select(QaPluginRelease).order_by(QaPluginRelease.created_at.desc())))


@router.post("/plugins/{release_id}/install", response_model=QaPluginReleaseView)
async def install_qa_plugin(
    release_id: str,
    db: Session = Depends(get_db),
    plugins: QaPluginRegistry = Depends(get_qa_plugins),
) -> QaPluginRelease:
    try:
        await plugins.install(release_id)
    except QaPluginError as exc:
        message = str(exc)
        raise HTTPException(
            status_code=404 if message == "QA plugin release not found" else 409,
            detail=message,
        ) from exc
    db.expire_all()
    release = db.get(QaPluginRelease, release_id)
    assert release
    return release


@router.post("/runs/{qa_run_id}/start", response_model=QaRunView)
async def start_qa_run(
    qa_run_id: str,
    db: Session = Depends(get_db),
    workflow: QaWorkflow = Depends(get_qa_workflow),
) -> QaRun:
    qa_run = db.get(QaRun, qa_run_id)
    if not qa_run:
        raise HTTPException(status_code=404, detail="QA run not found")
    if qa_run.status != QaRunStatus.CREATED:
        raise HTTPException(status_code=409, detail=f"QA run cannot start from {qa_run.status.value}")
    try:
        await workflow.start(qa_run_id)
    except QaWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    refreshed = db.scalar(_qa_run_query().where(QaRun.id == qa_run_id))
    assert refreshed
    return refreshed


@router.post("/runs/{qa_run_id}/decision", response_model=QaRunView)
def decide_qa_run(
    qa_run_id: str,
    payload: QaApprovalDecision,
    db: Session = Depends(get_db),
    workflow: QaWorkflow = Depends(get_qa_workflow),
) -> QaRun:
    try:
        workflow.decide(qa_run_id, payload.approved, payload.comment)
    except QaWorkflowError as exc:
        message = str(exc)
        raise HTTPException(status_code=404 if message == "QA run not found" else 409, detail=message) from exc
    db.expire_all()
    qa_run = db.scalar(_qa_run_query().where(QaRun.id == qa_run_id))
    assert qa_run
    return qa_run
