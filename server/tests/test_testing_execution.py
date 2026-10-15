from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from develop_hub.config import settings
from develop_hub.db import Base, get_db
from develop_hub.testing import runner, resources_api
from develop_hub.testing.api import router
from develop_hub.testing.execution import validate_case_result


@pytest.fixture
def application(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'test.db'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t.name.startswith('testing_')])
    factory = sessionmaker(engine, expire_on_commit=False)
    config = replace(settings, artifact_root=tmp_path / 'artifacts')
    monkeypatch.setattr(runner, 'SessionLocal', factory)
    monkeypatch.setattr(resources_api, 'settings', config)

    @asynccontextmanager
    async def lifespan(app):
        app.state.testing_runner = runner.TestingRunner(config)
        await app.state.testing_runner.recover()
        yield
        await app.state.testing_runner.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.include_router(resources_api.router)

    def sessions():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = sessions
    return app


def post(client, path, data=None):
    response = client.post('/api/testing' + path, json=data)
    assert response.status_code < 300, response.text
    return response.json()


def wait(client, identity, state):
    for _ in range(100):
        run = client.get('/api/testing/test-runs/' + identity).json()
        if run['state'] == state:
            return run
        time.sleep(.05)
    pytest.fail(str(run))


def demo_run(client, key='first'):
    post(client, '/demo')
    plan = client.get('/api/testing/test-plans').json()[0]
    payload = {'plan_id': plan['id'], 'draft_revision': plan['revision'], 'idempotency_key': key}
    run = post(client, '/test-runs', payload)
    assert post(client, '/test-runs', payload)['id'] == run['id']
    return run


def finish_manual(client, current):
    attached = client.post('/api/testing/test-runs/' + current['id'] + '/evidence?name=检查说明.txt', content='已检查'.encode()).json()
    payload = {'step_id': current['result']['awaiting_operator']['step_id'], 'revision': current['operator_revision'], 'operator': '测试人员', 'decision': 'passed', 'values': {'说明': '线缆连接可靠'}, 'attachments': [{'evidence_id': attached['evidence_id']}], 'idempotency_key': 'operator'}
    post(client, '/test-runs/' + current['id'] + '/operator-records', payload)
    post(client, '/test-runs/' + current['id'] + '/operator-records', payload)
    completed = wait(client, current['id'], 'completed')
    assert completed['verdict'] == 'passed', completed
    assert completed['summary']['simulated'] is True
    assert len(completed['operator_records']) == 1
    assert len(completed['result']['cases']) == 2
    download = client.get('/api/testing/test-runs/' + current['id'] + '/evidence/' + attached['evidence_id'])
    assert download.content.decode() == '已检查'
    exported = client.get('/api/testing/test-runs/' + current['id'] + '/report.html')
    assert exported.status_code == 200
    assert 'attachment;' in exported.headers['content-disposition']
    assert '线缆连接可靠' in exported.text and '75' in exported.text
    assert '模拟运行' in exported.text and '北京时间' in exported.text
    return completed


def test_complete_manual_flow_immutable_input_and_evidence(application):
    with TestClient(application) as client:
        current = wait(client, demo_run(client)['id'], 'awaiting_operator')
        plan = client.get('/api/testing/test-plans').json()[0]
        draft = plan['draft']
        draft['cases'][0]['parameter_overrides'] = {'limit_c': 1}
        changed = client.patch('/api/testing/test-plans/' + plan['id'] + '/draft', json={'revision': plan['revision'], 'draft': draft})
        assert changed.status_code == 200
        complete = finish_manual(client, current)
        assert complete['request_snapshot']['cases'][0]['parameters']['limit_c'] == 75


def test_device_check_refuses_busy_node_and_requires_recovery_record(application, monkeypatch):
    from develop_hub.testing.models import Device
    with TestClient(application) as client:
        current = wait(client, demo_run(client)['id'], 'awaiting_operator')
        device_id = current['request_snapshot']['device']['id']
        endpoint = '/api/testing/devices/' + device_id + '/check'
        assert client.post(endpoint, json={'operator': 'tester'}).status_code == 409
        finish_manual(client, current)
        with runner.SessionLocal() as db:
            db.get(Device, device_id).health = 'needs_attention'
            db.commit()
        assert client.post(endpoint, json={'operator': 'tester'}).status_code == 422
        checked = client.post(endpoint, json={'operator': 'tester', 'note': '已检查节点无残留任务', 'recovery_confirmed': True})
        assert checked.status_code == 200, checked.text
        assert checked.json()['health'] == 'ready'
        assert checked.json()['config']['last_check']['operator'] == 'tester'


def test_corrupt_package_upload_is_validation_error(application):
    with TestClient(application) as client:
        response = client.post('/api/testing/case-imports', content=b'not a zip')
        assert response.status_code == 422


