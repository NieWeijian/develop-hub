"""Static, non-executing import for rk-case-package/1 archives.

An accepted digest proves content identity, never trust. Publishing executable
code requires a separate explicit operator decision in the case service.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Any
import zipfile


class PackageError(ValueError):
    def __init__(self, message: str, code: str = "invalid_package", **details: Any):
        super().__init__(message)
        self.code, self.details = code, details


_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$")
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_COMMON = {"type", "title", "description", "default", "enum"}
_KEYS = {
    "object": {"properties", "required", "additionalProperties"},
    "string": {"minLength", "maxLength"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "boolean": set(),
    "array": {"items", "minItems", "maxItems"},
}


def _error(message: str, path: str, code: str = "invalid_parameters") -> None:
    raise PackageError(f"{path}: {message}", code, field=path)


def validate_parameter_schema(schema: dict, *, _path: str = "$", _depth: int = 0) -> None:
    """Validate the supported subset; reject unknown/remote-reference keywords."""
    if _depth > 12 or not isinstance(schema, dict):
        _error("参数定义必须是有限深度的对象", _path, "invalid_schema")
    kind = schema.get("type")
    if kind not in _KEYS:
        _error("不支持的参数类型", _path, "invalid_schema")
    unknown = set(schema) - _COMMON - _KEYS[kind]
    if unknown:
        _error("不支持的 schema 关键字：" + ", ".join(sorted(unknown)), _path, "invalid_schema")
    for key in ("title", "description"):
        if key in schema and not isinstance(schema[key], str):
            _error(f"{key} 必须是字符串", _path, "invalid_schema")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        _error("enum 必须为非空列表", _path, "invalid_schema")
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or any(not isinstance(k, str) or not k for k in properties):
            _error("properties 必须是字段字典", _path, "invalid_schema")
        if schema.get("additionalProperties") is not False:
            _error("对象必须声明 additionalProperties: false", _path, "invalid_schema")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(k, str) or k not in properties for k in required) or len(set(required)) != len(required):
            _error("required 必须引用已声明字段且不能重复", _path, "invalid_schema")
        for name, child in properties.items():
            validate_parameter_schema(child, _path=f"{_path}.{name}", _depth=_depth + 1)
    if kind == "array":
        if not isinstance(schema.get("items"), dict):
            _error("数组必须声明 items", _path, "invalid_schema")
        validate_parameter_schema(schema["items"], _path=_path + "[]", _depth=_depth + 1)
    pairs = [("minLength", "maxLength"), ("minItems", "maxItems")]
    for lower, upper in pairs:
        for key in (lower, upper):
            if key in schema and (type(schema[key]) is not int or schema[key] < 0):
                _error(f"{key} 必须为非负整数", _path, "invalid_schema")
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            _error("最小长度不能超过最大长度", _path, "invalid_schema")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            _error(f"{key} 必须为有限数值", _path, "invalid_schema")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        _error("下限不能超过上限", _path, "invalid_schema")
    if "enum" in schema:
        without_enum = {k: v for k, v in schema.items() if k not in {"enum", "default"}}
        for value in schema["enum"]:
            _validate(without_enum, value, False, _path)
        serialized = [json.dumps(v, sort_keys=True, allow_nan=False) for v in schema["enum"]]
        if len(set(serialized)) != len(serialized):
            _error("enum 值不能重复", _path, "invalid_schema")
    if "default" in schema:
        _validate(schema, schema["default"], False, _path)


def _validate(schema: dict, value: Any, defaults: bool, path: str) -> Any:
    kind = schema["type"]
    valid_type = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "boolean": lambda: type(value) is bool,
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) in (int, float) and math.isfinite(value),
    }[kind]()
    if not valid_type:
        _error(f"必须为 {kind}", path)
    if "enum" in schema and not any(type(value) is type(item) and value == item or kind == "number" and type(value) in (int, float) and type(item) in (int, float) and value == item for item in schema["enum"]):
        _error("值不在允许选项内", path)
    if kind == "object":
        properties = schema.get("properties", {})
        unknown = set(value) - set(properties)
        if unknown:
            _error("包含未声明字段：" + ", ".join(sorted(unknown)), path)
        result = deepcopy(value)
        for name, child in properties.items():
            if name not in result and defaults and "default" in child:
                result[name] = deepcopy(child["default"])
            if name in result:
                result[name] = _validate(child, result[name], defaults, path + "." + name)
            elif name in schema.get("required", []):
                _error("必填参数未填写", path + "." + name)
        return result
    if kind in {"array", "string"}:
        lower, upper = ("minItems", "maxItems") if kind == "array" else ("minLength", "maxLength")
        if lower in schema and len(value) < schema[lower]:
            _error(f"长度不能小于 {schema[lower]}", path)
        if upper in schema and len(value) > schema[upper]:
            _error(f"长度不能大于 {schema[upper]}", path)
        if kind == "array":
            return [_validate(schema["items"], item, defaults, f"{path}[{i}]") for i, item in enumerate(value)]
    if kind in {"integer", "number"}:
        for key, invalid in (("minimum", lambda bound: value < bound), ("maximum", lambda bound: value > bound),
                             ("exclusiveMinimum", lambda bound: value <= bound), ("exclusiveMaximum", lambda bound: value >= bound)):
            if key in schema and invalid(schema[key]):
                _error(f"超出允许范围（{key}={schema[key]}）", path)
    return deepcopy(value)


def validate_parameters(schema: dict, values: dict, apply_defaults: bool = True) -> dict:
    validate_parameter_schema(schema)
    if schema.get("type") != "object":
        raise PackageError("用例参数根类型必须为 object", "invalid_schema")
    return _validate(schema, values, apply_defaults, "parameters")


def _safe_path(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name or ":" in name:
        raise PackageError("包路径无效", path=name)
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"..", "."} for part in name.rstrip("/").split("/")) or str(path) == ".":
        raise PackageError("包路径不能越界", path=name)
    # Reject names that alias on common target filesystems.
    for part in path.parts:
        if part.endswith((" ", ".")) or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise PackageError("包路径包含保留文件名", path=name)
    return path.as_posix()


def validate_manifest(manifest: dict, files: set[str] | None = None) -> None:
    if not isinstance(manifest, dict) or manifest.get("package_schema") != "rk-case-package/1":
        raise PackageError("仅支持 rk-case-package/1 包格式")
    for key, pattern in (("package_id", _IDENTIFIER), ("package_version", _VERSION)):
        if not isinstance(manifest.get(key), str) or not pattern.fullmatch(manifest[key]):
            raise PackageError(f"{key} 格式无效")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise PackageError("包必须有可读名称")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 500:
        raise PackageError("包必须包含 1 到 500 个用例")
    identifiers = set()
    for case in cases:
        if not isinstance(case, dict):
            raise PackageError("用例定义必须是对象")
        ident, version = case.get("case_id"), case.get("case_version")
        if not isinstance(ident, str) or not _IDENTIFIER.fullmatch(ident) or not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise PackageError("用例标识或版本格式无效")
        if ident in identifiers:
            raise PackageError("同一包内用例标识不能重复", case_id=ident)
        identifiers.add(ident)
        if not isinstance(case.get("name"), str) or not case["name"].strip() or not isinstance(case.get("test_scope"), str) or not case["test_scope"].strip():
            raise PackageError("用例必须说明名称和验证范围", case_id=ident)
        if case.get("mode") != "automated":
            raise PackageError("导入代码包仅支持 automated；人工用例通过用例编辑器登记")
        if case.get("effects") not in {"read_only", "state_change", "firmware_write"}:
            raise PackageError("必须声明用例副作用级别")
        if type(case.get("timeout_seconds")) is not int or not 1 <= case["timeout_seconds"] <= 86400:
            raise PackageError("用例超时必须在 1 到 86400 秒之间")
        schema = case.get("parameters_schema")
        validate_parameter_schema(schema)
        if schema["type"] != "object":
            raise PackageError("参数根类型必须为 object")
        requirements = case.get("requirements", {})
        if not isinstance(requirements, dict) or not isinstance(requirements.get("capabilities", []), list) or any(not isinstance(v, str) or not v for v in requirements.get("capabilities", [])):
            raise PackageError("capabilities 必须为字符串列表")
        ui = case.get("parameter_ui", {})
        if not isinstance(ui, dict) or set(ui) - set(schema.get("properties", {})):
            raise PackageError("parameter_ui 只能引用已声明参数")
        for key, hints in ui.items():
            if not isinstance(hints, dict) or set(hints) - {"label", "unit", "help", "group", "order"}:
                raise PackageError("不支持的参数显示字段", field=key)
            if any(not isinstance(v, str) for k, v in hints.items() if k != "order") or ("order" in hints and type(hints["order"]) is not int):
                raise PackageError("参数显示字段类型无效", field=key)
        impl = case.get("implementation", {})
        if not isinstance(impl, dict) or impl.get("runtime_profile") != "python3-case" or impl.get("input_schema") != "rk-case-input/1" or impl.get("result_schema") != "rk-case-result/1":
            raise PackageError("不支持的用例运行协议")
        if set(impl) - {"runtime_profile", "entrypoint", "input_schema", "result_schema"}:
            raise PackageError("实现配置包含不支持字段，入口参数由平台固定")
        entrypoint = _safe_path(impl.get("entrypoint", ""))
        if not entrypoint.endswith(".py") or files is not None and entrypoint not in files:
            raise PackageError("Python 入口不存在或不是 .py 文件", path=entrypoint)
        assertions = case.get("required_assertions")
        if not isinstance(assertions, list) or not assertions or any(not isinstance(v, str) or not v for v in assertions) or len(set(assertions)) != len(assertions):
            raise PackageError("必须声明非空且不重复的 required_assertions")
        recovery = case.get("recovery")
        if not isinstance(recovery, dict) or type(recovery.get("required")) is not bool:
            raise PackageError("必须明确声明是否需要恢复")
        if case["effects"] != "read_only" and recovery["required"] is not True:
            raise PackageError("有副作用的用例必须声明恢复要求")


def _json(data: bytes) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise PackageError("manifest.json 包含重复字段", field=key)
            value[key] = item
        return value
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(PackageError("不允许非有限 JSON 数值")))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError("manifest.json 不是有效 UTF-8 JSON") from exc


def import_case_package(zip_path: str | Path, destination: str | Path, *, max_files: int = 1000,
                        max_bytes: int = 200 * 1024 * 1024) -> dict:
    """Import ZIP with root manifest.json; return an untrusted static preview.

    Destination is an artifact-store directory. Content is stored under its
    digest, so repeated identical imports are idempotent. Version conflicts are
    checked when publishing CaseRevision records, not while inspecting a ZIP.
    """
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".case-import-", dir=root))
    try:
        with zipfile.ZipFile(zip_path) as archive:
            entries = archive.infolist()
            if len(entries) > max_files:
                raise PackageError("包文件数量超过限制", "package_too_large")
            names, files, total = set(), [], 0
            for item in entries:
                name = _safe_path(item.filename)
                if name.casefold() in names:
                    raise PackageError("包路径重复或大小写冲突", path=name)
                names.add(name.casefold())
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise PackageError("包不允许符号链接或特殊文件", path=name)
                if item.flag_bits & 1:
                    raise PackageError("不支持加密 ZIP")
                if item.is_dir():
                    continue
                total += item.file_size
                if total > max_bytes or item.file_size > max_bytes:
                    raise PackageError("包解压体积超过限制", "package_too_large")
                if item.file_size > 1024 * 1024 and item.file_size > max(1, item.compress_size) * 1000:
                    raise PackageError("包压缩比异常", "package_too_large")
                dest = stage / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                digest, copied = hashlib.sha256(), 0
                with archive.open(item) as src, dest.open("xb") as out:
                    for block in iter(lambda: src.read(1024 * 1024), b""):
                        copied += len(block)
                        if copied > item.file_size or copied > max_bytes:
                            raise PackageError("包解压体积超过声明大小")
                        out.write(block)
                        digest.update(block)
                if copied != item.file_size:
                    raise PackageError("包文件大小不一致")
                files.append({"path": name, "size_bytes": copied, "sha256": digest.hexdigest()})
            paths = {item["path"] for item in files}
            if "manifest.json" not in paths:
                raise PackageError("包根目录必须包含 manifest.json")
            if (stage / "manifest.json").stat().st_size > 2 * 1024 * 1024:
                raise PackageError("包清单过大")
            manifest = _json((stage / "manifest.json").read_bytes())
            validate_manifest(manifest, paths)
        files.sort(key=lambda item: item["path"])
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        final = root / digest
        result = {"package_schema": manifest["package_schema"], "package_id": manifest["package_id"],
                  "package_version": manifest["package_version"], "digest": digest, "root_path": str(final),
                  "manifest": manifest, "cases": manifest["cases"], "files": files, "trusted": False}
        if final.exists():
            # Never trust an existing directory merely because its name is a digest.
            if final.is_symlink() or not final.is_dir():
                raise PackageError("已存包目录无效", "package_changed")
            actual_paths = set()
            for path in final.rglob("*"):
                if path.is_symlink():
                    raise PackageError("已存包包含符号链接", "package_changed")
                if path.is_file():
                    actual_paths.add(path.relative_to(final).as_posix())
            if actual_paths != paths:
                raise PackageError("已存包文件清单被修改", "package_changed")
            for item in files:
                path = final / item["path"]
                if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                    raise PackageError("已存包内容被修改", "package_changed")
            shutil.rmtree(stage)
        else:
            stage.rename(final)
        return result
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise PackageError("无法读取或保存用例包", "package_io_error") from exc
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
