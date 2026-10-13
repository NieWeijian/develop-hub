from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from develop_hub.db import SessionLocal
from develop_hub.main import app
from develop_hub.models import CodingRun, QaRun, QaRunStatus, Repository, RunStatus, WorktreeMode


def make_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("# api fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "branch", "stable"], cwd=path, check=True, capture_output=True)


def test_create_qa_linked_followup_coding_run(tmp_path: Path) -> None:
    repository_path = tmp_path / "followup-api-repository"
    make_repository(repository_path)

    with TestClient(app) as client:
        repository_response = client.post(
            "/api/repositories",
            json={
                "name": "followup-api-fixture-repo",
                "root_path": str(repository_path),
                "default_branch": "main",
            },
        )
        assert repository_response.status_code == 201
        repository_id = repository_response.json()["id"]

        with SessionLocal() as db:
            repository = db.get(Repository, repository_id)
            assert repository
            parent = CodingRun(
                display_number=900001,
                repository_id=repository.id,
                title="Parent coding run",
                requirement="Implement the original feature before QA handoff.",
                acceptance_criteria=["Original behavior passes"],
                source_branch="main",
                work_branch="existing/followup-parent",
                worktree_mode=WorktreeMode.EXISTING,
                worktree_path=str(repository_path),
                worktree_branch="main",
                worktree_initial_branch="main",
                status=RunStatus.COMPLETED,
            )
            db.add(parent)
            db.flush()
            qa_run = QaRun(
                display_number=900001,
                coding_run_id=parent.id,
                status=QaRunStatus.PASSED,
                handoff={},
                report={"summary": "QA found a follow-up question"},
            )
            db.add(qa_run)
            db.commit()
            parent_id = parent.id
            qa_run_id = qa_run.id

        readiness = client.get(f"/api/runs/{parent_id}/merge-readiness")
        assert readiness.status_code == 200
        assert readiness.json()["status"] == "already_default"
        assert readiness.json()["qa_run_id"] == qa_run_id

        followup = client.post(
            "/api/runs",
            json={
                "repository_id": repository_id,
                "parent_run_id": parent_id,
                "source_qa_run_id": qa_run_id,
                "title": "QA feedback follow-up",
                "requirement": "Investigate the linked QA evidence and implement the required correction.",
                "worktree_mode": "reuse_new_branch",
                "existing_worktree_path": str(repository_path),
                "source_branch": "main",
                "auto_start": False,
            },
        )
        assert followup.status_code == 201, followup.text
        assert followup.json()["parent_run_id"] == parent_id
        assert followup.json()["source_qa_run_id"] == qa_run_id
        assert followup.json()["worktree_mode"] == "reuse_new_branch"


def test_import_operator_built_firmware_at_completion_gate(tmp_path: Path) -> None:
    repository_path = tmp_path / "firmware-import-repository"
    make_repository(repository_path)
    image_root = repository_path / "output" / "image"
    image_root.mkdir(parents=True)
    (repository_path / "build.sh").write_text("#!/bin/sh\nset -eu\n", encoding="utf-8")
    (image_root / "oem.img").write_bytes(b"operator-built-firmware")

    with TestClient(app) as client:
        repository = client.post(
            "/api/repositories",
            json={
                "name": "firmware-import-fixture-repo",
                "root_path": str(repository_path),
                "default_branch": "main",
                "build_root": str(repository_path),
                "firmware_image_root": str(image_root),
            },
        )
        assert repository.status_code == 201
        coding = client.post(
            "/api/runs",
            json={
                "repository_id": repository.json()["id"],
                "title": "Operator firmware import fixture",
                "requirement": "Register an existing operator-built image at the completion gate.",
                "auto_start": False,
            },
        )
        assert coding.status_code == 201
        with SessionLocal() as db:
            run = db.get(CodingRun, coding.json()["id"])
            assert run
            run.status = RunStatus.AWAITING_COMPLETION_APPROVAL
            db.commit()

        imported = client.post(
            f"/api/runs/{coding.json()['id']}/firmware-artifacts/import",
            json={"partitions": ["oem"]},
        )
        assert imported.status_code == 200
        artifact = imported.json()["firmware_artifacts"][0]
        assert artifact["partition"] == "oem"
        assert artifact["source"] == "operator_import"
        assert artifact["local_path"] == str((image_root / "oem.img").resolve())
        assert len(artifact["sha256"]) == 64

        missing = client.post(
            f"/api/runs/{coding.json()['id']}/firmware-artifacts/import",
            json={"partitions": ["boot"]},
        )
        assert missing.status_code == 409


def test_register_repository_and_create_draft_run(tmp_path: Path) -> None:
    repository_path = tmp_path / "api-repository"
    make_repository(repository_path)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["node_id"] == "175-coding"

        invalid_repository = client.post(
            "/api/repositories",
            json={
                "name": "api-fixture-repo",
                "root_path": str(repository_path),
                "default_branch": "missing",
                "validation_commands": ["git diff --check"],
            },
        )
        assert invalid_repository.status_code == 422
        assert "does not exist" in invalid_repository.json()["detail"]

        created_repository = client.post(
            "/api/repositories",
            json={
                "name": "api-fixture-repo",
                "root_path": str(repository_path),
                "default_branch": "main",
                "validation_commands": ["git diff --check"],
            },
        )
        assert created_repository.status_code == 201
        repository_id = created_repository.json()["id"]

        worktrees = client.get(f"/api/repositories/{repository_id}/worktrees")
        assert worktrees.status_code == 200
        main_worktree = next(item for item in worktrees.json()["worktrees"] if item["is_main"])
        assert main_worktree["path"] == str(repository_path)
        assert main_worktree["branch"] == "main"

        existing_worktree_run = client.post(
            "/api/runs",
            json={
                "repository_id": repository_id,
                "title": "Existing worktree fixture",
                "requirement": "Use the selected existing worktree without creating or deleting it.",
                "worktree_mode": "existing",
                "existing_worktree_path": str(repository_path),
                "auto_start": False,
            },
        )
        assert existing_worktree_run.status_code == 201
        assert existing_worktree_run.json()["worktree_mode"] == "existing"
        assert existing_worktree_run.json()["worktree_branch"] == "main"
        assert existing_worktree_run.json()["worktree_path"] == str(repository_path)
        existing_record_delete = client.delete(
            f"/api/runs/{existing_worktree_run.json()['id']}/record-only"
        )
        assert existing_record_delete.status_code == 204
        assert repository_path.exists()

        reuse_worktree_run = client.post(
            "/api/runs",
            json={
                "repository_id": repository_id,
                "title": "Reuse worktree with new branch",
                "requirement": "Create a new task branch in the selected existing worktree.",
                "worktree_mode": "reuse_new_branch",
                "existing_worktree_path": str(repository_path),
                "source_branch": "stable",
                "auto_start": False,
            },
        )
        assert reuse_worktree_run.status_code == 201
        reuse_body = reuse_worktree_run.json()
        assert reuse_body["worktree_mode"] == "reuse_new_branch"
        assert reuse_body["source_branch"] == "stable"
        assert reuse_body["worktree_initial_branch"] == "main"
        assert reuse_body["worktree_branch"].startswith("codex/run-")
        assert reuse_body["worktree_path"] == str(repository_path)
        assert client.delete(f"/api/runs/{reuse_body['id']}/record-only").status_code == 204

        dirty_marker = repository_path / "untracked.txt"
        dirty_marker.write_text("dirty\n", encoding="utf-8")
        untracked_reuse = client.post(
            "/api/runs",
            json={
                "repository_id": repository_id,
                "title": "Untracked worktree accepted",
                "requirement": "Allow branch creation while preserving unrelated untracked content.",
                "worktree_mode": "reuse_new_branch",
                "existing_worktree_path": str(repository_path),
                "source_branch": "main",
                "auto_start": False,
            },
        )
        assert untracked_reuse.status_code == 201
        assert client.delete(f"/api/runs/{untracked_reuse.json()['id']}/record-only").status_code == 204
        dirty_marker.unlink()

        readme = repository_path / "README.md"
        readme.write_text("# tracked change\n", encoding="utf-8")
        tracked_reuse = client.post(
            "/api/runs",
            json={
                "repository_id": repository_id,
                "title": "Tracked worktree rejected",
                "requirement": "Do not switch a worktree with tracked uncommitted changes.",
                "worktree_mode": "reuse_new_branch",
                "existing_worktree_path": str(repository_path),
                "source_branch": "main",
                "auto_start": False,
            },
        )
        assert tracked_reuse.status_code == 422
        assert "tracked, staged, or conflicted changes" in tracked_reuse.json()["detail"]
        subprocess.run(["git", "restore", "README.md"], cwd=repository_path, check=True)

        unused_repository_path = tmp_path / "unused-api-repository"
        make_repository(unused_repository_path)
        unused_repository = client.post(
            "/api/repositories",
            json={
                "name": "unused-api-fixture-repo",
                "root_path": str(unused_repository_path),
                "default_branch": "main",
                "validation_commands": [],
            },
        )
        assert unused_repository.status_code == 201
        deleted_repository = client.delete(f"/api/repositories/{unused_repository.json()['id']}")
        assert deleted_repository.status_code == 204
        assert unused_repository_path.exists()
        assert all(
            item["id"] != unused_repository.json()["id"]
            for item in client.get("/api/repositories").json()
        )

        branches = client.get(f"/api/repositories/{repository_id}/branches")
        assert branches.status_code == 200
        assert branches.json()["branches"] == ["main", "stable"]

        invalid_update = client.put(
            f"/api/repositories/{repository_id}",
            json={
                "name": "api-fixture-repo",
                "root_path": str(repository_path),
                "default_branch": "missing",
                "validation_commands": ["git diff --check"],
                "enabled": True,
            },
        )
        assert invalid_update.status_code == 422

        updated_repository = client.put(
            f"/api/repositories/{repository_id}",
            json={
                "name": "api-fixture-repo",
                "root_path": str(repository_path),
                "default_branch": "stable",
                "validation_commands": ["git diff --check", "git status --short"],
                "enabled": True,
            },
        )
        assert updated_repository.status_code == 200
        assert updated_repository.json()["default_branch"] == "stable"

        run_count = len(client.get("/api/runs").json())
        invalid_run = client.post(
            "/api/runs",
            json={
                "repository_id": repository_id,
                "title": "Invalid source branch",
                "requirement": "This run must be rejected before a database record is created.",
                "source_branch": "missing",
                "auto_start": False,
            },
        )
        assert invalid_run.status_code == 422
        assert len(client.get("/api/runs").json()) == run_count

        created_run = client.post(
            "/api/runs",
            json={
                "repository_id": repository_id,
                "title": "API fixture run",
                "requirement": "Create a draft run without launching the Codex subprocess.",
                "acceptance_criteria": ["Run remains in created state"],
                "auto_start": False,
            },
        )
        assert created_run.status_code == 201
        body = created_run.json()
        assert body["status"] == "created"
        assert body["source_branch"] == "stable"
        assert body["work_branch"].startswith("codex/run-")
        assert body["id"][:8] in body["work_branch"]

        events = client.get(f"/api/runs/{body['id']}/events")
        assert events.status_code == 200
        assert events.json()[0]["event_type"] == "workflow.created"

        active_delete = client.delete(f"/api/runs/{body['id']}")
        assert active_delete.status_code == 409

        with SessionLocal() as db:
            run = db.get(CodingRun, body["id"])
            assert run
            run.status = RunStatus.FAILED
            worktree_path = app.state.workflow.settings.worktree_root / body["id"]
            subprocess.run(
                ["git", "worktree", "add", "-b", body["work_branch"], str(worktree_path), "stable"],
                cwd=repository_path,
                check=True,
                capture_output=True,
            )
            run.worktree_path = str(worktree_path)
            db.commit()

        artifact_path = app.state.workflow.settings.artifact_root / body["id"]
        artifact_path.mkdir(parents=True)
        (artifact_path / "event.jsonl").write_text("fixture\n", encoding="utf-8")

        deleted = client.delete(f"/api/runs/{body['id']}")
        assert deleted.status_code == 204
        assert not worktree_path.exists()
        assert not artifact_path.exists()
        branches_after_delete = subprocess.run(
            ["git", "branch", "--list", body["work_branch"], "--format=%(refname:short)"],
            cwd=repository_path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert branches_after_delete.stdout.strip() == ""
        assert client.get(f"/api/runs/{body['id']}").status_code == 404
        assert client.get(f"/api/runs/{body['id']}/events").status_code == 404

        completed_run = client.post(
            "/api/runs",
            json={
                "repository_id": repository_id,
                "title": "Completed deletable fixture",
                "requirement": "Verify that an explicitly deleted completed run is removed.",
                "auto_start": False,
            },
        )
        assert completed_run.status_code == 201
        assert completed_run.json()["display_number"] == body["display_number"] + 1
        with SessionLocal() as db:
            persisted = db.get(CodingRun, completed_run.json()["id"])
            assert persisted
            persisted.status = RunStatus.COMPLETED
            db.commit()
        completed_delete = client.delete(f"/api/runs/{completed_run.json()['id']}")
        assert completed_delete.status_code == 204

        next_run = client.post(
            "/api/runs",
            json={
                "repository_id": repository_id,
                "title": "API fixture run",
                "requirement": "Create another run after deleting the previous database record.",
                "auto_start": False,
            },
        )
        assert next_run.status_code == 201
        next_body = next_run.json()
        assert next_body["display_number"] == completed_run.json()["display_number"] + 1
        assert next_body["work_branch"] != body["work_branch"]
        referenced_repository_delete = client.delete(f"/api/repositories/{repository_id}")
        assert referenced_repository_delete.status_code == 409
        assert "运行记录" in referenced_repository_delete.json()["detail"]
        record_only_delete = client.delete(f"/api/runs/{next_body['id']}/record-only")
        assert record_only_delete.status_code == 204
        assert repository_path.exists()
        repository_delete = client.delete(f"/api/repositories/{repository_id}")
        assert repository_delete.status_code == 204
        assert repository_path.exists()
