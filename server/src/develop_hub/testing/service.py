from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import (
    BuildEnvironment, CaseRevision, Device, InputAsset, OperatorRecord,
    PlanRevision, Product, TestPlan, TestRun, utcnow,
)
from .schemas import CaseCreate, OperatorCreate, RunCreate


TERMINAL_STATES = {"completed", "cancelled", "interrupted"}
EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def product_firmware_board(db: Session, product: Product) -> str:
    boards = {d.config['board_config'] for d in db.scalars(select(Device).where(
        Device.product_id == product.id, Device.enabled.is_(True)))
        if d.config.get('managed_station_id') and d.config.get('board_config')}
    # A product's display board name is not necessarily a build BoardConfig.
    return next(iter(boards)) if len(boards) == 1 else product.board


def fail(status: int, code: str, message: str, errors: list[dict[str, str]] | None = None) -> None:
    raise HTTPException(status, detail={"code": code, "message": message, "errors": errors or []})


def require(db: Session, model: type, identity: str):
    row = db.get(model, identity)
    if row is None:
        fail(404, "not_found", "指定记录不存在")
    return row


def view(row: Any) -> dict[str, Any]:
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat() if isinstance(value, datetime) else deepcopy(value)
    return result


def digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        fail(422, "invalid_json_value", "配置必须使用有限数值和标准 JSON 数据")
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_node(node: dict[str, Any], *, build: bool = False) -> None:
    if not build:
        from .safety import require_simulation
        require_simulation(node)
    if not isinstance(node.get("kind"), str) or node.get("kind") not in {"local", "simulation", "ssh"}:
        fail(422, "invalid_node", "节点类型必须是 local、simulation 或 ssh")
    if build and node.get("kind") != "local":
        fail(422, "unsupported_build_node", "首批构建环境请登记在控制端本机")
    if node.get("kind") == "ssh":
        for field in ("host", "user"):
            value = node.get(field)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*" if field == "host" else r"[A-Za-z_][A-Za-z0-9_-]*", value):
                fail(422, "invalid_node", f"SSH 节点的 {field} 无效")
        root = node.get("root", "")
        if root and (not isinstance(root, str) or not root.startswith("/") or root == "/" or ".." in PurePosixPath(root).parts):
            fail(422, "invalid_node", "SSH 节点需要专用绝对工作目录")
        if type(node.get("port", 22)) is not int or not 1 <= node.get("port", 22) <= 65535:
            fail(422, "invalid_node", "SSH 端口必须在 1 至 65535 之间")


