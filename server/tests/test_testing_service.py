from __future__ import annotations

from copy import deepcopy

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from develop_hub.db import Base, get_db
from develop_hub.testing.api import router
from develop_hub.testing.models import (
    BuildEnvironment, CaseRevision, Device, InputAsset, OperatorRecord, PlanRevision,
    Product, TestPlan as Plan, TestRun as Run,
)
from develop_hub.testing import service
from develop_hub.testing.schemas import CaseCreate, RunCreate


class RunnerSpy:
    def __init__(self):
        self.started = []
        self.resumed = []
        self.cancelled = []

    async def start(self, identity):
        self.started.append(identity)

    async def resume(self, identity):
        self.resumed.append(identity)

    async def cancel(self, identity):
        self.cancelled.append(identity)


@pytest.fixture
def setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [table for table in Base.metadata.sorted_tables if table.name.startswith("testing_")]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)
    runner = RunnerSpy()
    app.state.testing_runner = runner

    def sessions():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = sessions
    with factory() as db:
        product = Product(name="测试模组", chip="RK1126", capabilities=["version.read"])
        db.add(product)
        db.flush()
        device = Device(name="模拟设备", product_id=product.id, node={"kind": "simulation"}, capabilities=["temperature.read"])
        db.add(device)
        case = service.create_case(db, CaseCreate(case_id="example.temperature", version="1.0.0", name="温度检查", mode="automated", definition={
            "parameters_schema": {"type": "object", "additionalProperties": False, "properties": {
                "limit": {"type": "number", "minimum": 0, "maximum": 100, "default": 75},
                "samples": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
            }, "required": ["limit", "samples"]},
            "implementation": {"runtime_profile": "simulation"},
            "requirements": {"capabilities": ["temperature.read"]},
        }))
        db.flush()
        config = {"product_id": product.id, "device_id": device.id, "firmware": {"kind": "current_device"},
                  "cases": [{"binding_id": "temperature", "case_revision_id": case.id, "required": True,
                             "parameter_overrides": {"limit": 60}}], "acceptance": {"mode": "all_required_cases"}}
        plan = Plan(name="每日检查", draft=deepcopy(config))
        db.add(plan)
        db.commit()
        ids = {"product": product.id, "device": device.id, "case": case.id, "plan": plan.id}
    with TestClient(app) as client:
        yield client, factory, ids, config, runner
    engine.dispose()


def publish(client, identity, revision=1):
    response = client.post(f"/api/testing/test-plans/{identity}/revisions", json={"revision": revision})
    assert response.status_code == 201, response.text
    return response.json()


def test_managed_station_registration_and_protected_configuration(setup, tmp_path, monkeypatch):
    import json
    client, factory, ids, _, _ = setup
    registry = tmp_path / 'stations.json'
    registry.write_text(json.dumps({'schema': 'rk-test-stations/1', 'stations': {'lab': {
        'name': '测试台 A', 'chip': 'RK1126', 'adapter_name': '内置适配器', 'functions': ['读取版本'],
        'node': {'kind': 'ssh', 'host': 'station.test', 'user': 'tester', 'key': '/private/key'},
        'capabilities': ['version.read'], 'config': {'ctl': {'path': '/private/tool'},
            'adapter': {'inspect_argv': ['python3', '/private/driver', 'inspect']}},
    }}}))
    monkeypatch.setenv('DEVELOP_HUB_TEST_STATIONS', str(registry))
    response = client.get('/api/testing/test-stations')
    assert response.status_code == 200
    assert response.json()[0]['name'] == '测试台 A'
    assert '/private' not in response.text and 'inspect_argv' not in response.text
    payload = {'name': '真机', 'product_id': ids['product'], 'station_id': 'lab', 'serial': 'abcdef1234'}
    response = client.post('/api/testing/devices', json=payload)
    assert response.status_code == 201, response.text
    device = response.json()
    assert device['config']['managed_station_id'] == 'lab'
    assert device['config']['ctl']['path'] == '/private/tool'
    assert device['capabilities'] == ['version.read']
    for field, value in [('config', {}), ('node', {'kind': 'local'}), ('capabilities', ['fake'])]:
        assert client.post('/api/testing/devices', json={**payload, field: value}).status_code == 422
        assert client.patch('/api/testing/devices/' + device['id'], json={field: value}).status_code == 422
    response = client.patch('/api/testing/devices/' + device['id'], json={'name': '改名'})
    assert response.status_code == 200 and response.json()['config'] == device['config']
    assert client.post('/api/testing/devices', json={**payload, 'serial': '0' * 16}).status_code == 422
    assert client.post('/api/testing/devices', json={**payload, 'station_id': 'missing'}).status_code == 422
    assert client.post('/api/testing/devices', json={'name': 'custom', 'product_id': ids['product'], 'node': {'kind': 'local'}}).status_code == 422
    with factory() as db:
        db.get(Product, ids['product']).chip = 'OTHER'
        db.commit()
    assert client.post('/api/testing/devices', json=payload).status_code == 422
    registry.write_text('{broken')
    assert client.get('/api/testing/test-stations').status_code == 503


