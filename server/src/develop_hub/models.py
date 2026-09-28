from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class RunStatus(str, enum.Enum):
    CREATED = "created"
    PREPARING = "preparing"
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    IMPLEMENTING = "implementing"
    BUILDING = "building"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    REVIEWING = "reviewing"
    AWAITING_COMPLETION_APPROVAL = "awaiting_completion_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


class StageKind(str, enum.Enum):
    PREPARE = "prepare"
    PLAN = "plan"
    IMPLEMENT = "implement"
    BUILD = "build"
    VALIDATE = "validate"
    REPAIR = "repair"
    REVIEW = "review"
    DISCUSS = "discuss"
    MERGE = "merge"
    COMPLETE = "complete"


class StageStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class AttemptStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class WorktreeMode(str, enum.Enum):
    NEW = "new"
    EXISTING = "existing"
    REUSE_NEW_BRANCH = "reuse_new_branch"


class QaRunStatus(str, enum.Enum):
    CREATED = "created"
    PREPARING = "preparing"
    TESTING = "testing"
    REPORTING = "reporting"
    AWAITING_APPROVAL = "awaiting_approval"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class QaCaseStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TestCaseRisk(str, enum.Enum):
    READ_ONLY = "read_only"
    STATE_CHANGE = "state_change"
    FIRMWARE_WRITE = "firmware_write"


class QaPluginStatus(str, enum.Enum):
    PENDING = "pending"
    INSTALLING = "installing"
    INSTALLED = "installed"
    FAILED = "failed"


class TestProductStatus(str, enum.Enum):
    DRAFT = "draft"
    VERIFYING = "verifying"
    READY = "ready"
    INVALID = "invalid"


class TestRoundStatus(str, enum.Enum):
    CREATED = "created"
    CHECKING_OUT = "checking_out"
    BUILDING = "building"
    PACKAGING = "packaging"
    TRANSFERRING = "transferring"
    VERIFYING = "verifying"
    REMOTE_READY = "remote_ready"
    BENCH_RUNNING = "bench_running"
    FLASH_COMPLETE = "flash_complete"
    TEST_COMPLETE = "test_complete"
    TEST_FAILED = "test_failed"
    BENCH_FAILED = "bench_failed"
    BUILD_FAILED = "build_failed"
    BLOCKED = "blocked"


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    root_path: Mapped[str] = mapped_column(Text, unique=True)
    default_branch: Mapped[str] = mapped_column(String(120), default="main")
    validation_commands: Mapped[list[str]] = mapped_column(JSON, default=list)
    build_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    firmware_image_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    runs: Mapped[list["CodingRun"]] = relationship(back_populates="repository")
    test_products: Mapped[list["TestProduct"]] = relationship(back_populates="tested_repository")


class SdkRepository(Base):
    """An SDK workspace registered by the 175 administrator.

    Product operators select one of these records; they never submit an SDK path.
    """

    __tablename__ = "sdk_repositories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    root_path: Mapped[str] = mapped_column(Text, unique=True)
    board_configs: Mapped[list[str]] = mapped_column(JSON, default=list)
    artifact_root: Mapped[str] = mapped_column(Text)
    artifact_patterns: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    products: Mapped[list["TestProduct"]] = relationship(back_populates="sdk_repository")


class GerritRepository(Base):
    """A reusable Gerrit repository identity, independent of any local SDK."""

    __tablename__ = "gerrit_repositories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), index=True)
    project_path: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    products: Mapped[list["TestProduct"]] = relationship(back_populates="gerrit_repository")


