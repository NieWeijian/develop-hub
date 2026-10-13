from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from develop_hub.main import app


def git_init(path: Path, filename: str = "README.md") -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "workbench@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Workbench Test"], cwd=path, check=True)
    (path / filename).write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=path, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_sdk(path: Path) -> tuple[Path, Path, str]:
    manifests = path / ".repo" / "manifests"
    git_init(manifests, "default.xml")
    app_repo = path / "app" / "fixture"
    app_commit = git_init(app_repo)
    remote = path.parent / "apps" / "fixture.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "rk", str(remote)], cwd=app_repo, check=True)
    subprocess.run(
        ["git", "push", "rk", "HEAD:refs/changes/32/18432/7"],
        cwd=app_repo,
        check=True,
        capture_output=True,
    )
    (manifests / "default.xml").write_text(
        '<manifest><project name="apps/fixture" path="app/fixture" /></manifest>\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "default.xml"], cwd=manifests, check=True)
    subprocess.run(["git", "commit", "-m", "add app project"], cwd=manifests, check=True, capture_output=True)
    (path / ".repo" / "manifest.xml").symlink_to("manifests/default.xml")
    artifacts = path / "output" / "image"
    artifacts.mkdir(parents=True)
    board_configs = path / "project" / "cfg"
    board_configs.mkdir(parents=True)
    (board_configs / "BoardConfig-fixture_release.mk").write_text("# fixture\n", encoding="utf-8")
    defconfigs = path / "device" / "rockchip" / ".chips" / "rk3572"
    defconfigs.mkdir(parents=True)
    (defconfigs / "rockchip_rk3572_struct_light_defconfig").write_text("# fixture\n", encoding="utf-8")
    build = path / "build.sh"
    build.write_text(
        '#!/bin/sh\nset -eu\n# RK_TARGET_BOARD_ARRAY\nif [ "${1:-}" = lunch ]; then read index; test "$index" = 0; ln -sf project/cfg/BoardConfig-fixture_release.mk .BoardConfig.mk; exit 0; fi\necho firmware-build-start\nsleep 1\nprintf firmware > output/image/update.img\n',
        encoding='utf-8',
    )
    os.chmod(build, 0o755)
    return app_repo, artifacts, app_commit


def test_product_driven_round_snapshots_without_coding_run(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sdk_root = tmp_path / "fixture-sdk"
    app_repo, artifacts, app_commit = make_sdk(sdk_root)
    (app_repo/'AGENTS.md').write_text('local guidance')
    (app_repo/'docs').mkdir()
    (app_repo/'docs'/'note.md').write_text('local notes')
    manifest_file = sdk_root/'.repo/manifests/default.xml'
    manifest_file.write_text(manifest_file.read_text() + '\n')

    with TestClient(app) as client:
        from develop_hub.db import SessionLocal
        from develop_hub.models import SdkRepository
        assert client.post('/api/qa/sdk-repositories/discover', json={'root_path': str(sdk_root)}).status_code == 403
        assert client.post('/api/qa/sdk-repositories', json={'root_path': str(sdk_root)}).status_code == 403
        # Administrative registration is out of the operator API.
        with SessionLocal() as db:
            registered = SdkRepository(name='fixture-sdk', root_path=str(sdk_root), board_configs=['BoardConfig-fixture_release.mk'], artifact_root=str(artifacts), artifact_patterns=['*.img'])
            db.add(registered)
            db.commit()
            sdk_id = registered.id
        repository = client.post(
            "/api/repositories",
            json={"name": "workbench-app-repo", "root_path": str(app_repo), "default_branch": "main"},
        )
        assert repository.status_code == 201, repository.text
        manifest = client.get(f"/api/qa/sdk-repositories/{sdk_id}/manifest")
        assert manifest.status_code == 200
        assert manifest.json()["manifest_file"] == ".repo/manifests/default.xml"
        assert manifest.json()["project_count"] == 1
        assert ".repo/manifests" in manifest.json()["local_changes"]

        invalid_board = client.post(
            "/api/qa/products",
            json={
                "code": "fixture-invalid",
                "name": "Invalid board",
                "sdk_repository_id": sdk_id,
                "tested_repository_id": repository.json()["id"],
                "gerrit_project": "apps/fixture",
                "board_config": "operator_supplied_command",
            },
        )
        assert invalid_board.status_code == 422

        product = client.post(
            "/api/qa/products",
            json={
                "code": " fixture-product ",
                "name": " Fixture Product ",
                "sdk_repository_id": sdk_id,
                "tested_repository_id": repository.json()["id"],
                "gerrit_project": " apps/fixture ",
                "board_config": " BoardConfig-fixture_release.mk ",
                "kernel_baseline": "6.1",
                "uboot_baseline": "2017.09",
                "rkbin_baseline": "fixture",
            },
        )
        assert product.status_code == 201, product.text
        assert product.json()["code"] == "fixture-product"
        assert product.json()["gerrit_project"] == "apps/fixture"
        verified = client.post(f"/api/qa/products/{product.json()['id']}/verify-baseline")
        assert verified.status_code == 202, client.get(f"/api/qa/products/{product.json()['id']}").text
        assert client.post(f"/api/qa/products/{product.json()['id']}/verify-baseline").status_code == 409
        saw_live_output = False
        for _ in range(200):
            verified = client.get(f"/api/qa/products/{product.json()['id']}")
            if verified.json()['status'] == 'verifying' and 'firmware-build-start' in verified.json()['baseline_result'].get('progress', {}).get('logs', ''):
                saw_live_output = True
            if verified.json()['status'] != 'verifying':
                break
            time.sleep(0.02)
        assert saw_live_output, "Build output must be visible through the API before completion"
        assert verified.json()["status"] == "ready"
        from develop_hub.test_workbench import sdk_lease
        with sdk_lease(app.state.test_round_workflow.settings, str(sdk_root)):
            rejected=client.post(f"/api/qa/products/{product.json()['id']}/verify-baseline")
            assert rejected.status_code==409
            preserved=client.get(f"/api/qa/products/{product.json()['id']}").json()
            assert preserved['status']=='ready'
            assert preserved['baseline_result']['passed'] is True

        assert verified.json()['repository_snapshot']['tracked_clean'] is True
        assert verified.json()['repository_snapshot']['git_status'] == 'untracked_only'
        assert 'AGENTS.md' in verified.json()['repository_snapshot']['untracked_files']
        assert verified.json()["baseline_result"]["configuration"]["command"] == "env LC_ALL=C ./build.sh lunch"
        assert verified.json()["baseline_result"]["build"]["exit_code"] == 0

        point = client.post(
            f"/api/qa/products/{product.json()['id']}/test-points",
            json={
                "change_number": 18432,
                "patchset_number": 7,
                "commit_sha": app_commit,
                "ref": "refs/changes/32/18432/7",
                "subject": "fixture patchset",
                "branch": "main",
                "owner": "fixture",
                "submitted_at": "2026-09-11T00:00:00Z",
                "ready": True,
            },
        )
        assert point.status_code == 201
        repeated=client.post(f"/api/qa/products/{product.json()['id']}/test-points",json={
            'change_number':18432,'patchset_number':7,'commit_sha':app_commit,
            'ref':'refs/changes/32/18432/7','subject':'same point','branch':'main','ready':True})
        assert repeated.status_code == 201
        assert repeated.json()['id'] == point.json()['id']
        bench = client.post(
            "/api/qa/benches",
            json={
                "name": "fixture-bench",
                "host": "127.0.0.1",
                "port": 22,
                "user": "fixture",
                "remote_root": "/srv/develop-hub-test",
            },
        )
        assert bench.status_code == 201
        # A stale/offline execution package is rejected before creating a round or starting a build.
        from develop_hub import test_workbench_api
        from develop_hub.bench_readiness import BOARD
        from develop_hub.models import TestProduct
        client.post('/api/qa/basic-case-definitions/install')
        version=next(c for c in client.get('/api/qa/test-cases').json() if c['code']=='BASIC-VERSION')
        with SessionLocal() as db:
            db.get(TestProduct,product.json()['id']).board_config=BOARD
            db.commit()
        async def unready(*args):
            return {'passed':False,'checks':[{'name':'Controller','passed':False,'detail':'inactive'}]}
        with monkeypatch.context() as patch:
            patch.setattr(test_workbench_api,'inspect_bench',unready)
            rejected=client.post('/api/qa/test-rounds',json={
                'product_id':product.json()['id'],'test_point_id':point.json()['id'],
                'bench_id':bench.json()['id'],'test_case_ids':[version['id']],
                'flash_firmware':True,'execute_test_cases':True,'device_serial':'fixture','auto_start':True})
            assert rejected.status_code==422
            assert 'inactive' in rejected.json()['detail']
            assert client.get('/api/qa/test-rounds').json()==[]
        with SessionLocal() as db:
            db.get(TestProduct,product.json()['id']).board_config='BoardConfig-fixture_release.mk'
            db.commit()
        case = client.post(
            "/api/qa/test-cases",
            json={
                "code": "QA-WORKBENCH-FIXTURE",
                "category": "special",
                "name": "Workbench fixture",
                "description": "Snapshotted but not executed in this implementation phase.",
                "command": "true",
                "expected_exit_code": 0,
                "timeout_seconds": 30,
                "risk_level": "read_only",
                "enabled": True,
            },
        )
        assert case.status_code == 201
        test_round = client.post(
            "/api/qa/test-rounds",
            json={
                "product_id": product.json()["id"],
                "test_point_id": point.json()["id"],
                "bench_id": bench.json()["id"],
                "test_case_ids": [case.json()["id"]],
                "test_case_parameters": {case.json()["id"]: {"frames": 300}},
                "auto_start": False,
            },
        )
        assert test_round.status_code == 201, test_round.text
        body = test_round.json()
        assert body["status"] == "created"
        assert client.get(f"/api/qa/test-rounds/{body['id']}/report").json()['verdict']=='IN_PROGRESS'
        assert "coding_run_id" not in body
        assert body["gerrit_project"] == "apps/fixture"
        assert body["product_snapshot"]["tested_repository_id"] == repository.json()["id"]
        assert body["manifest_commit"] == verified.json()["manifest_snapshot"]["manifest_commit"]
        assert body["board_config"] == "BoardConfig-fixture_release.mk"
        assert body["test_case_snapshot"][0]["parameters"] == {"frames": 300}
        assert body["events"][0]["event_type"] == "created"
        assert client.patch(f"/api/qa/test-cases/{case.json()['id']}/category", json={"category":"invalid"}).status_code == 422
        assert client.patch(f"/api/qa/test-cases/{case.json()['id']}/category", json={"category":"basic"}).status_code == 200
        assert body["test_case_snapshot"][0]["category"] == "special"

        # Editing a registered node after creation cannot redirect this round.
        from develop_hub.models import TestBench
        with SessionLocal() as db:
            db.get(TestBench, body['bench_id']).host = 'changed.example.invalid'
            db.commit()
        workflow = app.state.test_round_workflow

        async def fake_transfer(_round, _bench, config_path, images):  # type: ignore[no-untyped-def]
            assert _bench.host == "127.0.0.1"
            assert config_path.name == "test-run.json"
            assert [item["filename"] for item in images] == ["update.img"]
            return "/srv/develop-hub-test/jobs/TEST-000001"

        async def fake_verify(_bench, remote_path, images, config_path):  # type: ignore[no-untyped-def]
            assert remote_path.endswith("TEST-000001")
            assert len(images[0]["sha256"]) == 64
            return {"passed": True, "files": [], "verified_at": "2026-09-11T00:00:00Z"}

        monkeypatch.setattr(workflow, "_transfer", fake_transfer)
        monkeypatch.setattr(workflow, "_verify_remote", fake_verify)
        started = client.post(f"/api/qa/test-rounds/{body['id']}/start")
        assert started.status_code == 200
        final = started.json()
        for _ in range(100):
            final = client.get(f"/api/qa/test-rounds/{body['id']}").json()
            if final["status"] in {"remote_ready", "build_failed", "blocked"}:
                break
            time.sleep(0.02)
        assert final["status"] == "remote_ready", final.get("last_error")
        assert all(event["status"] != "running" for event in final["events"])
        assert final["test_case_snapshot"][0]["category"] == "special"
        assert final["generated_config"]["test_cases"][0]["category"] == "special"
        assert final["generated_config"]["source"]["owner"] == "fixture"
        assert final["generated_config"]["source"]["submitted_at"].startswith("2026-09-11")
        assert [image["filename"] for image in final["image_snapshot"]] == ["update.img"]
        assert final["build_result"]["command_passed"] is True
        assert final["build_result"]["artifact_validation_passed"] is True
        assert final["generated_config"]["execution_boundary"] == "remote_file_verification"
        assert final["generated_config"]["source"]["project"] == "apps/fixture"
        assert final["remote_path"].endswith("TEST-000001")
        assert not any("test case" in event["event_type"] for event in final["events"])

        assert final['bench_snapshot']['host'] == '127.0.0.1'
        assert final['build_result']['configuration']['exit_code'] == 0
        assert final['delivery_result']['passed'] is True
        from develop_hub.test_workbench import round_progress
        before=client.get(f"/api/qa/test-rounds/{body['id']}").json()['build_result']
        round_progress(body['id'],workflow.settings.artifact_root,'CONTROLLER_OUTPUT_MUST_NOT_CHANGE_BUILD','ssh result.json')
        assert client.get(f"/api/qa/test-rounds/{body['id']}").json()['build_result']==before
        assert 'CONTROLLER_OUTPUT_MUST_NOT_CHANGE_BUILD' not in (workflow.settings.artifact_root/'test-rounds'/body['id']/'build.log').read_text()

        report=client.get(f"/api/qa/test-rounds/{body['id']}/report")
        assert report.status_code==200
        assert report.json()['verdict']=='NOT_TESTED'
        assert report.json()['case_results']==[]
        assert 'TEST-000001.json' in report.headers['content-disposition']
        assert client.post(f"/api/qa/test-rounds/{body['id']}/start").status_code == 409
        assert client.get(f"/api/qa/test-rounds/{body['id']}").json()['status'] == 'remote_ready'
        invalid = client.post(f"/api/qa/products/{product.json()['id']}/test-points", json={
            'change_number':18432, 'patchset_number':7, 'commit_sha':'f'*40,
            'ref':'refs/changes/32/18432/7', 'subject':'wrong commit', 'branch':'main',
        })
        assert invalid.status_code == 422


def test_sdk_lease_rejects_overlapping_work(tmp_path):
    import pytest
    from dataclasses import replace
    from develop_hub.config import settings
    from develop_hub.test_workbench import sdk_lease, TestWorkbenchError
    config = replace(settings, artifact_root=tmp_path/'artifacts')
    with sdk_lease(config, str(tmp_path/'sdk')):
        with pytest.raises(TestWorkbenchError, match='busy'):
            with sdk_lease(config, str(tmp_path/'sdk')):
                pass
    with sdk_lease(config, str(tmp_path/'sdk')):
        pass


def test_artifacts_require_nonempty_and_new_output(tmp_path):
    import pytest
    from develop_hub.models import SdkRepository
    from develop_hub.test_workbench import TestRoundWorkflow, TestWorkbenchError
    image_root = tmp_path/'output'
    image_root.mkdir()
    sdk = SdkRepository(root_path=str(tmp_path), artifact_root=str(image_root), artifact_patterns=['*.img'])
    with pytest.raises(TestWorkbenchError, match='no configured'):
        TestRoundWorkflow._collect_artifacts(sdk, {})
    image = image_root/'oem.img'
    image.touch()
    with pytest.raises(TestWorkbenchError, match='Empty'):
        TestRoundWorkflow._collect_artifacts(sdk, {})
    image.write_bytes(b'firmware')
    versions = TestRoundWorkflow._artifact_versions(sdk)
    with pytest.raises(TestWorkbenchError, match='not refreshed'):
        TestRoundWorkflow._collect_artifacts(sdk, versions)


def test_remote_verification_checks_json_digest_and_fails_closed(tmp_path, monkeypatch):
    import asyncio
    import pytest
    from develop_hub.config import settings
    from develop_hub.models import TestBench
    from develop_hub.test_workbench import TestRoundWorkflow, TestWorkbenchError, sha256_file
    config = tmp_path/'test-run.json'
    config.write_text('{"round_id":"TEST-000001"}')
    workflow = TestRoundWorkflow(settings)
    bench = TestBench(name='node',host='127.0.0.1',port=22,user='test',remote_root='/tmp/jobs')
    async def remote(_bench, command, timeout=120):
        assert sha256_file(config) in command
        assert '/test-run.json' in command
        return {'exit_code':1,'stderr_tail':'digest mismatch'}
    monkeypatch.setattr(workflow,'_remote',remote)
    with pytest.raises(TestWorkbenchError, match='digest mismatch'):
        asyncio.run(workflow._verify_remote(bench,'/tmp/jobs/TEST-000001',[],config))


def test_baseline_rejects_nonzero_build_without_agent_inference(tmp_path, monkeypatch):
    import asyncio
    import pytest
    from develop_hub.config import settings
    from develop_hub.db import SessionLocal
    from develop_hub.models import Repository, SdkRepository, TestProduct, GerritRepository
    from develop_hub.test_workbench import verify_product_baseline, TestWorkbenchError
    sdk_root = tmp_path/'failed-sdk'
    app_repo, artifacts, _ = make_sdk(sdk_root)
    (sdk_root/'build.sh').write_text('#!/bin/sh\n# RK_TARGET_BOARD_ARRAY\nif [ "$1" = lunch ]; then read index; ln -sf project/cfg/BoardConfig-fixture_release.mk .BoardConfig.mk; exit 0; fi\necho "Agent says build passed"\nexit 9\n')
    with TestClient(app):
        with SessionLocal() as db:
            sdk = SdkRepository(name='failure-sdk',root_path=str(sdk_root),board_configs=['BoardConfig-fixture_release.mk'],artifact_root=str(artifacts),artifact_patterns=['*.img'])
            repo = Repository(name='failure-app',root_path=str(app_repo))
            gerrit = GerritRepository(name='failure-project',project_path='failure/fixture')
            db.add_all([sdk,repo,gerrit]);db.flush()
            product = TestProduct(code='failure-product',name='Failure',sdk_repository_id=sdk.id,tested_repository_id=repo.id,gerrit_repository_id=gerrit.id,gerrit_project='apps/fixture',board_config='BoardConfig-fixture_release.mk')
            db.add(product);db.commit();product_id=product.id
        with pytest.raises(TestWorkbenchError):
            asyncio.run(verify_product_baseline(product_id,settings))
        with SessionLocal() as db:
            result=db.get(TestProduct,product_id).baseline_result
            assert result['passed'] is False
            assert result['build']['exit_code']==9


def test_remote_verification_accepts_exact_json_and_images(tmp_path, monkeypatch):
    import asyncio
    import shutil
    from develop_hub.config import settings
    from develop_hub.models import TestBench
    from develop_hub.test_workbench import TestRoundWorkflow, run_command, sha256_file
    local = tmp_path/'test-run.json'
    local.write_text('{"schema_version":"1.0"}')
    remote = tmp_path/'remote'
    (remote/'firmware').mkdir(parents=True)
    shutil.copy2(local, remote/'test-run.json')
    image = remote/'firmware'/'oem.img'
    image.write_bytes(b'firmware-image')
    workflow = TestRoundWorkflow(settings)
    bench = TestBench(name='fixture')
    async def execute_local(_bench, command, timeout=120):
        return await run_command(['bash','-c',command],tmp_path,timeout)
    monkeypatch.setattr(workflow,'_remote',execute_local)
    result = asyncio.run(workflow._verify_remote(bench,str(remote),[{'filename':'oem.img','sha256':sha256_file(image),'size_bytes':image.stat().st_size}],local))
    assert result['passed'] is True
    assert [item['filename'] for item in result['files']] == ['test-run.json','firmware/oem.img']


def test_defconfig_menu_maps_registered_name_to_actual_index(tmp_path):
    import asyncio
    from develop_hub.models import SdkRepository
    from develop_hub.test_workbench import board_config_choices, configure_board
    sdk_root=tmp_path/'sdk'
    chip=sdk_root/'device/rockchip/.chips/rk3572'
    chip.mkdir(parents=True)
    (chip.parent.parent/'.chip').symlink_to('.chips/rk3572')
    hook=sdk_root/'device/rockchip/common/build-hooks/00-config.sh'
    hook.parent.mkdir(parents=True)
    hook.write_text('# menu adapter fixture')
    for name in ['rockchip_defconfig','rockchip_other_defconfig','rockchip_target_defconfig']:
        (chip/name).write_text('# config')
    (sdk_root/'output').mkdir()
    build=sdk_root/'build.sh'
    build.write_text('#!/bin/sh\nset -eu\ntest "$#" = 1\ntest "$1" = lunch\nread index\ntest "$index" = 3\nln -sf ../device/rockchip/.chip/rockchip_target_defconfig output/defconfig\n')
    build.chmod(0o755)
    sdk=SdkRepository(root_path=str(sdk_root),board_configs=['rockchip_target_defconfig'])
    choices=asyncio.run(board_config_choices(sdk))
    assert choices[0]['menu_index']==3
    result=asyncio.run(configure_board(sdk,'rockchip_target_defconfig',20))
    assert result['exit_code']==0
    assert result['selection_verified'] is True
    build.write_text('#!/bin/sh\nrm -f output/defconfig\nln -s ../device/rockchip/.chip/rockchip_other_defconfig output/defconfig\n')
    result=asyncio.run(configure_board(sdk,'rockchip_target_defconfig',20))
    assert result['exit_code']!=0


def test_repo_clean_gate_distinguishes_untracked_and_staged(tmp_path):
    import asyncio
    from develop_hub.models import Repository, SdkRepository
    from develop_hub.test_workbench import inspect_repository
    root=tmp_path/'sdk'
    app_repo,_,_=make_sdk(root)
    repo=Repository(root_path=str(app_repo))
    sdk=SdkRepository(root_path=str(root))
    (app_repo/'AGENTS.md').write_text('local guidance')
    snapshot=asyncio.run(inspect_repository(repo,sdk))
    assert snapshot['tracked_clean'] is True
    assert snapshot['untracked_files']==['AGENTS.md']
    (app_repo/'README.md').write_text('modified source')
    assert asyncio.run(inspect_repository(repo,sdk))['tracked_clean'] is False
    subprocess.run(['git','add','README.md'],cwd=app_repo,check=True)
    assert asyncio.run(inspect_repository(repo,sdk))['tracked_clean'] is False


def test_command_reports_output_before_completion(tmp_path):
    import asyncio
    from develop_hub.test_workbench import run_command, _command_progress
    async def check():
        reports=[]
        token=_command_progress.set(lambda text, command: reports.append(text))
        try:
            task=asyncio.create_task(run_command(['bash','-c','echo first; sleep 1.5; echo last'],tmp_path,10))
            await asyncio.sleep(0.8)
            assert not task.done()
            assert 'first' in ''.join(reports)
            result=await task
            assert result['exit_code']==0
            assert 'last' in ''.join(reports)
        finally:
            _command_progress.reset(token)
    asyncio.run(check())