def test_fresh_product_needs_no_sdk_or_gerrit(setup):
    client, factory, ids, _, _ = setup
    result = client.post("/api/testing/products", json={"name": "只读固件测试"})
    assert result.status_code == 201
    assert "sdk_repository_id" not in result.json()
    preview = client.post(f"/api/testing/test-plans/{ids['plan']}/validate", json={}).json()
    assert preview["valid"]
    assert preview["resolved"]["environment"] is None
    assert preview["warnings"]


def test_parameter_defaults_plan_run_precedence_and_unknown_fields(setup):
    client, _, ids, _, _ = setup
    url = f"/api/testing/test-plans/{ids['plan']}/validate"
    preview = client.post(url, json={"parameter_overrides": {"temperature": {"samples": 8}}}).json()
    assert preview["valid"]
    assert preview["parameters"]["temperature"] == {"limit": 60, "samples": 8}
    assert preview["parameter_sources"]["temperature"] == {"limit": "plan", "samples": "run"}
    defaults = client.post(url, json={}).json()
    assert defaults["parameters"]["temperature"]["samples"] == 3
    assert defaults["parameter_sources"]["temperature"]["samples"] == "default"
    bad = client.post(url, json={"parameter_overrides": {"temperature": {"typo": 1}}}).json()
    assert not bad["valid"] and bad["errors"][0]["code"] == "invalid_parameters"
    bad_binding = client.post(url, json={"parameter_overrides": {"unknown": {"samples": 1}}}).json()
    assert not bad_binding["valid"]
    assert any(error["code"] == "unknown_binding" for error in bad_binding["errors"])


def test_draft_cas_copy_and_immutable_publication(setup):
    client, factory, ids, config, _ = setup
    url = f"/api/testing/test-plans/{ids['plan']}"
    response = client.patch(url + "/draft", json={"revision": 1, "draft": config, "name": "更新标题"})
    assert response.status_code == 200 and response.json()["revision"] == 2
    assert client.patch(url + "/draft", json={"revision": 1, "draft": {}}).status_code == 409
    fixed = publish(client, ids["plan"], 2)
    assert fixed["draft_revision"] == 3
    assert client.patch(url + "/draft", json={"revision": 3, "draft": {}}).status_code == 200
    with factory() as db:
        assert db.get(PlanRevision, fixed["id"]).config == config
    copy = client.post(url + "/copy", json={}).json()
    assert copy["id"] != ids["plan"] and copy["revision"] == 1
    assert client.get(f"/api/testing/test-plans/{copy['id']}").json()["revisions"] == []


def test_create_run_freezes_environment_and_case_defaults(setup):
    client, factory, ids, _, _ = setup
    fixed = publish(client, ids["plan"])
    # A product's live display/config edits cannot alter an already published
    # plan's material inputs, and a new case version cannot replace the old one.
    with factory() as db:
        db.get(Product, ids["product"]).name = "修改后的产品"
        db.add(CaseRevision(case_id="example.temperature", version="2.0.0", name="新版", mode="automated", definition={}))
        db.commit()
    response = client.post("/api/testing/test-runs", json={"plan_revision_id": fixed["id"], "idempotency_key": "frozen-1"})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["request_snapshot"]["product"]["name"] == "测试模组"
    assert run["request_snapshot"]["cases"][0]["version"] == "1.0.0"
    assert run["request_snapshot"]["cases"][0]["parameters"]["samples"] == 3
    assert run["summary"]["simulated"] is True