class TestProduct(Base):
    __tablename__ = "test_products"
    __table_args__ = (
        UniqueConstraint("code", name="uq_test_products_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(200))
    sdk_repository_id: Mapped[str] = mapped_column(ForeignKey("sdk_repositories.id", ondelete="RESTRICT"), index=True)
    tested_repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="RESTRICT"), index=True)
    gerrit_repository_id: Mapped[str] = mapped_column(
        ForeignKey("gerrit_repositories.id", ondelete="RESTRICT"), index=True
    )
    # Denormalized snapshot kept for existing Test Round contracts and audit readability.
    gerrit_project: Mapped[str] = mapped_column(String(255), index=True)
    board_config: Mapped[str] = mapped_column(String(160))
    kernel_baseline: Mapped[str] = mapped_column(String(120), default="")
    uboot_baseline: Mapped[str] = mapped_column(String(120), default="")
    rkbin_baseline: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[TestProductStatus] = mapped_column(
        Enum(TestProductStatus, native_enum=False, length=32), default=TestProductStatus.DRAFT, index=True
    )
    manifest_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    repository_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    baseline_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    sdk_repository: Mapped[SdkRepository] = relationship(back_populates="products")
    tested_repository: Mapped[Repository] = relationship(back_populates="test_products")
    gerrit_repository: Mapped[GerritRepository] = relationship(back_populates="products")
    test_points: Mapped[list["TestPoint"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    rounds: Mapped[list["TestRound"]] = relationship(back_populates="product")


class TestPoint(Base):
    __tablename__ = "test_points"
    __table_args__ = (UniqueConstraint("product_id", "change_number", "patchset_number", name="uq_test_point_patchset"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("test_products.id", ondelete="CASCADE"), index=True)
    change_number: Mapped[int] = mapped_column(Integer)
    patchset_number: Mapped[int] = mapped_column(Integer)
    commit_sha: Mapped[str] = mapped_column(String(64), index=True)
    ref: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(400))
    branch: Mapped[str] = mapped_column(String(255))
    owner: Mapped[str] = mapped_column(String(160), default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[TestProduct] = relationship(back_populates="test_points")
    rounds: Mapped[list["TestRound"]] = relationship(back_populates="test_point")


class TestBench(Base):
    __tablename__ = "test_benches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=22)
    user: Mapped[str] = mapped_column(String(120))
    remote_root: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    rounds: Mapped[list["TestRound"]] = relationship(back_populates="bench")


class QaDemoRun(Base):
    __tablename__ = "qa_demo_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    bench_id: Mapped[str] = mapped_column(ForeignKey("test_benches.id", ondelete="RESTRICT"), index=True)
    remote_job_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    request_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    dispatch_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    controller_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TestRound(Base):
    __tablename__ = "test_rounds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    display_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("test_products.id", ondelete="RESTRICT"), index=True)
    test_point_id: Mapped[str | None] = mapped_column(ForeignKey("test_points.id", ondelete="RESTRICT"), index=True, nullable=True)
    bench_id: Mapped[str] = mapped_column(ForeignKey("test_benches.id", ondelete="RESTRICT"), index=True)
    status: Mapped[TestRoundStatus] = mapped_column(
        Enum(TestRoundStatus, native_enum=False, length=32), default=TestRoundStatus.CREATED, index=True
    )
    bench_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    controller_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    delivery_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    product_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    manifest_commit: Mapped[str] = mapped_column(String(64))
    board_config: Mapped[str] = mapped_column(String(160))
    gerrit_project: Mapped[str] = mapped_column(String(255))
    change_number: Mapped[int] = mapped_column(Integer)
    patchset_number: Mapped[int] = mapped_column(Integer)
    commit_sha: Mapped[str] = mapped_column(String(64))
    work_branch: Mapped[str] = mapped_column(String(255))
    build_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    image_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    test_case_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    generated_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    remote_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped[TestProduct] = relationship(back_populates="rounds")
    test_point: Mapped[TestPoint | None] = relationship(back_populates="rounds")
    bench: Mapped[TestBench] = relationship(back_populates="rounds")
    events: Mapped[list["TestRoundEvent"]] = relationship(back_populates="round", cascade="all, delete-orphan")


class TestRoundEvent(Base):
    __tablename__ = "test_round_events"
    __table_args__ = (UniqueConstraint("test_round_id", "sequence", name="uq_test_round_event_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    test_round_id: Mapped[str] = mapped_column(ForeignKey("test_rounds.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    round: Mapped[TestRound] = relationship(back_populates="events")


class ControlSequence(Base):
    __tablename__ = "control_sequences"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    next_value: Mapped[int] = mapped_column(Integer, nullable=False)


class CodingRun(Base):
    __tablename__ = "coding_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    display_number: Mapped[int] = mapped_column(Integer, unique=True, autoincrement=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="RESTRICT"), index=True)
    parent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("coding_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_qa_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("qa_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(240))
    requirement: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_branch: Mapped[str] = mapped_column(String(120))
    work_branch: Mapped[str] = mapped_column(String(160), unique=True)
    worktree_mode: Mapped[WorktreeMode] = mapped_column(
        Enum(WorktreeMode, native_enum=False, length=32), default=WorktreeMode.NEW
    )
    worktree_branch: Mapped[str] = mapped_column(String(255), default="")
    worktree_initial_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, native_enum=False), default=RunStatus.CREATED, index=True)
    current_stage: Mapped[StageKind] = mapped_column(Enum(StageKind, native_enum=False), default=StageKind.PREPARE)
    repair_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), default="local-user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    repository: Mapped[Repository] = relationship(back_populates="runs")
    stages: Mapped[list["StageRun"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    sessions: Mapped[list["AgentSession"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    qa_runs: Mapped[list["QaRun"]] = relationship(
        back_populates="coding_run",
        cascade="all, delete-orphan",
        foreign_keys="QaRun.coding_run_id",
    )
    qa_plugin_releases: Mapped[list["QaPluginRelease"]] = relationship(
        back_populates="coding_run", cascade="all, delete-orphan"
    )
    firmware_artifacts: Mapped[list["FirmwareArtifact"]] = relationship(
        back_populates="coding_run", cascade="all, delete-orphan"
    )


class StageRun(Base):
    __tablename__ = "stage_runs"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_stage_run_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("coding_runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[StageKind] = mapped_column(Enum(StageKind, native_enum=False), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[StageStatus] = mapped_column(Enum(StageStatus, native_enum=False), default=StageStatus.PENDING)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[CodingRun] = relationship(back_populates="stages")
    attempts: Mapped[list["ExecutionAttempt"]] = relationship(back_populates="stage", cascade="all, delete-orphan")


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (UniqueConstraint("node_id", "codex_thread_id", name="uq_node_codex_thread"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("coding_runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(80), default="175-coding")
    role: Mapped[str] = mapped_column(String(80), default="coding")
    codex_thread_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    cwd: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[CodingRun] = relationship(back_populates="sessions")
    attempts: Mapped[list["ExecutionAttempt"]] = relationship(back_populates="session")


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    stage_id: Mapped[str] = mapped_column(ForeignKey("stage_runs.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id", ondelete="SET NULL"), nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    executor: Mapped[str] = mapped_column(String(40), default="codex")
    status: Mapped[AttemptStatus] = mapped_column(Enum(AttemptStatus, native_enum=False), default=AttemptStatus.QUEUED)
    command: Mapped[list[str]] = mapped_column(JSON, default=list)
    resumed: Mapped[bool] = mapped_column(Boolean, default=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_message: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stage: Mapped[StageRun] = relationship(back_populates="attempts")
    session: Mapped[AgentSession | None] = relationship(back_populates="attempts")
    events: Mapped[list["JobEvent"]] = relationship(back_populates="attempt", cascade="all, delete-orphan")


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        UniqueConstraint("attempt_id", "sequence", name="uq_attempt_event_sequence"),
        Index("ix_job_events_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("coding_runs.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("execution_attempts.id", ondelete="CASCADE"), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    attempt: Mapped[ExecutionAttempt | None] = relationship(back_populates="events")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("coding_runs.id", ondelete="CASCADE"), index=True)
    stage_id: Mapped[str] = mapped_column(ForeignKey("stage_runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus, native_enum=False), default=ApprovalStatus.PENDING)
    summary: Mapped[str] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[CodingRun] = relationship(back_populates="approvals")


class TestCase(Base):
    __tablename__ = "test_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(32), default="basic", server_default="basic")
    description: Mapped[str] = mapped_column(Text)
    command: Mapped[str] = mapped_column(Text)
    expected_exit_code: Mapped[int] = mapped_column(Integer, default=0)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)
    risk_level: Mapped[TestCaseRisk] = mapped_column(
        Enum(TestCaseRisk, native_enum=False), default=TestCaseRisk.READ_ONLY
    )
    plugin_release_id: Mapped[str | None] = mapped_column(
        ForeignKey("qa_plugin_releases.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    plugin_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plugin_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    executions: Mapped[list["QaCaseExecution"]] = relationship(back_populates="test_case")
    plugin_release: Mapped["QaPluginRelease | None"] = relationship(back_populates="test_cases")


class QaPluginRelease(Base):
    __tablename__ = "qa_plugin_releases"
    __table_args__ = (
        UniqueConstraint("plugin_id", "version", name="uq_qa_plugin_release_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    coding_run_id: Mapped[str] = mapped_column(ForeignKey("coding_runs.id", ondelete="CASCADE"), index=True)
    plugin_id: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    source_relpath: Mapped[str] = mapped_column(Text)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    bundle_path: Mapped[str] = mapped_column(Text)
    status: Mapped[QaPluginStatus] = mapped_column(
        Enum(QaPluginStatus, native_enum=False), default=QaPluginStatus.PENDING, index=True
    )
    remote_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    coding_run: Mapped[CodingRun] = relationship(back_populates="qa_plugin_releases")
    test_cases: Mapped[list[TestCase]] = relationship(back_populates="plugin_release")


class FirmwareArtifact(Base):
    __tablename__ = "firmware_artifacts"
    __table_args__ = (
        UniqueConstraint("coding_run_id", "partition", name="uq_firmware_artifact_run_partition"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    coding_run_id: Mapped[str] = mapped_column(ForeignKey("coding_runs.id", ondelete="CASCADE"), index=True)
    partition: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    local_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(32), default="controlled_build")
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    coding_run: Mapped[CodingRun] = relationship(back_populates="firmware_artifacts")


class QaRun(Base):
    __tablename__ = "qa_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    display_number: Mapped[int] = mapped_column(Integer, unique=True)
    coding_run_id: Mapped[str] = mapped_column(ForeignKey("coding_runs.id", ondelete="CASCADE"), index=True)
    status: Mapped[QaRunStatus] = mapped_column(Enum(QaRunStatus, native_enum=False), default=QaRunStatus.CREATED, index=True)
    handoff: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    firmware_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    firmware_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    firmware_artifact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    ota_partitions: Mapped[list[str]] = mapped_column(JSON, default=list)
    perform_ota: Mapped[bool] = mapped_column(Boolean, default=False)
    remote_workspace: Mapped[str | None] = mapped_column(Text, nullable=True)
    codex_thread_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    coding_run: Mapped[CodingRun] = relationship(
        back_populates="qa_runs", foreign_keys=[coding_run_id]
    )
    case_executions: Mapped[list["QaCaseExecution"]] = relationship(
        back_populates="qa_run", cascade="all, delete-orphan"
    )


class QaCaseExecution(Base):
    __tablename__ = "qa_case_executions"
    __table_args__ = (UniqueConstraint("qa_run_id", "test_case_id", name="uq_qa_run_test_case"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    qa_run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), index=True)
    test_case_id: Mapped[str] = mapped_column(ForeignKey("test_cases.id", ondelete="RESTRICT"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    case_code: Mapped[str] = mapped_column(String(80))
    case_name: Mapped[str] = mapped_column(String(200))
    case_description: Mapped[str] = mapped_column(Text)
    command: Mapped[str] = mapped_column(Text)
    expected_exit_code: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    risk_level: Mapped[TestCaseRisk] = mapped_column(Enum(TestCaseRisk, native_enum=False))
    plugin_release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plugin_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plugin_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[QaCaseStatus] = mapped_column(Enum(QaCaseStatus, native_enum=False), default=QaCaseStatus.PENDING)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    qa_run: Mapped[QaRun] = relationship(back_populates="case_executions")
    test_case: Mapped[TestCase] = relationship(back_populates="executions")
