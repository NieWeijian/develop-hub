from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, computed_field

from .models import (
    ApprovalStatus,
    QaCaseStatus,
    QaRunStatus,
    QaPluginStatus,
    RunStatus,
    StageKind,
    StageStatus,
    TestCaseRisk,
    TestProductStatus,
    TestRoundStatus,
    WorktreeMode,
)


class RepositoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    root_path: str
    default_branch: str = "main"
    validation_commands: list[str] = Field(default_factory=list)
    build_root: str | None = None
    firmware_image_root: str | None = None


class RepositoryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    root_path: str
    default_branch: str = Field(min_length=1, max_length=120)
    validation_commands: list[str] = Field(default_factory=list)
    build_root: str | None = None
    firmware_image_root: str | None = None
    enabled: bool = True


class RepositoryView(RepositoryCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    enabled: bool
    created_at: datetime


class RepositoryBranchesView(BaseModel):
    repository_id: str
    default_branch: str
    branches: list[str]


class RepositoryWorktreeView(BaseModel):
    path: str
    branch: str | None
    commit: str
    is_main: bool


class RepositoryWorktreesView(BaseModel):
    repository_id: str
    worktrees: list[RepositoryWorktreeView]


class CodingRunCreate(BaseModel):
    repository_id: str
    parent_run_id: str | None = None
    source_qa_run_id: str | None = None
    title: str = Field(min_length=3, max_length=240)
    requirement: str = Field(min_length=10)
    acceptance_criteria: list[str] = Field(default_factory=list)
    source_branch: str | None = Field(default=None, min_length=1, max_length=255)
    work_branch: str | None = Field(default=None, min_length=1, max_length=255)
    worktree_mode: WorktreeMode = WorktreeMode.NEW
    existing_worktree_path: str | None = None
    auto_start: bool = True


class StageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: StageKind
    sequence: int
    status: StageStatus
    result: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None


class SessionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    node_id: str
    role: str
    codex_thread_id: str | None
    cwd: str
    active: bool
    last_used_at: datetime


class ApprovalView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    stage_id: str
    kind: str
    status: ApprovalStatus
    summary: str
    requested_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    comment: str | None


class FirmwareArtifactView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    coding_run_id: str
    partition: str
    filename: str
    local_path: str
    sha256: str
    size_bytes: int
    source: str
    verified_at: datetime
    created_at: datetime


class CodingRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_number: int
    repository_id: str
    parent_run_id: str | None
    source_qa_run_id: str | None
    title: str
    requirement: str
    acceptance_criteria: list[str]
    source_branch: str
    work_branch: str
    worktree_mode: WorktreeMode
    worktree_branch: str
    worktree_initial_branch: str | None
    worktree_path: str | None
    status: RunStatus
    current_stage: StageKind
    repair_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    closed_at: datetime | None
    stages: list[StageView] = Field(default_factory=list)
    sessions: list[SessionView] = Field(default_factory=list)
    approvals: list[ApprovalView] = Field(default_factory=list)
    firmware_artifacts: list[FirmwareArtifactView] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    approved: bool
    decided_by: str = "local-user"
    comment: str | None = None


class ApprovalFeedback(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    submitted_by: str = "local-user"

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("Feedback must not be blank")
        return message


class CodingInquiryCreate(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    qa_run_id: str | None = None
    submitted_by: str = "local-user"

    @field_validator("message")
    @classmethod
    def inquiry_must_not_be_blank(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("Inquiry must not be blank")
        return message


class MergeRequest(BaseModel):
    qa_run_id: str
    target_branch: str | None = Field(default=None, min_length=1, max_length=255)
    confirmed: bool = False
    approved_by: str = Field(default="local-user", min_length=1, max_length=120)


class MergeReadinessView(BaseModel):
    eligible: bool
    status: str
    reason: str
    qa_run_id: str | None
    source_branch: str
    target_branch: str
    merge_result: dict[str, Any] | None = None


class OperatorRecoveryCreate(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    submitted_by: str = Field(default="local-user", min_length=1, max_length=120)

    @field_validator("message")
    @classmethod
    def recovery_message_must_not_be_blank(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("Recovery message must not be blank")
        return message


class EventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    attempt_id: str | None
    sequence: int
    event_type: str
    message: str | None
    payload: dict[str, Any]
    created_at: datetime


class HealthView(BaseModel):
    status: str
    node_id: str
    codex_version: str | None
    active_jobs: int
    database: str


class QaNodeHealthView(BaseModel):
    online: bool
    node_id: str = "3588-qa"
    hostname: str | None = None
    codex_version: str | None = None
    agent_ready: bool = False
    ota_tool: str | None = None
    module_connected: bool = False
    error: str | None = None


class TestCaseCreate(BaseModel):
    category: Literal["basic", "special"] = "basic"
    code: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=2)
    command: str = Field(min_length=1, max_length=8000)
    expected_exit_code: int = 0
    timeout_seconds: int = Field(default=300, ge=1, le=172800)
    risk_level: TestCaseRisk = TestCaseRisk.READ_ONLY
    enabled: bool = True


class TestCaseView(TestCaseCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    plugin_release_id: str | None
    plugin_id: str | None
    plugin_version: str | None
    created_at: datetime
    updated_at: datetime


class QaCaseExecutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ordinal: int
    case_code: str
    case_name: str
    case_description: str
    command: str
    expected_exit_code: int
    timeout_seconds: int
    risk_level: TestCaseRisk
    plugin_release_id: str | None
    plugin_id: str | None
    plugin_version: str | None
    status: QaCaseStatus
    exit_code: int | None
    stdout_tail: str | None
    stderr_tail: str | None
    started_at: datetime | None
    finished_at: datetime | None
    test_case: TestCaseView


class QaRunCreate(BaseModel):
    coding_run_id: str
    test_case_ids: list[str] = Field(min_length=1)
    firmware_path: str | None = None
    firmware_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    firmware_artifact_ids: list[str] = Field(default_factory=list)
    ota_partitions: list[str] = Field(default_factory=list)
    perform_ota: bool = False
    auto_start: bool = True


class QaRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_number: int
    coding_run_id: str
    status: QaRunStatus
    handoff: dict[str, Any]
    firmware_path: str | None
    firmware_sha256: str | None
    firmware_artifact_ids: list[str]
    ota_partitions: list[str]
    perform_ota: bool
    remote_workspace: str | None
    codex_thread_id: str | None
    report: dict[str, Any] | None
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    case_executions: list[QaCaseExecutionView] = Field(default_factory=list)


class FirmwareArtifactImport(BaseModel):
    partitions: list[str] = Field(min_length=1)


class QaApprovalDecision(BaseModel):
    approved: bool
    comment: str | None = Field(default=None, max_length=8000)


class QaPluginReleaseView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    coding_run_id: str
    plugin_id: str
    version: str
    name: str
    summary: str
    source_relpath: str
    manifest: dict[str, Any]
    sha256: str
    status: QaPluginStatus
    remote_path: str | None
    last_error: str | None
    created_at: datetime
    installed_at: datetime | None


class SdkRepositoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    root_path: str
    board_configs: list[str] = Field(min_length=1)
    artifact_root: str
    artifact_patterns: list[str] = Field(default_factory=lambda: ["*.img"], min_length=1)


class SdkRepositoryDiscoveryRequest(BaseModel):
    root_path: str = Field(min_length=1)
    board_config_path: str = Field(default="project/cfg", min_length=1)
    board_config_pattern: str = Field(default="BoardConfig*.mk", min_length=1, max_length=160)


class SdkRepositoryDiscoveryView(BaseModel):
    root_path: str
    suggested_name: str
    board_config_path: str
    board_config_pattern: str
    board_configs: list[str]
    artifact_roots: list[str]


class SdkRepositoryView(SdkRepositoryCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class GerritRepositoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    project_path: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")

    @field_validator("name", "project_path", mode="before")
    @classmethod
    def strip_gerrit_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class GerritRepositoryView(GerritRepositoryCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ManifestSnapshotView(BaseModel):
    sdk_path: str
    manifest_file: str
    manifest_branch: str
    manifest_commit: str
    sync_status: str
    project_count: int
    local_changes: list[str]


class TestProductCreate(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,79}$")
    name: str = Field(min_length=1, max_length=200)
    sdk_repository_id: str
    tested_repository_id: str
    gerrit_repository_id: str | None = None
    board_config: str = Field(min_length=1, max_length=160)
    kernel_baseline: str = Field(default="", max_length=120)
    uboot_baseline: str = Field(default="", max_length=120)
    rkbin_baseline: str = Field(default="", max_length=120)

    @field_validator("code", "name", "board_config", mode="before")
    @classmethod
    def strip_product_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class TestProductView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    sdk_repository_id: str
    tested_repository_id: str
    gerrit_repository_id: str
    gerrit_project: str
    board_config: str
    kernel_baseline: str
    uboot_baseline: str
    rkbin_baseline: str
    status: TestProductStatus
    manifest_snapshot: dict[str, Any]
    repository_snapshot: dict[str, Any]
    baseline_result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    sdk_repository: SdkRepositoryView
    tested_repository: RepositoryView
    gerrit_repository: GerritRepositoryView


class TestPointCreate(BaseModel):
    submitted_at: datetime | None = None

    @field_validator("submitted_at")
    @classmethod
    def submitted_time_utc(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

    change_number: int = Field(gt=0)
    patchset_number: int = Field(gt=0)
    commit_sha: str = Field(pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
    ref: str = Field(pattern=r"^refs/changes/[0-9]{2}/[0-9]+/[0-9]+$")
    subject: str = Field(min_length=1, max_length=400)
    branch: str = Field(min_length=1, max_length=255)
    owner: str = Field(default="", max_length=160)
    ready: bool = True


class TestPointView(TestPointCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str
    created_at: datetime


class TestBenchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,254}$")
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,119}$")
    remote_root: str = Field(pattern=r"^/[A-Za-z0-9_./-]+$")


class TestBenchView(TestBenchCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class TestRoundCreate(BaseModel):
    allow_wear_limit_override: bool = False
    execute_test_cases: bool = False
    flash_firmware: bool = False
    device_serial: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    product_id: str
    test_point_id: str
    bench_id: str
    test_case_ids: list[str] = Field(min_length=1)
    test_case_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    auto_start: bool = True


class TestRoundEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence: int
    event_type: str
    status: str
    message: str
    details: dict[str, Any]
    created_at: datetime


class TestRoundView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_number: int
    product_id: str
    test_point_id: str
    bench_id: str
    status: TestRoundStatus
    bench_snapshot: dict[str, Any]
    controller_result: dict[str, Any] | None
    delivery_result: dict[str, Any] | None
    product_snapshot: dict[str, Any]
    manifest_commit: str
    board_config: str
    gerrit_project: str
    change_number: int
    patchset_number: int
    commit_sha: str
    work_branch: str
    build_result: dict[str, Any] | None
    image_snapshot: list[dict[str, Any]]
    test_case_snapshot: list[dict[str, Any]]
    generated_config: dict[str, Any] | None
    remote_path: str | None
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    product: TestProductView
    test_point: TestPointView
    bench: TestBenchView
    events: list[TestRoundEventView] = Field(default_factory=list)


    @computed_field
    @property
    def case_classifications(self) -> dict[str, Any]:
        from .case_classification import classifications
        return classifications(self.test_case_snapshot, self.controller_result, self.events)


    @computed_field
    @property
    def case_classification_verdict(self) -> str:
        values = list(self.case_classifications.values())
        if any(v['outcome'] == 'failed' for v in values):
            return 'failed'
        if any(v['outcome'] == 'error' for v in values):
            return 'error'
        if values and all(v['executed'] and v['outcome'] == 'passed' for v in values):
            return 'passed'
        return 'pending'