def test_waiting_manual_step_survives_service_restart(application):
    with TestClient(application) as client:
        identity = demo_run(client)['id']
        wait(client, identity, 'awaiting_operator')
    with TestClient(application) as client:
        current = wait(client, identity, 'awaiting_operator')
        finish_manual(client, current)


def test_cancelling_queued_run_does_not_release_another_device_lease(application):
    with TestClient(application) as client:
        first = wait(client, demo_run(client)['id'], 'awaiting_operator')
        plan = client.get('/api/testing/test-plans').json()[0]
        second = post(client, '/test-runs', {'plan_id': plan['id'], 'draft_revision': plan['revision'], 'idempotency_key': 'second'})
        post(client, '/test-runs/' + second['id'] + '/cancel')
        cancelled = wait(client, second['id'], 'cancelled')
        assert cancelled['verdict'] != 'passed'
        device = client.get('/api/testing/devices').json()[0]
        assert device['health'] == 'busy'
        finish_manual(client, first)


def test_isolated_build_only_freezes_dirty_source(application, tmp_path):
    source = tmp_path / 'sdk'
    source.mkdir()
    subprocess.run(['git', 'init', str(source)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(source), 'config', 'user.email', 'test@example.invalid'], check=True)
    subprocess.run(['git', '-C', str(source), 'config', 'user.name', 'Test'], check=True)
    (source / 'source.txt').write_text('original')
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), 'commit', '-m', 'base'], check=True, capture_output=True)
    (source / 'source.txt').write_text('local modification')
    with TestClient(application) as client:
        product = post(client, '/products', {'name': '构建测试产品'})
        environment = post(client, '/build-environments', {'name': '临时 SDK', 'node': {'kind': 'local'}, 'config': {'sdk_path': str(source), 'recipe': {'argv': [sys.executable, '-c', "from pathlib import Path; Path('out.img').write_text(Path('source.txt').read_text())"], 'artifacts': ['out.img'], 'timeout_seconds': 10}}})
        plan = post(client, '/test-plans', {'name': '独立构建', 'draft': {'product_id': product['id'], 'firmware': {'kind': 'build', 'environment_id': environment['id'], 'source': {'kind': 'local_workspace'}, 'build_only': True}, 'cases': []}})
        result = post(client, '/test-runs', {'plan_id': plan['id'], 'draft_revision': plan['revision'], 'idempotency_key': 'build'})
        complete = wait(client, result['id'], 'completed')
        assert complete['verdict'] == 'not_tested', complete
        manifest = complete['result']['firmware_bundle']
        assert (Path(manifest['root_path']) / 'out.img').read_text() == 'local modification'
        assert not (source / 'out.img').exists()


@pytest.mark.parametrize('crash', [False, True])
def test_import_publish_and_execute_external_code_package(application, tmp_path, crash):
    from test_testing_packages import manifest
    value = manifest()
    code = '''import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output-dir');a=p.parse_args()
i=json.loads(Path(a.input).read_text());out=Path(a.output_dir)
data={'result_schema':'rk-case-result/1','run_id':i['run_id'],'binding_id':i['binding_id'],'case_revision_id':i['case_revision_id'],'state':'completed','verdict':'passed','assertions':[{'id':'file-size','name':'文件检查','expected':i['parameters']['minimum_bytes'],'actual':100,'passed':True}],'recovery':{'state':'not_required'},'attachments':[]}
(out/'result.json').write_text(json.dumps(data))
'''
    if crash:
        code = "print('intentional fixture error', flush=True)\nraise RuntimeError('expected failure')\n"
    memory = BytesIO()
    with zipfile.ZipFile(memory, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(value))
        archive.writestr('run.py', code)
    inspector = tmp_path / 'inspect.py'
    inspector.write_text('print(\'{"serial":"HOST-1","firmware_version":"host fixture"}\')')
    with TestClient(application) as client:
        imported = client.post('/api/testing/case-imports', content=memory.getvalue())
        assert imported.status_code == 201, imported.text
        published = post(client, '/case-imports/' + imported.json()['id'] + '/publish', {'trusted': True})
        product = post(client, '/products', {'name': '主机检查样例'})
        device = post(client, '/devices', {'name': '隔离主机执行器', 'product_id': product['id'], 'node': {'kind': 'local'}, 'config': {'serial': 'HOST-1', 'adapter': {'inspect_argv': [sys.executable, str(inspector)]}}})
        plan = post(client, '/test-plans', {'name': '包扩展验证', 'draft': {'product_id': product['id'], 'firmware': {'kind': 'current_device'}, 'device_id': device['id'], 'cases': [{'binding_id': 'host', 'case_revision_id': published['cases'][0]['id'], 'required': True, 'parameter_overrides': {}}]}})
        run = post(client, '/test-runs', {'plan_id': plan['id'], 'draft_revision': plan['revision'], 'idempotency_key': 'external'})
        completed = wait(client, run['id'], 'completed')
        assert completed['verdict'] == ('error' if crash else 'passed'), completed
        if not crash:
            assert completed['result']['cases'][0]['assertions'][0]['actual'] == 100
        files = completed['result']['evidence']
        assert files
        if crash:
            log = next(item for item in files if item['name'].endswith('stderr.log') and 'case-' in item['name'])
            response = client.get('/api/testing/test-runs/' + run['id'] + '/evidence/' + log['evidence_id'])
            assert b'expected failure' in response.content


