from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductCreate(InputModel):
    name: str = Field(min_length=1, max_length=160)
    chip: str = Field(default="", max_length=80)
    board: str = Field(default="", max_length=160)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)


class ProductPatch(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    chip: str | None = Field(default=None, max_length=80)
    board: str | None = Field(default=None, max_length=160)
    capabilities: list[str] | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class EnvironmentCreate(InputModel):
    name: str = Field(min_length=1, max_length=160)
    node: dict[str, Any] = Field(default_factory=lambda: {"kind": "local"})
    config: dict[str, Any] = Field(default_factory=dict)


class EnvironmentPatch(InputModel):
    revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    node: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class DeviceCreate(InputModel):
    name: str = Field(min_length=1, max_length=160)
    product_id: str = Field(min_length=1, max_length=36)
    station_id: str | None = Field(default=None, max_length=160)
    serial: str = Field(default='', max_length=64)
    node: dict[str, Any] = Field(default_factory=lambda: {"kind": "simulation"})
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)


class DevicePatch(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    node: dict[str, Any] | None = None
    capabilities: list[str] | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class DeviceCheck(InputModel):
    operator: str = Field(min_length=1, max_length=160)
    note: str = Field(default='', max_length=2000)
    recovery_confirmed: bool = False


class CaseCreate(InputModel):
    case_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    version: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    mode: Literal["automated", "manual"] = "manual"
    definition: dict[str, Any] = Field(default_factory=dict)


class EnabledPatch(InputModel):
    enabled: bool


class PlanCreate(InputModel):
    name: str = Field(min_length=1, max_length=160)
    draft: dict[str, Any] = Field(default_factory=dict)


class PlanPatch(InputModel):
    revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    draft: dict[str, Any]


class RevisionCreate(InputModel):
    revision: int = Field(ge=1)


class PlanCopy(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)


class PlanValidate(InputModel):
    draft: dict[str, Any] | None = None
    parameter_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RunCreate(InputModel):
    plan_revision_id: str | None = None
    plan_id: str | None = None
    draft_revision: int | None = Field(default=None, ge=1)
    parameter_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def one_source(self) -> "RunCreate":
        if bool(self.plan_revision_id) == bool(self.plan_id):
            raise ValueError("选择已发布方案版本，或提供方案及草稿修订号")
        if self.plan_id and self.draft_revision is None:
            raise ValueError("保存并运行必须提供草稿修订号")
        if self.plan_revision_id and self.draft_revision is not None:
            raise ValueError("已发布版本不能同时指定草稿修订号")
        return self


class OperatorCreate(InputModel):
    step_id: str = Field(min_length=1, max_length=320)
    revision: int = Field(ge=0)
    operator: str = Field(min_length=1, max_length=160)
    decision: Literal["passed", "failed"]
    values: dict[str, Any] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    idempotency_key: str = Field(min_length=1, max_length=160)


class RepeatCreate(InputModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