def create_case(db: Session, payload: CaseCreate | dict[str, Any], *, allow_package: bool = False) -> CaseRevision:
    from .packages import validate_parameter_schema

    data = payload.model_dump() if isinstance(payload, CaseCreate) else CaseCreate.model_validate(payload).model_dump()
    definition = deepcopy(data["definition"])
    definition.setdefault("parameters_schema", deepcopy(EMPTY_SCHEMA))
    try:
        validate_parameter_schema(definition["parameters_schema"])
    except ValueError as exc:
        fail(422, "invalid_parameter_schema", str(exc))
    if data["mode"] == "manual":
        if not isinstance(definition.get('recovery', {}), dict):
            fail(422, 'invalid_recovery', '恢复配置必须是对象')
        recovery = definition.get('recovery') or {}
        if definition.get('effects', 'read_only') != 'read_only' or recovery.get('required'):
            steps = definition.get('steps') or []
            final_schema = steps[-1].get('record_schema', {}) if steps and isinstance(steps[-1], dict) else {}
            if (recovery.get('required') is not True or recovery.get('mode') != 'operator_confirmation'
                    or not steps or steps[-1].get('id') != 'restore-confirmation'
                    or final_schema.get('properties', {}).get('recovery_confirmed', {}).get('type') != 'boolean'
                    or 'recovery_confirmed' not in final_schema.get('required', [])
                    or steps[-1].get('attachments_required') is not True):
                fail(422, 'manual_recovery_unsupported', '有状态变更的人工用例必须以恢复确认及证据附件收尾')
        wait_limit = definition.get('operator_wait_timeout_seconds', 3600)
        if type(wait_limit) is not int or not 1 <= wait_limit <= 172800:
            fail(422, 'invalid_operator_timeout', '人工步骤等待时长必须为 1 到 172800 秒')
        steps = definition.get("steps", [])
        if not isinstance(steps, list) or not steps:
            fail(422, "manual_steps_required", "人工用例至少需要一个步骤和明确的通过标准")
        identities = set()
        for step in steps:
            if (not isinstance(step, dict) or not isinstance(step.get("id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", step["id"]) or not step.get("title")):
                fail(422, "invalid_manual_step", "每个人工步骤必须有 id 和标题")
            if step["id"] in identities:
                fail(422, "duplicate_manual_step", "人工步骤 id 不能重复")
            identities.add(step["id"])
            if not (step.get("instruction") or step.get("pass_criteria")):
                fail(422, "manual_criteria_required", "人工步骤必须说明操作与通过标准")
            required_fields = step.get("required_fields", [])
            if not isinstance(required_fields, list) or any(not isinstance(field, str) or not field for field in required_fields):
                fail(422, "invalid_manual_fields", "人工必填字段必须是名称列表")
            if type(step.get("attachments_required", False)) is not bool:
                fail(422, "invalid_manual_evidence", "附件要求必须是布尔值")
            confirmations = step.get("required_confirmations", [])
            properties = step.get("record_schema", {}).get("properties", {})
            if not isinstance(confirmations, list) or any(not isinstance(key, str) or properties.get(key, {}).get("type") != "boolean" for key in confirmations):
                fail(422, "invalid_manual_confirmations", "人工确认项必须引用布尔类型记录字段")
            if "record_schema" in step:
                try:
                    validate_parameter_schema(step["record_schema"])
                except ValueError as exc:
                    fail(422, "invalid_record_schema", str(exc))
    else:
        implementation = definition.get("implementation", {})
        if not isinstance(implementation, dict):
            fail(422, "invalid_implementation", "用例执行定义无效")
        if implementation.get("runtime_profile") != "simulation":
            if not allow_package:
                fail(422, "package_required", "自动用例请通过代码包导入、校验和发布")
            asset = require(db, InputAsset, implementation.get("package_asset_id", ""))
            if asset.kind != "case_package":
                fail(422, "wrong_asset_kind", "执行入口必须引用已导入的用例包")
    requirements = definition.get("requirements", {})
    if (not isinstance(requirements, dict) or not isinstance(requirements.get("capabilities", []), list)
            or any(not isinstance(c, str) or not c for c in requirements.get("capabilities", []))):
        fail(422, "invalid_capabilities", "用例能力要求必须是列表")
    data["definition"] = definition
    existing = db.scalar(select(CaseRevision).where(CaseRevision.case_id == data["case_id"], CaseRevision.version == data["version"]))
    if existing:
        def content(definition_value):
            normalized = deepcopy(definition_value)
            implementation = normalized.get("implementation", {})
            package_id = implementation.pop("package_asset_id", None)
            if package_id:
                package = require(db, InputAsset, package_id)
                implementation["package_digest"] = package.digest
            return normalized
        if (existing.name == data["name"] and existing.mode == data["mode"]
                and content(existing.definition) == content(data["definition"])):
            return existing
        fail(409, "immutable_case_version", "该用例版本已经发布，修改内容请使用新版本号")
    row = CaseRevision(**data)
    db.add(row)
    db.flush()
    return row


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def resolve_parameters(schema: dict[str, Any], plan: dict[str, Any], run: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    from .packages import validate_parameters

    if not isinstance(plan, dict) or not isinstance(run, dict):
        raise ValueError("参数覆盖必须是对象")
    values = validate_parameters(schema, _merge(plan, run), apply_defaults=True)
    sources: dict[str, str] = {}

    def collect(current: dict[str, Any], planned: dict[str, Any], temporary: dict[str, Any], prefix: str = "") -> None:
        for key, value in current.items():
            path = prefix + key
            sources[path] = "run" if key in temporary else "plan" if key in planned else "default"
            if isinstance(value, dict):
                collect(value, planned.get(key, {}) if isinstance(planned.get(key), dict) else {},
                        temporary.get(key, {}) if isinstance(temporary.get(key), dict) else {}, path + ".")

    collect(values, plan, run)
    return values, sources


def validate_plan(db: Session, config: dict[str, Any], overrides: dict[str, dict[str, Any]] | None = None,
                  *, frozen: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate without performing source, hardware, network or package execution."""
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    parameters: dict[str, Any] = {}
    parameter_sources: dict[str, Any] = {}
    resolved: dict[str, Any] = {"product": None, "device": None, "environment": None, "cases": [], "assets": {}}
    overrides = overrides or {}

    def error(path: str, code: str, message: str) -> None:
        errors.append({"path": path, "code": code, "message": message})

    def argv_valid(value: Any) -> bool:
        return isinstance(value, list) and bool(value) and all(isinstance(arg, str) and "\x00" not in arg for arg in value)

    def entity(model: type, identity: Any, path: str, frozen_key: str | None = None):
        if not isinstance(identity, str) or not identity:
            error(path, "required", "请选择记录")
            return None
        if frozen_key and frozen and frozen.get(frozen_key):
            return deepcopy(frozen[frozen_key])
        row = db.get(model, identity)
        if row is None or not getattr(row, "enabled", True):
            error(path, "unavailable", "所选记录不存在或已停用")
            return None
        return view(row)

    def asset(identity: Any, kind: str, path: str):
        item = entity(InputAsset, identity, path)
        if item and item["kind"] != kind:
            error(path, "wrong_asset_kind", "输入资产类型不匹配")
            return None
        if item:
            resolved["assets"][item["id"]] = item
        return item

    product = entity(Product, config.get("product_id"), "product_id", "product")
    resolved["product"] = product
    firmware = config.get("firmware", {})
    if not isinstance(firmware, dict):
        firmware = {}
    kind = firmware.get("kind")
    if not isinstance(kind, str) or kind not in {"current_device", "artifact", "build"}:
        error("firmware.kind", "invalid_choice", "请选择当前固件、已有镜像或源码构建")
        kind = ""
    build_only = firmware.get("build_only", False)
    if type(build_only) is not bool or (build_only and kind != "build"):
        error("firmware.build_only", "invalid_build_only", "仅构建终点只适用于源码构建")
    if kind == "build":
        resolved["environment"] = entity(BuildEnvironment, firmware.get("environment_id"), "firmware.environment_id", "environment")
        environment = resolved["environment"]
        if environment:
            env_config = environment.get("config", {})
            if environment.get("node", {}).get("kind") != "local":
                error("firmware.environment_id", "unsupported_build_node", "首批构建环境必须在控制端本机")
            if not isinstance(env_config.get("sdk_path"), str) or not env_config["sdk_path"].strip():
                error("firmware.environment_id", "sdk_path_required", "构建环境需要填写 SDK 目录")
            recipe = env_config.get("recipe", {})
            if not isinstance(recipe, dict) or not argv_valid(recipe.get("argv")):
                error("firmware.environment_id", "build_command_required", "构建配方需要填写命令参数数组")
            if not isinstance(recipe, dict) or not isinstance(recipe.get("artifacts"), list) or not recipe["artifacts"]:
                error("firmware.environment_id", "artifact_rules_required", "构建配方需要声明镜像输出路径")
        source = firmware.get("source", {"kind": "local_workspace"})
        if not isinstance(source, dict) or not isinstance(source.get("kind"), str) or source.get("kind") not in {"local_workspace", "snapshot"}:
            error("firmware.source.kind", "invalid_source", "选择本地工作区或已封存源码快照")
        elif source.get("kind") == "snapshot":
            asset(source.get("snapshot_id"), "source_snapshot", "firmware.source.snapshot_id")
        if isinstance(source, dict) and not isinstance(source.get("repository_overrides", []), list):
            error("firmware.source.repository_overrides", "invalid_overrides", "仓库版本覆盖必须是列表")
    if kind == "artifact":
        bundle = asset(firmware.get("bundle_id"), "firmware_bundle", "firmware.bundle_id")
        if bundle and product:
            manifest = bundle["manifest"]
            if manifest.get("product_id") and manifest["product_id"] != product["id"]:
                error("firmware.bundle_id", "wrong_product", "固件不属于所选产品")
            if manifest.get("chip") and product.get("chip") and manifest["chip"].lower() != product["chip"].lower():
                error("firmware.bundle_id", "wrong_chip", "固件芯片与产品不匹配")
    if not build_only:
        device = entity(Device, config.get("device_id"), "device_id", "device")
        resolved["device"] = device
        if product and device and device["product_id"] != product["id"]:
            error("device_id", "wrong_product", "设备不属于所选产品")
        if device and device.get("health") != "ready":
            warnings.append("设备尚未就绪，执行前需要确认设备状态")
        if device and device.get("node", {}).get("kind") == "simulation":
            warnings.append("这是模拟设备，结果不能作为真实硬件测试结论")
        elif device:
            device_config = device.get("config", {})
            adapter = device_config.get("adapter", {})
            if not isinstance(device_config.get("serial"), str) or not device_config["serial"]:
                error("device_id", "device_identity_required", "真实设备需要登记序列号，以便执行前核对身份")
            if not isinstance(adapter, dict) or not argv_valid(adapter.get("inspect_argv")):
                error("device_id", "inspect_adapter_required", "设备需要配置身份读取适配器")
            if kind in {"artifact", "build"} and (not isinstance(adapter, dict) or not argv_valid(adapter.get("deploy_argv"))):
                error("device_id", "deploy_adapter_required", "本次需要烧录，请配置设备部署适配器")
            if kind == 'artifact' and bundle:
                from .deployment import metadata_error
                problem = metadata_error(device, bundle['manifest'], firmware.get('expected_version') or bundle['manifest'].get('expected_version'))
                if problem:
                    error('firmware.bundle_id', 'deployment_metadata', problem)
    else:
        device = None
    bindings = config.get("cases", [])
    if not isinstance(bindings, list):
        error("cases", "invalid_cases", "用例列表格式无效")
        bindings = []
    if build_only and bindings:
        error("cases", "build_only_has_cases", "仅构建终点不执行用例，请移除用例或启用测试")
    if not build_only and not bindings:
        error("cases", "cases_required", "请选择至少一个必需用例")
    acceptance = config.get("acceptance", {"mode": "all_required_cases"})
    if not isinstance(acceptance, dict) or not isinstance(acceptance.get("mode"), str) or acceptance.get("mode") not in {"all_required_cases", "not_tested"}:
        error("acceptance.mode", "unsupported_acceptance", "首批支持全部必需项通过的验收规则")
    elif not build_only and acceptance.get("mode") != "all_required_cases":
        error("acceptance.mode", "invalid_acceptance", "执行测试时必须按必需项目形成结论")
    policy = config.get("policy", {})
    if not isinstance(policy, dict):
        error("policy", "invalid_policy", "执行策略必须是对象")
    else:
        if policy.get("independent_case_failure", "continue") not in ("continue", "stop"):
            error("policy.independent_case_failure", "invalid_policy", "用例失败策略只能选择继续或停止")
        if policy.get("recovery_failure", "quarantine_device") != "quarantine_device":
            error("policy.recovery_failure", "invalid_policy", "设备恢复失败后必须停止使用并人工处理")
        timeout = policy.get("manual_wait_timeout_seconds", 3600)
        if type(timeout) is not int or not 1 <= timeout <= 604800:
            error("policy.manual_wait_timeout_seconds", "invalid_timeout", "人工等待时限必须为 1 至 604800 秒")
    seen: set[str] = set()
    required_count = 0
    available = set((device or {}).get("capabilities", []))
    frozen_cases = {c["binding_id"]: c for c in (frozen or {}).get("cases", [])}
    for index, binding in enumerate(bindings):
        path = f"cases.{index}"
        if not isinstance(binding, dict):
            error(path, "invalid_binding", "用例绑定必须是对象")
            continue
        bid = binding.get("binding_id")
        if not isinstance(bid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", bid):
            error(path + ".binding_id", "invalid_binding_id", "用例绑定需要字母、数字或短横线组成的唯一标识")
            continue
        if bid in seen:
            error(path + ".binding_id", "duplicate_binding", "同一方案内用例绑定标识不能重复")
            continue
        seen.add(bid)
        required = binding.get("required", True)
        if type(required) is not bool:
            error(path + ".required", "invalid_required", "必需项必须是布尔值")
        elif required:
            required_count += 1
        case = deepcopy(frozen_cases.get(bid)) if frozen else entity(CaseRevision, binding.get("case_revision_id"), path + ".case_revision_id")
        if not case:
            if frozen:
                error(path, "missing_frozen_case", "固定输入缺少用例定义")
            continue
        definition = case["definition"]
        missing = set(definition.get("requirements", {}).get("capabilities", [])) - available
        if missing:
            error(path + ".case_revision_id", "missing_capabilities", "设备缺少能力：" + "、".join(sorted(missing)))
        implementation = definition.get("implementation", {})
        if implementation.get("package_asset_id"):
            asset(implementation["package_asset_id"], "case_package", path + ".case_revision_id")
        if case["mode"] == "automated" and implementation.get("runtime_profile") == "simulation" and device and device.get("node", {}).get("kind") != "simulation":
            error(path + ".case_revision_id", "simulation_mismatch", "模拟用例只能在模拟设备上执行")
        try:
            params, sources = resolve_parameters(definition.get("parameters_schema", EMPTY_SCHEMA), binding.get("parameter_overrides", {}), overrides.get(bid, {}))
        except (ValueError, TypeError) as exc:
            error(path + ".parameter_overrides", "invalid_parameters", str(exc))
            continue
        parameters[bid], parameter_sources[bid] = params, sources
        resolved["cases"].append({
            "binding_id": bid, "required": required, "case_revision_id": binding["case_revision_id"],
            "case_id": case["case_id"], "version": case["version"], "name": case["name"], "mode": case["mode"],
            "definition": deepcopy(definition), "parameters": params, "parameter_sources": sources,
        })
    if not build_only and bindings and required_count == 0:
        error("cases", "required_case_missing", "执行测试的方案至少需要一个必需项")
    for bid in set(overrides) - seen:
        error("parameter_overrides." + bid, "unknown_binding", "临时参数只允许覆盖当前方案的用例")
    stages = {"current_device": ["绑定设备", "读取身份", "执行用例", "恢复设备", "生成报告"],
              "artifact": ["校验镜像", "绑定设备", "烧录", "启动确认", "执行用例", "恢复设备", "生成报告"],
              "build": ["固定源码", "构建", "封存镜像"] + ([] if build_only else ["绑定设备", "烧录", "启动确认", "执行用例", "恢复设备", "生成报告"])}
    return {"valid": not errors, "errors": errors, "warnings": warnings, "parameters": parameters,
            "parameter_sources": parameter_sources, "stages": stages.get(kind, []), "resolved": resolved}


def publish_plan(db: Session, plan_id: str, revision: int) -> PlanRevision:
    plan = require(db, TestPlan, plan_id)
    if plan.archived:
        fail(409, "plan_archived", "方案已归档")
    if plan.revision != revision:
        fail(409, "stale_draft", "草稿已被修改，请刷新后重试")
    preview = validate_plan(db, plan.draft)
    if not preview["valid"]:
        fail(422, "invalid_plan", "方案配置不完整或无效", preview["errors"])
    claimed = db.execute(update(TestPlan).where(TestPlan.id == plan_id, TestPlan.revision == revision).values(revision=revision + 1, updated_at=utcnow()))
    if claimed.rowcount != 1:
        fail(409, "stale_draft", "草稿已被修改，请刷新后重试")
    version = (db.scalar(select(func.max(PlanRevision.version)).where(PlanRevision.plan_id == plan_id)) or 0) + 1
    row = PlanRevision(plan_id=plan_id, version=version, config=deepcopy(plan.draft), resolved=preview["resolved"])
    db.add(row)
    db.flush()
    return row


def create_run(db: Session, payload: RunCreate) -> tuple[TestRun, bool]:
    request_digest = digest(payload.model_dump(exclude={"idempotency_key"}))
    existing = db.scalar(select(TestRun).where(TestRun.idempotency_key == payload.idempotency_key))
    if existing:
        if existing.request_digest != request_digest:
            fail(409, "idempotency_conflict", "该请求标识已用于不同配置")
        return existing, False
    revision = require(db, PlanRevision, payload.plan_revision_id) if payload.plan_revision_id else publish_plan(db, payload.plan_id, payload.draft_revision)
    if require(db, TestPlan, revision.plan_id).archived:
        fail(409, "plan_archived", "方案已归档")
    for key, model in (("product", Product), ("device", Device), ("environment", BuildEnvironment)):
        item = revision.resolved.get(key)
        if item:
            live = db.get(model, item["id"])
            if live is None or not live.enabled:
                fail(409, "resource_disabled", "方案引用的资源已停用，请选择可用资源")
            if key == "device" and live.health not in {"ready", "busy"}:
                fail(409, "device_not_ready", "设备需要人工处理或尚未就绪，不能启动测试")
    for case in revision.resolved.get("cases", []):
        live = db.get(CaseRevision, case["case_revision_id"])
        if live is None or not live.enabled:
            fail(409, "case_disabled", "方案引用的用例版本已停用")
    preview = validate_plan(db, revision.config, payload.parameter_overrides, frozen=revision.resolved)
    if not preview["valid"]:
        fail(422, "invalid_run", "运行参数无效", preview["errors"])
    snapshot = {"plan_revision_id": revision.id, "plan_name": require(db, TestPlan, revision.plan_id).name, "plan": deepcopy(revision.config),
                "parameter_overrides": deepcopy(payload.parameter_overrides), "parameters": preview["parameters"],
                "parameter_sources": preview["parameter_sources"], **preview["resolved"]}
    row = TestRun(plan_revision_id=revision.id, idempotency_key=payload.idempotency_key,
                  request_digest=request_digest, request_snapshot=snapshot,
                  events=[{"at": utcnow().isoformat(), "type": "created", "message": "测试请求已创建，等待执行"}])
    db.add(row)
    db.flush()
    return row, True


def repeat_run(db: Session, original_id: str, idempotency_key: str) -> tuple[TestRun, bool]:
    """Reuse recorded inputs, never resolve a branch or rebuild implicitly."""
    request_digest = digest({"repeat_run_id": original_id})
    existing = db.scalar(select(TestRun).where(TestRun.idempotency_key == idempotency_key))
    if existing:
        if existing.request_digest != request_digest:
            fail(409, "idempotency_conflict", "该请求标识已用于不同的运行请求")
        return existing, False
    original = require(db, TestRun, original_id)
    if original.resolved_input is None:
        fail(422, "repeat_input_unresolved", "本次运行尚无完整固定输入，请按方案重新运行")
    snapshot = deepcopy(original.request_snapshot)
    resolved = deepcopy(original.resolved_input)
    firmware = snapshot["plan"]["firmware"]
    result: dict[str, Any] = {}
    bundle = (original.result or {}).get("firmware_bundle") or resolved.get("firmware_bundle")
    if firmware["kind"] in {"artifact", "build"}:
        if not isinstance(bundle, dict) or not bundle.get("files") or not bundle.get("digest"):
            fail(422, "repeat_firmware_missing", "原运行没有可复用的固定镜像，请按方案重新运行")
        result["firmware_bundle"] = deepcopy(bundle)
        if (original.result or {}).get("firmware_bundle_id"):
            result["firmware_bundle_id"] = original.result["firmware_bundle_id"]
        if (original.result or {}).get("build"):
            result["build"] = deepcopy(original.result["build"])

    def available(manifest: dict[str, Any], label: str) -> None:
        root_value = manifest.get("root_path")
        if not isinstance(root_value, str) or not Path(root_value).is_dir():
            fail(422, "repeat_asset_missing", label + "内容已不可用，请按方案重新运行")
        root = Path(root_value).resolve()
        for item in manifest.get("files", []):
            relative = item.get("path") if isinstance(item, dict) else None
            if not isinstance(relative, str):
                fail(422, "repeat_asset_invalid", label + "清单无效，请重新导入")
            path = (root / relative).resolve()
            if root not in path.parents or not path.is_file():
                fail(422, "repeat_asset_missing", label + "文件已不可用，请按方案重新运行")
        # Actual bytes are checked again by the execution adapter before use.

    if bundle:
        available(bundle, "固定镜像")
    for case in snapshot.get("cases", []):
        package_id = case.get("definition", {}).get("implementation", {}).get("package_asset_id")
        live_case = db.get(CaseRevision, case["case_revision_id"])
        if live_case is None or not live_case.enabled:
            fail(409, "case_disabled", "原运行引用的用例版本已停用，不能创建复测")
        if package_id:
            package = db.get(InputAsset, package_id)
            if package is None or package.kind != "case_package":
                fail(422, "repeat_asset_missing", "原用例包已不可用，请重新导入")
            available(package.manifest, "用例包")
    for key, model in (("product", Product), ("device", Device)):
        item = snapshot.get(key)
        if item:
            live = db.get(model, item["id"])
            if live is None or not live.enabled:
                fail(409, "resource_disabled", "原运行引用的资源已停用")
            if key == "device" and live.health not in {"ready", "busy"}:
                fail(409, "device_not_ready", "设备状态需要人工处理，不能复测")
    snapshot["repeat_of_run_id"] = original.id
    snapshot["repeat_notice"] = (
        "当前固件测试将重新读取设备实际版本，不能保证设备仍运行上一轮二进制。"
        if firmware["kind"] == "current_device" else "复用原运行的固定镜像和用例参数，不重新获取代码或编译。"
    )
    row = TestRun(plan_revision_id=original.plan_revision_id, idempotency_key=idempotency_key,
                  request_digest=request_digest, request_snapshot=snapshot, resolved_input=resolved, result=result,
                  events=[{"at": utcnow().isoformat(), "type": "repeat", "message": snapshot["repeat_notice"]}])
    db.add(row)
    db.flush()
    return row, True


def record_operator(db: Session, run_id: str, payload: OperatorCreate) -> tuple[OperatorRecord, bool]:
    request_digest = digest(payload.model_dump(exclude={"idempotency_key"}))
    existing = db.scalar(select(OperatorRecord).where(OperatorRecord.run_id == run_id, OperatorRecord.idempotency_key == payload.idempotency_key))
    if existing:
        if existing.request_digest != request_digest:
            fail(409, "idempotency_conflict", "该操作请求标识已经用于不同记录")
        return existing, False
    run = require(db, TestRun, run_id)
    if db.scalar(select(OperatorRecord.id).where(OperatorRecord.run_id == run_id, OperatorRecord.step_id == payload.step_id)):
        fail(409, "operator_step_recorded", "该步骤已提交，正在继续执行，请刷新状态")
    waiting = (run.result or {}).get("awaiting_operator", {})
    if run.state != "awaiting_operator" or waiting.get("step_id") != payload.step_id:
        fail(409, "not_current_step", "当前运行未等待此人工步骤，请刷新状态")
    if waiting.get("record_schema"):
        from .packages import validate_parameters
        try:
            validate_parameters(waiting["record_schema"], payload.values, apply_defaults=False)
        except ValueError as exc:
            fail(422, "invalid_operator_values", str(exc))
    for field in waiting.get("required_fields", []):
        if payload.values.get(field) is None or payload.values.get(field) == "":
            fail(422, "operator_value_required", "请填写人工记录字段：" + field)
    if waiting.get("attachments_required") and not payload.attachments:
        fail(422, "operator_evidence_required", "此步骤需要上传证据附件")
    for attachment in payload.attachments:
        if not isinstance(attachment, dict) or not attachment.get("evidence_id"):
            fail(422, "invalid_evidence", "附件必须引用已登记的证据编号")
        evidence = (run.result or {}).get("evidence", [])
        if not any(e.get("evidence_id") == attachment["evidence_id"] for e in evidence):
            fail(422, "unknown_evidence", "附件不属于当前运行已登记的证据")
    changed = db.execute(update(TestRun).where(TestRun.id == run_id, TestRun.state == "awaiting_operator",
                        TestRun.operator_revision == payload.revision, TestRun.cancel_requested.is_(False)).values(operator_revision=payload.revision + 1))
    if changed.rowcount != 1:
        fail(409, "stale_operator_step", "步骤已被处理或正在取消，请刷新后重试")
    row = OperatorRecord(run_id=run_id, step_id=payload.step_id, revision=payload.revision + 1,
                         idempotency_key=payload.idempotency_key, request_digest=request_digest,
                         operator=payload.operator, decision=payload.decision, values=deepcopy(payload.values), attachments=deepcopy(payload.attachments))
    db.add(row)
    db.flush()
    return row, True


def report(db: Session, run: TestRun, *, diagnostics: bool = False) -> dict[str, Any]:
    data = view(run)
    data.pop("request_digest", None)
    data.pop("idempotency_key", None)
    result = run.result or {}
    cases = result.get("cases", [])
    data["summary"] = {
        "plan_name": run.request_snapshot.get("plan_name", ""),
        "repeat_of_run_id": run.request_snapshot.get("repeat_of_run_id"),
        "repeat_notice": run.request_snapshot.get("repeat_notice"),
        "product": run.request_snapshot.get("product", {}).get("name", ""),
        "device": (run.request_snapshot.get("device") or {}).get("name", ""),
        "firmware_source": run.request_snapshot["plan"].get("firmware", {}).get("kind"),
        "simulated": (run.request_snapshot.get("device") or {}).get("node", {}).get("kind") == "simulation",
        "total": len(run.request_snapshot.get("cases", [])),
        "passed": sum(c.get("verdict", c.get("outcome")) == "passed" for c in cases),
        "failed": sum(c.get("verdict", c.get("outcome")) == "failed" for c in cases),
        "error": sum(c.get("verdict", c.get("outcome")) == "error" for c in cases),
    }
    data["operator_records"] = [view(record) for record in db.scalars(select(OperatorRecord).where(OperatorRecord.run_id == run.id).order_by(OperatorRecord.revision))]
    from .report_analysis import annotate_report
    from ..config import settings
    root = settings.artifact_root / 'testing' / 'runs' / run.id if diagnostics else None
    return annotate_report(data, root)