def test_save_and_run_idempotency_and_payload_conflict(setup):
    client, factory, ids, _, _ = setup
    payload = {"plan_id": ids["plan"], "draft_revision": 1, "idempotency_key": "same-request"}
    first = client.post("/api/testing/test-runs", json=payload)
    assert first.status_code == 201, first.text
    second = client.post("/api/testing/test-runs", json=payload)
    assert second.status_code == 201 and second.json()["id"] == first.json()["id"]
    changed = {**payload, "parameter_overrides": {"temperature": {"samples": 9}}}
    assert client.post("/api/testing/test-runs", json=changed).status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        assert db.scalar(select(func.count()).select_from(PlanRevision)) == 1


def test_invalid_save_and_run_rolls_back_publication(setup):
    client, factory, ids, _, _ = setup
    response = client.post("/api/testing/test-runs", json={"plan_id": ids["plan"], "draft_revision": 1,
        "idempotency_key": "bad-request", "parameter_overrides": {"temperature": {"limit": 200}}})
    assert response.status_code == 422
    with factory() as db:
        assert db.get(Plan, ids["plan"]).revision == 1
        assert db.scalar(select(func.count()).select_from(PlanRevision)) == 0
        assert db.scalar(select(func.count()).select_from(Run)) == 0


def test_case_revision_cannot_change_content(setup):
    client, factory, ids, _, _ = setup
    with factory() as db:
        original = service.view(db.get(CaseRevision, ids["case"]))
    payload = {key: original[key] for key in ("case_id", "version", "name", "mode", "definition")}
    assert client.post("/api/testing/case-revisions", json=payload).status_code == 201
    payload["definition"]["parameters_schema"]["properties"]["limit"]["default"] = 50
    assert client.post("/api/testing/case-revisions", json=payload).status_code == 409
    # Registering an arbitrary filesystem entrypoint must go through package import.
    payload["version"] = "3.0.0"
    payload["definition"]["implementation"] = {"runtime_profile": "python3-case", "entrypoint": "/tmp/arbitrary.py"}
    assert client.post("/api/testing/case-revisions", json=payload).status_code == 422


def test_capabilities_and_required_case_validation(setup):
    client, _, ids, config, _ = setup
    draft = deepcopy(config)
    draft["cases"][0]["required"] = False
    preview = client.post(f"/api/testing/test-plans/{ids['plan']}/validate", json={"draft": draft}).json()
    assert any(error["code"] == "required_case_missing" for error in preview["errors"])
    client.patch(f"/api/testing/devices/{ids['device']}", json={"capabilities": []})
    preview = client.post(f"/api/testing/test-plans/{ids['plan']}/validate", json={}).json()
    assert any(error["code"] == "missing_capabilities" for error in preview["errors"])


def test_build_only_has_no_device_or_cases(setup):
    client, _, ids, _, _ = setup
    environment = client.post("/api/testing/build-environments", json={"name": "SDK", "node": {"kind": "local"},
        "config": {"sdk_path": "/tmp/sdk", "recipe": {"argv": ["./build.sh"], "artifacts": ["images/*.img"]}}}).json()
    draft = {"product_id": ids["product"], "firmware": {"kind": "build", "environment_id": environment["id"],
        "source": {"kind": "local_workspace"}, "build_only": True}, "cases": [], "acceptance": {"mode": "not_tested"}}
    result = client.post(f"/api/testing/test-plans/{ids['plan']}/validate", json={"draft": draft}).json()
    assert result["valid"], result
    assert result["resolved"]["device"] is None
    assert result["stages"] == ["固定源码", "构建", "封存镜像"]


def test_image_product_identity_is_checked_before_run(setup):
    client, factory, ids, config, _ = setup
    with factory() as db:
        asset = InputAsset(kind="firmware_bundle", name="其他产品镜像", digest="abc", manifest={"product_id": "other"})
        db.add(asset)
        db.commit()
        asset_id = asset.id
    draft = {**config, "firmware": {"kind": "artifact", "bundle_id": asset_id}}
    result = client.post(f"/api/testing/test-plans/{ids['plan']}/validate", json={"draft": draft}).json()
    assert not result["valid"] and any(e["code"] == "wrong_product" for e in result["errors"])