def test_missing_assertion_never_becomes_passed():
    case = {'binding_id': 'a', 'case_revision_id': 'v1', 'definition': {'required_assertions': ['must-exist']}}
    result = {'exit_code': 0, 'data': {'result_schema': 'rk-case-result/1', 'run_id': 'r', 'binding_id': 'a', 'case_revision_id': 'v1', 'state': 'completed', 'verdict': 'passed', 'assertions': [], 'recovery': {'state': 'not_required'}}}
    with pytest.raises(ValueError):
        validate_case_result(result, case, 'r')


def test_export_escapes_user_supplied_text(application):
    with TestClient(application) as client:
        current = wait(client, demo_run(client)['id'], 'awaiting_operator')
        assert client.get('/api/testing/test-runs/' + current['id'] + '/report.html').status_code == 409
        payload = {'step_id': current['result']['awaiting_operator']['step_id'], 'revision': 0, 'operator': '<script>bad()</script>', 'decision': 'passed', 'values': {'说明': '<img src=x onerror=bad()>'}, 'idempotency_key': 'escape'}
        post(client, '/test-runs/' + current['id'] + '/operator-records', payload)
        wait(client, current['id'], 'completed')
        content = client.get('/api/testing/test-runs/' + current['id'] + '/report.html').text
        assert '<script>' not in content and '<img' not in content
        assert '&lt;script&gt;' in content and '&lt;img' in content


def test_firmware_import_records_board_but_does_not_gate_deployment(application, tmp_path):
    from develop_hub.testing.models import InputAsset
    with TestClient(application) as client:
        product = post(client, '/products', {'name': 'RMSL321', 'chip': 'RV1126B'})
        device = post(client, '/devices', {'name': 'module', 'product_id': product['id'], 'node': {'kind': 'local'},
            'config': {'managed_station_id': 'lab', 'board_config': 'BoardConfig-test.mk', 'serial': 'abcd1234',
                'adapter': {'inspect_argv': ['not-executed'], 'deploy_argv': ['not-executed']}}})
        image = tmp_path / 'boot.img'
        image.write_bytes(b'image fixture')
        asset = post(client, '/firmware-imports', {'name': 'image', 'product_id': product['id'], 'paths': [str(image)]})
        assert asset['manifest']['board_config'] == 'BoardConfig-test.mk'
        assert asset['manifest']['board_source'] == 'declared_product_target'
        draft = {'product_id': product['id'], 'device_id': device['id'], 'firmware': {
            'kind': 'artifact', 'bundle_id': asset['id'], 'expected_version': 'factory-V2.0.7'}, 'cases': []}
        plan = post(client, '/test-plans', {'name': 'preflight', 'draft': draft})
        endpoint = '/test-plans/' + plan['id'] + '/validate'
        assert not any(e['code'] == 'deployment_metadata' for e in post(client, endpoint, {})['errors'])
        for board in ['', 'WrongBoard.mk']:
            with runner.SessionLocal() as db:
                row = db.get(InputAsset, asset['id'])
                row.manifest = {**row.manifest, 'board_config': board}
                db.commit()
            errors = post(client, endpoint, {})['errors']
            assert not any(e['code'] == 'deployment_metadata' for e in errors)


def test_deploy_exit_error_is_preserved_without_result_file(tmp_path):
    import asyncio
    from develop_hub.testing.execution import execute_step
    folder = tmp_path / 'run' / 'deploy'
    job = {'action': 'deploy', 'input': {'run_id': 'run'}, 'argv': [sys.executable, '-c',
        "import sys; print('Board and firmware must explicitly identify the supported RMSL321 RV1126B board', file=sys.stderr); sys.exit(2)"]}
    with pytest.raises(RuntimeError, match='镜像目标板型缺失'):
        asyncio.run(execute_step({'kind': 'local'}, folder, job))
    saved = json.loads((folder / 'node-result.json').read_text())
    assert saved['exit_code'] == 2 and saved['state'] == 'error'
    assert saved['evidence'] and (folder / 'output/stderr.log').read_text().startswith('Board and firmware')
