from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .db import SessionLocal
from .models import (
    CodingRun,
    QaPluginRelease,
    QaPluginStatus,
    TestCase,
    TestCaseRisk,
    new_id,
)
from .qa_runner import QaRunnerError, RemoteQaRunner


PLUGIN_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,79}$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
MAX_PLUGIN_BYTES = 200 * 1024 * 1024
MAX_PLUGIN_FILES = 1000


def _now() -> datetime:
    return datetime.now(timezone.utc)


class QaPluginError(RuntimeError):
    pass


class PluginCaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=2)
    entrypoint: str = Field(min_length=1, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=100)
    expected_exit_code: int = 0
    timeout_seconds: int = Field(default=300, ge=1, le=172800)
    risk_level: TestCaseRisk = TestCaseRisk.READ_ONLY
    sudo: bool = False

    @field_validator("entrypoint")
    @classmethod
    def safe_entrypoint(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value.endswith("/"):
            raise ValueError("entrypoint must be a relative file path inside the plugin")
        return path.as_posix()

    @field_validator("args")
    @classmethod
    def safe_args(cls, value: list[str]) -> list[str]:
        if any("\x00" in item or len(item) > 1000 for item in value):
            raise ValueError("plugin arguments contain invalid values")
        return value


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    plugin_id: str = Field(pattern=PLUGIN_ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    name: str = Field(min_length=2, max_length=200)
    summary: str = Field(min_length=2, max_length=8000)
    cases: list[PluginCaseManifest] = Field(min_length=1, max_length=100)

    @field_validator("cases")
    @classmethod
    def unique_case_codes(cls, value: list[PluginCaseManifest]) -> list[PluginCaseManifest]:
        codes = [item.code for item in value]
        if len(codes) != len(set(codes)):
            raise ValueError("case codes must be unique within a plugin")
        return value


class QaPluginRegistry:
    def __init__(self, settings: Settings, runner: RemoteQaRunner) -> None:
        self.settings = settings
        self.runner = runner

    def discover_from_coding_run(self, run_id: str) -> list[str]:
        with SessionLocal() as db:
            run = db.get(CodingRun, run_id)
            if not run or not run.worktree_path:
                raise QaPluginError("Coding run or worktree not found for QA plugin discovery")
            worktree = Path(run.worktree_path).resolve()

        plugin_root = (worktree / "tools" / "qa-plugins").resolve()
        if not plugin_root.is_dir():
            return []
        if worktree not in plugin_root.parents:
            raise QaPluginError("QA plugin root escaped the coding worktree")
        manifests = sorted(plugin_root.glob("*/manifest.json"))
        release_ids: list[str] = []
        for manifest_path in manifests:
            release_ids.append(self._register_manifest(run_id, worktree, manifest_path))
        return release_ids

    def _register_manifest(self, run_id: str, worktree: Path, manifest_path: Path) -> str:
        try:
            manifest = PluginManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise QaPluginError(f"Invalid QA plugin manifest {manifest_path}: {exc}") from exc
        source_dir = manifest_path.parent.resolve()
        if worktree not in source_dir.parents:
            raise QaPluginError("QA plugin source escaped the coding worktree")
        files = self._validated_files(source_dir, manifest)
        digest = self._content_digest(source_dir, files)

        with SessionLocal() as db:
            existing = db.scalar(
                select(QaPluginRelease).where(
                    QaPluginRelease.plugin_id == manifest.plugin_id,
                    QaPluginRelease.version == manifest.version,
                )
            )
            if existing:
                if existing.sha256 == digest:
                    return existing.id
                raise QaPluginError(
                    f"QA plugin {manifest.plugin_id}@{manifest.version} already exists with different content; "
                    "bump the plugin version"
                )
            release_id = new_id()
            release_dir = (self.settings.qa_plugin_root / release_id).resolve()
            if self.settings.qa_plugin_root.resolve() not in release_dir.parents:
                raise QaPluginError("QA plugin artifact path escaped the configured root")
            release_dir.mkdir(parents=True, exist_ok=False)
            bundle_path = release_dir / "plugin.tar.gz"
            with tarfile.open(bundle_path, "w:gz") as archive:
                for file_path in files:
                    archive.add(file_path, arcname=file_path.relative_to(source_dir).as_posix(), recursive=False)
            release = QaPluginRelease(
                id=release_id,
                coding_run_id=run_id,
                plugin_id=manifest.plugin_id,
                version=manifest.version,
                name=manifest.name,
                summary=manifest.summary,
                source_relpath=source_dir.relative_to(worktree).as_posix(),
                manifest=manifest.model_dump(mode="json"),
                sha256=digest,
                bundle_path=str(bundle_path),
            )
            try:
                db.add(release)
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                shutil.rmtree(release_dir, ignore_errors=True)
                raise QaPluginError("QA plugin release registration conflicted with existing content") from exc
            return release.id

    @staticmethod
    def _validated_files(source_dir: Path, manifest: PluginManifest) -> list[Path]:
        files: list[Path] = []
        total_size = 0
        for path in sorted(source_dir.rglob("*")):
            if path.is_symlink():
                raise QaPluginError(f"QA plugins cannot contain symlinks: {path}")
            if not path.is_file():
                continue
            files.append(path)
            total_size += path.stat().st_size
            if len(files) > MAX_PLUGIN_FILES or total_size > MAX_PLUGIN_BYTES:
                raise QaPluginError("QA plugin exceeds the file-count or size limit")
        if not files or source_dir / "manifest.json" not in files:
            raise QaPluginError("QA plugin is missing manifest.json")
        for case in manifest.cases:
            entrypoint = (source_dir / case.entrypoint).resolve()
            if source_dir not in entrypoint.parents or not entrypoint.is_file():
                raise QaPluginError(f"QA plugin entrypoint not found: {case.entrypoint}")
            if not os.access(entrypoint, os.X_OK):
                raise QaPluginError(f"QA plugin entrypoint is not executable: {case.entrypoint}")
        return files

    @staticmethod
    def _content_digest(source_dir: Path, files: list[Path]) -> str:
        digest = hashlib.sha256()
        for path in files:
            relative = path.relative_to(source_dir).as_posix().encode()
            executable = b"1" if os.access(path, os.X_OK) else b"0"
            digest.update(relative + b"\x00" + executable + b"\x00")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    async def install(self, release_id: str) -> None:
        with SessionLocal() as db:
            release = db.get(QaPluginRelease, release_id)
            if not release:
                raise QaPluginError("QA plugin release not found")
            if release.status == QaPluginStatus.INSTALLED:
                return
            if release.status == QaPluginStatus.INSTALLING:
                raise QaPluginError("QA plugin release is already installing")
            release.status = QaPluginStatus.INSTALLING
            release.last_error = None
            db.commit()
            manifest = PluginManifest.model_validate(release.manifest)
            bundle_path = Path(release.bundle_path)
            plugin_id = release.plugin_id
            version = release.version
            digest = release.sha256

        try:
            if not bundle_path.is_file():
                raise QaPluginError("QA plugin bundle is missing on the 175 Server")
            remote_path = await self.runner.install_plugin(
                bundle_path,
                plugin_id,
                version,
                digest,
                [case.entrypoint for case in manifest.cases],
            )
            with SessionLocal() as db:
                release = db.get(QaPluginRelease, release_id)
                assert release
                existing_cases = list(db.scalars(select(TestCase).where(TestCase.plugin_release_id == release_id)))
                if not existing_cases:
                    for case in manifest.cases:
                        command_parts = [f"{remote_path}/{case.entrypoint}", *case.args]
                        if case.sudo:
                            command_parts = ["sudo", "-n", *command_parts]
                        db.add(
                            TestCase(
                                code=case.code,
                                name=case.name,
                                description=case.description,
                                command=shlex.join(command_parts),
                                expected_exit_code=case.expected_exit_code,
                                timeout_seconds=case.timeout_seconds,
                                risk_level=case.risk_level,
                                plugin_release_id=release.id,
                                plugin_id=release.plugin_id,
                                plugin_version=release.version,
                                enabled=True,
                            )
                        )
                release.status = QaPluginStatus.INSTALLED
                release.remote_path = remote_path
                release.installed_at = _now()
                release.last_error = None
                db.commit()
        except (QaPluginError, QaRunnerError, OSError) as exc:
            with SessionLocal() as db:
                release = db.get(QaPluginRelease, release_id)
                if release:
                    release.status = QaPluginStatus.FAILED
                    release.last_error = str(exc)
                    db.commit()
            raise QaPluginError(str(exc)) from exc