def test_busy_device_queues_but_quarantined_device_blocks(setup):
    client, factory, ids, _, _ = setup
    fixed = publish(client, ids["plan"])
    with factory() as db:
        db.get(Device, ids["device"]).health = "busy"
        db.commit()
    response = client.post("/api/testing/test-runs", json={"plan_revision_id": fixed["id"], "idempotency_key": "queued"})
    assert response.status_code == 201
    with factory() as db:
        db.get(Device, ids["device"]).health = "needs_attention"
        db.commit()
    response = client.post("/api/testing/test-runs", json={"plan_revision_id": fixed["id"], "idempotency_key": "blocked"})
    assert response.status_code == 409


def test_operator_cas_idempotency_and_evidence_ownership(setup):
    client, factory, ids, _, runner = setup
    fixed = publish(client, ids["plan"])
    run = client.post("/api/testing/test-runs", json={"plan_revision_id": fixed["id"], "idempotency_key": "manual"}).json()
    with factory() as db:
        row = db.get(Run, run["id"])
        row.state = "awaiting_operator"
        row.result = {"awaiting_operator": {"step_id": "inspection:one", "required_fields": ["note"], "attachments_required": True},
                      "evidence": [{"evidence_id": "image-1"}]}
        db.commit()
    url = f"/api/testing/test-runs/{run['id']}/operator-records"
    payload = {"step_id": "inspection:one", "revision": 0, "operator": "测试员", "decision": "passed",
               "values": {"note": "外观正常"}, "attachments": [{"evidence_id": "foreign-image"}], "idempotency_key": "step-once"}
    assert client.post(url, json=payload).status_code == 422
    payload["attachments"] = [{"evidence_id": "image-1"}]
    first = client.post(url, json=payload)
    assert first.status_code == 201, first.text
    assert client.post(url, json=payload).json()["id"] == first.json()["id"]
    assert runner.resumed == [run["id"]]
    stale = {**payload, "idempotency_key": "stale-request"}
    assert client.post(url, json=stale).status_code == 409
    # Even if a client sees the newer revision before the runner advances,
    # another request cannot record the same physical step twice.
    assert client.post(url, json={**stale, "revision": 1}).status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(OperatorRecord)) == 1
        assert db.get(Run, run["id"]).operator_revision == 1


def test_manual_record_schema_is_enforced(setup):
    client, factory, ids, _, _ = setup
    fixed = publish(client, ids["plan"])
    run = client.post("/api/testing/test-runs", json={"plan_revision_id": fixed["id"], "idempotency_key": "record-schema"}).json()
    with factory() as db:
        row = db.get(Run, run["id"])
        row.state = "awaiting_operator"
        row.result = {"awaiting_operator": {"step_id": "operator", "record_schema": {"type": "object", "additionalProperties": False,
            "properties": {"voltage": {"type": "number", "minimum": 0}}, "required": ["voltage"]}}}
        db.commit()
    payload = {"step_id": "operator", "revision": 0, "operator": "测试员", "decision": "passed", "idempotency_key": "value"}
    assert client.post(f"/api/testing/test-runs/{run['id']}/operator-records", json=payload).status_code == 422
    payload["values"] = {"voltage": 5.0}
    assert client.post(f"/api/testing/test-runs/{run['id']}/operator-records", json=payload).status_code == 201


