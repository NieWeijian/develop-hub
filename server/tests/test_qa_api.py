from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from develop_hub.db import SessionLocal
from develop_hub.main import app
from develop_hub.models import CodingRun, FirmwareArtifact, RunStatus
from develop_hub.seed_rmsl321_cases import CASES


def make_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "qa@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "QA Test"], cwd=path, check=True)
    (path / "README.md").write_text("# qa fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def test_rmsl321_catalog_has_unique_commands_and_firmware_risk() -> None:
    codes = [item["code"] for item in CASES]
    assert len(codes) == len(set(codes))
    ota = next(item for item in CASES if item["code"] == "QA-L12-OTA-AB")
    assert ota["risk_level"].value == "firmware_write"
    assert "ota-ab" in ota["command"]


def test_create_qa_handoff_and_enforce_result_gate(tmp_path: Path) -> None:
    repository_path = tmp_path / "qa-repository"
    make_repository(repository_path)
    with TestClient(app) as client:
        repository = client.post(
            "/api/repositories",
            json={"name": "qa-fixture-repo", "root_path": str(repository_path), "default_branch": "main"},
        )
        assert repository.status_code == 201
        coding = client.post(
            "/api/runs",
            json={
                "repository_id": repository.json()["id"],
                "title": "Firmware delivery fixture",
                "requirement": "Deliver a firmware change with an explicit QA handoff.",
                "acceptance_criteria": ["Module is detected"],
                "auto_start": False,
            },
        )
        assert coding.status_code == 201
        with SessionLocal() as db:
            run = db.get(CodingRun, coding.json()["id"])
            assert run
            run.status = RunStatus.COMPLETED
            db.commit()

        test_case = client.post(
            "/api/qa/test-cases",
            json={
                "code": "QA-MODULE-DETECT-FIXTURE",
                "name": "Detect module",
                "description": "The connected module must be visible on USB.",
                "command": "lsusb -d 2207:001a | grep -q RMSL321",
                "expected_exit_code": 0,
                "timeout_seconds": 30,
                "enabled": True,
            },
        )
        assert test_case.status_code == 201

        qa_run = client.post(
            "/api/qa/runs",
            json={
                "coding_run_id": coding.json()["id"],
                "test_case_ids": [test_case.json()["id"]],
                "perform_ota": False,
                "auto_start": False,
            },
        )
        assert qa_run.status_code == 201
        body = qa_run.json()
        assert body["status"] == "created"
        assert body["perform_ota"] is False
        assert body["handoff"]["coding_run"]["requirement"] == "Deliver a firmware change with an explicit QA handoff."
        assert body["handoff"]["requested_tests"][0]["code"] == "QA-MODULE-DETECT-FIXTURE"
        assert body["case_executions"][0]["status"] == "pending"

        invalid_approval = client.post(
            f"/api/qa/runs/{body['id']}/decision",
            json={"approved": True, "comment": "Must not bypass controlled commands"},
        )
        assert invalid_approval.status_code == 409

        invalid_ota = client.post(
            "/api/qa/runs",
            json={
                "coding_run_id": coding.json()["id"],
                "test_case_ids": [test_case.json()["id"]],
                "perform_ota": True,
                "auto_start": False,
            },
        )
        assert invalid_ota.status_code == 422

        firmware_case = client.post(
            "/api/qa/test-cases",
            json={
                "code": "QA-OTA-FIXTURE",
                "name": "OTA fixture",
                "description": "Fixture representing a real firmware-write command.",
                "command": "./develop_hub_case_runner ota-ab",
                "expected_exit_code": 0,
                "timeout_seconds": 1800,
                "risk_level": "firmware_write",
                "enabled": True,
            },
        )
        assert firmware_case.status_code == 201
        missing_write_approval = client.post(
            "/api/qa/runs",
            json={
                "coding_run_id": coding.json()["id"],
                "test_case_ids": [firmware_case.json()["id"]],
                "perform_ota": False,
                "auto_start": False,
            },
        )
        assert missing_write_approval.status_code == 422
        firmware_file = tmp_path / "oem.img"
        firmware_file.write_bytes(b"fixture-firmware")
        with SessionLocal() as db:
            artifact = FirmwareArtifact(
                coding_run_id=coding.json()["id"],
                partition="oem",
                filename="oem.img",
                local_path=str(firmware_file),
                sha256="d3a592744d1f6f3f08cf84ca2509973e1d2085929f255ca1af77cbd2b29c64f7",
                size_bytes=firmware_file.stat().st_size,
                source="operator_import",
            )
            db.add(artifact)
            db.commit()
            artifact_id = artifact.id
        approved_write = client.post(
            "/api/qa/runs",
            json={
                "coding_run_id": coding.json()["id"],
                "test_case_ids": [firmware_case.json()["id"]],
                "firmware_artifact_ids": [artifact_id],
                "perform_ota": True,
                "auto_start": False,
            },
        )
        assert approved_write.status_code == 201
        assert approved_write.json()["case_executions"][0]["risk_level"] == "firmware_write"
