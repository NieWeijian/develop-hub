from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from develop_hub.config import settings
from develop_hub.db import SessionLocal
from develop_hub.models import CodingRun, QaPluginRelease, QaPluginStatus, Repository, TestCase as QaTestCase, new_id
from develop_hub.qa_plugins import QaPluginError, QaPluginRegistry


class FakeQaRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str, str, list[str]]] = []

    async def install_plugin(
        self,
        bundle_path: Path,
        plugin_id: str,
        version: str,
        digest: str,
        entrypoints: list[str],
    ) -> str:
        self.calls.append((bundle_path, plugin_id, version, digest, entrypoints))
        return f"/home/linaro/develop-hub-qa/plugins/{plugin_id}/{version}/{digest[:16]}"


def create_coding_run(worktree: Path) -> str:
    repository_id = new_id()
    run_id = new_id()
    with SessionLocal() as db:
        db.add(
            Repository(
                id=repository_id,
                name=f"plugin-fixture-{repository_id}",
                root_path=str(worktree.parent / repository_id),
                default_branch="main",
            )
        )
        db.add(
            CodingRun(
                id=run_id,
                display_number=900000 + int(run_id[:4], 16),
                repository_id=repository_id,
                title="QA plugin fixture",
                requirement="Deliver a versioned QA plugin fixture.",
                acceptance_criteria=[],
                source_branch="main",
                work_branch=f"codex/plugin-{run_id}",
                worktree_branch=f"codex/plugin-{run_id}",
                worktree_path=str(worktree),
            )
        )
        db.commit()
    return run_id


def write_plugin(worktree: Path, *, version: str = "1.0.0", script: str = "#!/bin/sh\nexit 0\n") -> Path:
    plugin_dir = worktree / "tools" / "qa-plugins" / "exposure-readback"
    plugin_dir.mkdir(parents=True)
    entrypoint = plugin_dir / "run-case"
    entrypoint.write_text(script, encoding="utf-8")
    entrypoint.chmod(0o755)
    manifest = {
        "schema_version": 1,
        "plugin_id": "exposure-readback",
        "version": version,
        "name": "Exposure readback",
        "summary": "Read back exposure metadata added by this Coding Run.",
        "cases": [
            {
                "code": "QA-EXPOSURE-READBACK",
                "name": "Exposure metadata readback",
                "description": "Start one capture and require exposure metadata to be present.",
                "entrypoint": "run-case",
                "args": ["--frames", "1"],
                "expected_exit_code": 0,
                "timeout_seconds": 60,
                "risk_level": "read_only",
                "sudo": False,
            }
        ],
    }
    (plugin_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return plugin_dir


@pytest.mark.asyncio
async def test_register_and_install_versioned_plugin(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    plugin_dir = write_plugin(worktree)
    run_id = create_coding_run(worktree)
    runner = FakeQaRunner()
    registry = QaPluginRegistry(settings, runner)  # type: ignore[arg-type]

    release_ids = registry.discover_from_coding_run(run_id)
    assert len(release_ids) == 1
    assert registry.discover_from_coding_run(run_id) == release_ids

    with SessionLocal() as db:
        release = db.get(QaPluginRelease, release_ids[0])
        assert release
        assert release.status == QaPluginStatus.PENDING
        assert Path(release.bundle_path).is_file()
        assert release.source_relpath == plugin_dir.relative_to(worktree).as_posix()

    await registry.install(release_ids[0])

    with SessionLocal() as db:
        release = db.get(QaPluginRelease, release_ids[0])
        cases = list(db.scalars(select(QaTestCase).where(QaTestCase.plugin_release_id == release_ids[0])))
        assert release and release.status == QaPluginStatus.INSTALLED
        assert release.remote_path and release.sha256[:16] in release.remote_path
        assert len(cases) == 1
        assert cases[0].plugin_id == "exposure-readback"
        assert cases[0].plugin_version == "1.0.0"
        assert cases[0].command == f"{release.remote_path}/run-case --frames 1"
    assert runner.calls[0][4] == ["run-case"]


def test_same_plugin_version_cannot_change_content(tmp_path: Path) -> None:
    first = tmp_path / "first"
    write_plugin(first)
    first_run = create_coding_run(first)
    registry = QaPluginRegistry(settings, FakeQaRunner())  # type: ignore[arg-type]
    registry.discover_from_coding_run(first_run)

    second = tmp_path / "second"
    write_plugin(second, script="#!/bin/sh\necho changed\n")
    second_run = create_coding_run(second)
    with pytest.raises(QaPluginError, match="bump the plugin version"):
        registry.discover_from_coding_run(second_run)


@pytest.mark.asyncio
async def test_plugin_versions_coexist_with_same_case_code(tmp_path: Path) -> None:
    runner = FakeQaRunner()
    registry = QaPluginRegistry(settings, runner)  # type: ignore[arg-type]
    release_ids: list[str] = []
    for directory, version in (("v3", "3.0.0"), ("v31", "3.1.0")):
        worktree = tmp_path / directory
        write_plugin(worktree, version=version, script=f"#!/bin/sh\necho {version}\n")
        run_id = create_coding_run(worktree)
        release_id = registry.discover_from_coding_run(run_id)[0]
        await registry.install(release_id)
        release_ids.append(release_id)

    with SessionLocal() as db:
        cases = list(
            db.scalars(
                select(QaTestCase).where(QaTestCase.plugin_release_id.in_(release_ids)).order_by(QaTestCase.plugin_version)
            )
        )
        assert [case.code for case in cases] == ["QA-EXPOSURE-READBACK", "QA-EXPOSURE-READBACK"]
        assert [case.plugin_version for case in cases] == ["3.0.0", "3.1.0"]
        assert cases[0].command != cases[1].command


def test_plugin_rejects_symlink(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    plugin_dir = write_plugin(worktree, version="2.0.0")
    (plugin_dir / "escaped").symlink_to("/etc/hosts")
    run_id = create_coding_run(worktree)
    registry = QaPluginRegistry(settings, FakeQaRunner())  # type: ignore[arg-type]
    with pytest.raises(QaPluginError, match="cannot contain symlinks"):
        registry.discover_from_coding_run(run_id)