def test_repeat_keeps_fixed_inputs_but_never_previous_results(setup, monkeypatch):
    client, factory, ids, _, _ = setup
    fixed = publish(client, ids["plan"])
    original = client.post("/api/testing/test-runs", json={"plan_revision_id": fixed["id"], "idempotency_key": "original"}).json()
    with factory() as db:
        row = db.get(Run, original["id"])
        row.resolved_input = deepcopy(row.request_snapshot)
        row.result = {"cases": [{"binding_id": "temperature", "verdict": "passed"}], "device_observation": {"version": "old"}}
        row.state, row.verdict = "completed", "passed"
        db.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("A fixed-input repeat must not resolve or publish a plan again")
    monkeypatch.setattr(service, "validate_plan", forbidden)
    monkeypatch.setattr(service, "publish_plan", forbidden)
    url = f"/api/testing/test-runs/{original['id']}/repeat"
    response = client.post(url, json={"idempotency_key": "fixed-repeat"})
    assert response.status_code == 201, response.text
    repeated = response.json()
    assert repeated["id"] != original["id"]
    assert repeated["state"] == "queued" and repeated["verdict"] == "not_tested"
    assert repeated["result"] == {}
    assert repeated["request_snapshot"]["parameters"] == original["request_snapshot"]["parameters"]
    assert repeated["summary"]["repeat_of_run_id"] == original["id"]
    assert "不能保证" in repeated["summary"]["repeat_notice"]
    assert client.post(url, json={"idempotency_key": "fixed-repeat"}).json()["id"] == repeated["id"]
    assert client.post(f"/api/testing/test-runs/{repeated['id']}/repeat", json={"idempotency_key": "fixed-repeat"}).status_code == 409


def test_repeat_without_resolved_inputs_does_not_claim_exact_retest(setup):
    client, _, ids, _, _ = setup
    fixed = publish(client, ids["plan"])
    original = client.post("/api/testing/test-runs", json={"plan_revision_id": fixed["id"], "idempotency_key": "unresolved"}).json()
    response = client.post(f"/api/testing/test-runs/{original['id']}/repeat", json={"idempotency_key": "cannot-repeat"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "repeat_input_unresolved"


def test_repeat_build_reuses_bundle_and_rejects_missing_bytes(setup, tmp_path):
    client, factory, ids, _, _ = setup
    fixed = publish(client, ids["plan"])
    original = client.post("/api/testing/test-runs", json={"plan_revision_id": fixed["id"], "idempotency_key": "built-original"}).json()
    image = tmp_path / "firmware.img"
    image.write_bytes(b"original firmware")
    bundle = {"digest": "fixed-digest", "root_path": str(tmp_path), "files": [{"path": image.name, "sha256": "byte-digest"}]}
    with factory() as db:
        row = db.get(Run, original["id"])
        row.request_snapshot = {**row.request_snapshot, "plan": {**row.request_snapshot["plan"], "firmware": {
            "kind": "build", "environment_id": "must-not-resolve-live-environment", "source": {"kind": "local_workspace"}}}}
        row.resolved_input = {"source_snapshot": {"digest": "fixed-source-digest"}}
        row.result = {"firmware_bundle": bundle, "firmware_bundle_id": "bundle-id", "build": {"exit_code": 0},
                      "deployment": {"boot_verified": True}, "cases": [{"verdict": "passed"}]}
        row.state = "completed"
        db.commit()
    url = f"/api/testing/test-runs/{original['id']}/repeat"
    response = client.post(url, json={"idempotency_key": "repeat-built"})
    assert response.status_code == 201, response.text
    assert response.json()["result"] == {"firmware_bundle": bundle, "firmware_bundle_id": "bundle-id", "build": {"exit_code": 0}}
    assert response.json()["resolved_input"]["source_snapshot"]["digest"] == "fixed-source-digest"
    image.unlink()
    response = client.post(url, json={"idempotency_key": "missing-bytes"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "repeat_asset_missing"


def test_repeat_failed_build_cannot_rebuild_implicitly(setup):
    client, factory, ids, _, _ = setup
    fixed = publish(client, ids["plan"])
    original = client.post("/api/testing/test-runs", json={"plan_revision_id": fixed["id"], "idempotency_key": "failed-build"}).json()
    with factory() as db:
        row = db.get(Run, original["id"])
        row.request_snapshot = {**row.request_snapshot, "plan": {**row.request_snapshot["plan"], "firmware": {"kind": "build"}}}
        row.resolved_input = {"source_snapshot": {"digest": "source"}}
        row.state, row.verdict = "completed", "error"
        db.commit()
    response = client.post(f"/api/testing/test-runs/{original['id']}/repeat", json={"idempotency_key": "do-not-build"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "repeat_firmware_missing"
