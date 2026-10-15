from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import stat
import zipfile

import pytest

from develop_hub.testing.packages import PackageError, import_case_package, validate_parameter_schema, validate_parameters


def manifest():
    return {"package_schema": "rk-case-package/1", "package_id": "example.host-file-check", "package_version": "1.0.0",
            "name": "主机文件检查示例（不验证硬件）", "license": "MIT", "cases": [{
                "case_id": "example.host-file-check", "case_version": "1.0.0", "name": "文件字节数检查", "mode": "automated",
                "test_scope": "只检查运行主机临时文件，不产生硬件验证结论", "requirements": {"capabilities": []},
                "effects": "read_only", "timeout_seconds": 30,
                "parameters_schema": {"type": "object", "additionalProperties": False,
                                      "properties": {"minimum_bytes": {"type": "integer", "minimum": 1, "maximum": 1024, "default": 10}}, "required": ["minimum_bytes"]},
                "parameter_ui": {"minimum_bytes": {"label": "最小字节数", "unit": "bytes"}},
                "implementation": {"runtime_profile": "python3-case", "entrypoint": "run.py", "input_schema": "rk-case-input/1", "result_schema": "rk-case-result/1"},
                "required_assertions": ["file-size"], "recovery": {"required": False}}]}


def package(path, value=None, files=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(value or manifest(), ensure_ascii=False))
        archive.writestr("run.py", "raise RuntimeError('import must not execute me')")
        for name, content in (files or {}).items():
            archive.writestr(name, content)
    return path


def test_import_is_static_and_digest_storage_idempotent(tmp_path):
    archive = package(tmp_path / "case.zip")
    first = import_case_package(archive, tmp_path / "store")
    second = import_case_package(archive, tmp_path / "store")
    assert first == second
    assert first["trusted"] is False
    assert Path(first["root_path"]).name == first["digest"]
    assert {entry["path"] for entry in first["files"]} == {"manifest.json", "run.py"}
    assert not list((tmp_path / "store").glob(".case-import-*"))


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "a\\b", "a/../../escape", "NUL.txt", "file."])
def test_archive_paths_are_rejected_without_writes_outside_store(tmp_path, path):
    archive = package(tmp_path / "case.zip", files={path: "bad"})
    with pytest.raises(PackageError):
        import_case_package(archive, tmp_path / "store")
    assert not (tmp_path / "escape").exists()
    assert not list((tmp_path / "store").iterdir())


def test_symlink_zip_entry_rejected(tmp_path):
    archive = package(tmp_path / "case.zip")
    with zipfile.ZipFile(archive, "a") as output:
        link = zipfile.ZipInfo("symlink")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(link, "/etc/passwd")
    with pytest.raises(PackageError, match="符号链接"):
        import_case_package(archive, tmp_path / "store")


@pytest.mark.parametrize("limits", [{"max_files": 1}, {"max_bytes": 10}])
def test_archive_limits(tmp_path, limits):
    with pytest.raises(PackageError) as error:
        import_case_package(package(tmp_path / "case.zip"), tmp_path / "store", **limits)
    assert error.value.code == "package_too_large"


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(package_schema="legacy/1"),
    lambda m: m.update(package_version="latest"),
    lambda m: m["cases"][0]["implementation"].update(entrypoint="missing.py"),
    lambda m: m["cases"][0]["implementation"].update(runtime_profile="shell"),
    lambda m: m["cases"][0].update(required_assertions=[]),
    lambda m: m["cases"][0].update(effects="firmware_write"),
    lambda m: m["cases"][0]["parameters_schema"].update(**{"$ref": "https://example.invalid/schema"}),
    lambda m: m["cases"][0]["parameters_schema"]["properties"]["minimum_bytes"].update(default=0),
])
def test_unsupported_or_incomplete_contract_rejected(tmp_path, mutation):
    value = manifest()
    mutation(value)
    with pytest.raises(PackageError):
        import_case_package(package(tmp_path / "case.zip", value), tmp_path / "store")


def test_schema_defaults_ranges_booleans_and_unknown_fields():
    schema = manifest()["cases"][0]["parameters_schema"]
    assert validate_parameters(schema, {}) == {"minimum_bytes": 10}
    assert validate_parameters(schema, {"minimum_bytes": 20}) == {"minimum_bytes": 20}
    for values in ({"minimum_bytes": True}, {"minimum_bytes": 1.5}, {"minimum_bytes": 0}, {"extra": 1}):
        with pytest.raises(PackageError):
            validate_parameters(schema, values)
    with pytest.raises(PackageError):
        validate_parameters(schema, {}, apply_defaults=False)


def test_nested_arrays_schema_and_defaults():
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "channels": {"type": "array", "items": {"type": "string", "enum": ["rgb", "depth"]}, "minItems": 1, "default": ["rgb"]},
        "settings": {"type": "object", "additionalProperties": False, "properties": {"enabled": {"type": "boolean", "default": True}}}}, "required": ["channels"]}
    values = {"settings": {}}
    result = validate_parameters(schema, values)
    assert result == {"settings": {"enabled": True}, "channels": ["rgb"]}
    assert values == {"settings": {}}
    with pytest.raises(PackageError):
        validate_parameters(schema, {"channels": ["unknown"]})


def test_schema_unsupported_keywords_and_invalid_defaults_rejected():
    for schema in ({"type": "string", "pattern": "x"}, {"type": "object", "properties": {}},
                   {"type": "number", "default": float("nan")}, {"type": "integer", "enum": [True]},
                   {"type": "array", "items": {"type": "boolean"}, "default": [1]}):
        with pytest.raises(PackageError):
            validate_parameter_schema(schema)


@pytest.mark.parametrize("tamper", ["change", "extra"])
def test_existing_digest_directory_is_verified_before_reuse(tmp_path, tamper):
    archive = package(tmp_path / "case.zip")
    result = import_case_package(archive, tmp_path / "store")
    (Path(result["root_path"]) / ("run.py" if tamper == "change" else "injected.py")).write_text("unexpected")
    with pytest.raises(PackageError) as error:
        import_case_package(archive, tmp_path / "store")
    assert error.value.code == "package_changed"


def test_same_version_different_contents_have_different_digest_for_publish_conflict(tmp_path):
    first = import_case_package(package(tmp_path / "one.zip"), tmp_path / "store")
    second = import_case_package(package(tmp_path / "two.zip", files={"extra.txt": "new version content"}), tmp_path / "store")
    assert first["package_version"] == second["package_version"]
    assert first["digest"] != second["digest"]
