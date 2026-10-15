"""Deployment boundaries, database safety, and failed-update recovery."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / 'deploy/isolated-development'
sys.path.insert(0, str(DEPLOY))
import hub
import stable_release as stable


@pytest.mark.parametrize('path', ['/home/nwj/develop-hub-redesign', '/home/nwj/develop-hub-redesign/var', '/home/nwj/develop-hub/.venv', '/'])
def test_development_rejects_stable_locations(path):
    with pytest.raises(ValueError):
        hub.validate_root(path)


@pytest.mark.parametrize('port', [3000, 3015, 8175, 8185])
def test_development_never_uses_stable_ports(port):
    with pytest.raises(ValueError):
        hub.validate_config(dict(web_port=port, api_port=8195, name='develop-hub-dev', public_host='localhost'))


def test_symlink_deployment_root_is_rejected(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(real)
    with pytest.raises(ValueError):
        hub.validate_root(alias)


@pytest.mark.parametrize('action,flags', [('apply', []), ('apply', ['--approve-stable-update']), ('rollback', []), ('rollback', ['--approve-stable-rollback'])])
def test_stable_mutation_requires_approval_and_maintenance_before_reading_files(action, flags, tmp_path):
    control = tmp_path / 'must-not-be-created'
    with pytest.raises(ValueError, match='Explicit stable approval'):
        stable.main([action, '--control-root', str(control)] + flags)
    assert not control.exists()


def test_database_restore_needs_separate_data_loss_acknowledgment(tmp_path):
    control = tmp_path / 'must-not-be-created'
    with pytest.raises(ValueError, match='accept-data-loss'):
        stable.main(['rollback', '--control-root', str(control), '--approve-stable-rollback', '--maintenance-confirmed', '--restore-database'])
    assert not control.exists()


def test_live_wal_backup_keeps_committed_records_and_source(tmp_path):
    live = tmp_path / 'live.db'
    with sqlite3.connect(live) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE evidence (value TEXT)')
        writer.execute("INSERT INTO evidence VALUES ('preserved')")
        writer.commit()
        hub.backup_db(live, tmp_path / 'backup.db')
        assert writer.execute('SELECT value FROM evidence').fetchone()[0] == 'preserved'
    with sqlite3.connect(tmp_path / 'backup.db') as backup:
        assert backup.execute('SELECT value FROM evidence').fetchone()[0] == 'preserved'


def schema_db(path, version='old'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE alembic_version (version_num TEXT)')
        db.execute('INSERT INTO alembic_version VALUES (?)', (version,))
        db.execute('CREATE TABLE testing_runs (state TEXT)')


def test_schema_change_blocks_deployment_before_any_process_stop(tmp_path, monkeypatch):
    schema_db(tmp_path / 'var/testing.db')
    calls = []
    monkeypatch.setattr(hub, 'run', lambda *a, **kw: calls.append(a))
    with pytest.raises(RuntimeError, match='Schema change'):
        hub.activate(tmp_path, {'name': 'dhdev-test'}, tmp_path / 'release', {'profile': 'development', 'database_schema': ['new'], 'id': 'test'})
    assert calls == []


def test_failed_development_start_recovers_retained_containers(tmp_path, monkeypatch):
    schema_db(tmp_path / 'var/testing.db')
    calls = []
    monkeypatch.setattr(hub, 'run', lambda argv, **kw: calls.append([str(x) for x in argv]))
    monkeypatch.setattr(hub, 'container_exists', lambda name: True)
    monkeypatch.setattr(hub, 'start_pair', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('candidate failed')))
    manifest = {'profile': 'development', 'database_schema': ['old'], 'id': 'test', 'image': 'test-image', 'commit': 'a' * 40}
    config = {'name': 'dhdev-test', 'public_host': 'localhost', 'web_port': 3025, 'api_port': 8195}
    with pytest.raises(RuntimeError, match='candidate failed'):
        hub.activate(tmp_path, config, tmp_path / 'release', manifest)
    assert ['docker', 'start', 'dhdev-test-api'] in calls
    assert ['docker', 'start', 'dhdev-test-web'] in calls
    assert not (tmp_path / 'active.json').exists()


def test_running_or_waiting_operator_tasks_block_cutover(tmp_path):
    path = tmp_path / 'testing.db'
    schema_db(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO testing_runs VALUES ('awaiting_operator')")
    with pytest.raises(RuntimeError, match='active development runs'):
        hub.require_idle(path)


def test_failed_rename_does_not_delete_an_untouched_old_container(tmp_path, monkeypatch):
    schema_db(tmp_path / 'var/testing.db')
    calls = []
    def command(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ['docker', 'inspect']:
            return 'original-' + argv[-1]
        if argv[:2] == ['docker', 'rename']:
            raise RuntimeError('rename failed')
    monkeypatch.setattr(hub, 'run', command)
    monkeypatch.setattr(hub, 'container_exists', lambda name: True)
    config = {'name': 'dhdev-test', 'public_host': 'localhost', 'web_port': 3025, 'api_port': 8195}
    manifest = {'profile': 'development', 'database_schema': ['old'], 'id': 'test', 'image': 'image', 'commit': 'a' * 40}
    with pytest.raises(RuntimeError, match='rename failed'):
        hub.activate(tmp_path, config, tmp_path / 'release', manifest)
    assert not any(call[:3] == ['docker', 'rm', '-f'] for call in calls)
    assert ['docker', 'start', 'dhdev-test-api'] in calls
    assert ['docker', 'start', 'dhdev-test-web'] in calls


def test_reactivating_same_release_uses_unique_retention_names(tmp_path, monkeypatch):
    schema_db(tmp_path / 'var/testing.db')
    calls = []
    monkeypatch.setattr(hub, 'run', lambda argv, **kwargs: calls.append(list(argv)))
    monkeypatch.setattr(hub, 'container_exists', lambda name: True)
    monkeypatch.setattr(hub, 'start_pair', lambda *args: {'status': 'ok'})
    config = {'name': 'dhdev-test', 'public_host': 'localhost', 'web_port': 3025, 'api_port': 8195}
    manifest = {'profile': 'development', 'database_schema': ['old'], 'id': 'test', 'image': 'image', 'commit': 'a' * 40}
    hub.activate(tmp_path, config, tmp_path / 'release', manifest)
    hub.activate(tmp_path, config, tmp_path / 'release', manifest)
    destinations = [call[-1] for call in calls if call[:2] == ['docker', 'rename']]
    assert len(destinations) == len(set(destinations)) == 4


def test_package_tamper_is_detected(tmp_path):
    (tmp_path / 'server.py').write_text('original')
    manifest = {'profile': 'production', 'tests_passed': True, 'frontend_passed': True, 'file_hashes': {'server.py': hub.sha(tmp_path / 'server.py')}}
    (tmp_path / 'package.json').write_text(json.dumps(manifest))
    stable.verify_package(tmp_path)
    (tmp_path / 'server.py').write_text('changed')
    with pytest.raises(ValueError, match='mismatch'):
        stable.verify_package(tmp_path)


def test_python_and_node_are_downloaded_or_pinned_not_read_from_a_host_install():
    dockerfile = (DEPLOY / 'Dockerfile').read_text()
    lock = json.loads((DEPLOY / 'toolchain.lock.json').read_text())
    assert 'FROM ' + lock['python_image'] in dockerfile
    assert 'COPY node/' not in dockerfile and '/home/nwj' not in dockerfile
    assert len(lock['node_sha256']) == 64
    requirements = (DEPLOY.parents[1] / 'requirements/development-linux-amd64.lock').read_text()
    assert all('==' in line and '--hash=sha256:' in line for line in requirements.splitlines() if line and not line.startswith('#'))


def test_stale_or_reused_pid_is_never_signaled(monkeypatch):
    monkeypatch.setattr(stable, 'unchanged', lambda value: False)
    signals = []
    monkeypatch.setattr(stable.os, 'kill', lambda *args: signals.append(args))
    with pytest.raises(RuntimeError, match='identity changed'):
        stable.stop({'pid': 123})
    assert signals == []


def test_failed_stable_candidate_restores_previous_processes(tmp_path, monkeypatch):
    path = tmp_path / 'package'
    path.mkdir()
    (path / 'package.json').write_text('{}')
    database = tmp_path / 'live.db'
    schema_db(database)
    old = {'api': {'tag': 'old-api'}, 'web': {'tag': 'old-web'}}
    plan = {'id': 'test', 'created_at': time.time(), 'package': str(path), 'package_hash': hub.sha(path / 'package.json'), 'database': str(database), 'old': old, 'api_port': 8185, 'web_port': 3015}
    plan_path = tmp_path / 'plan.json'
    plan_path.write_text(json.dumps(plan))
    monkeypatch.setattr(stable, 'verify_package', lambda value: {'database_schema': ['old'], 'commit': 'a' * 40})
    monkeypatch.setattr(stable, 'unchanged', lambda value: True)
    monkeypatch.setattr(stable, 'setup_native', lambda *args: None)
    monkeypatch.setattr(stable, 'native_records', lambda *args: {'api': {'tag': 'new-api'}, 'web': {'tag': 'new-web'}})
    stopped, launched = [], []
    monkeypatch.setattr(stable, 'stop', lambda record: stopped.append(record['tag']))
    def launch(record, log):
        launched.append(record['tag'])
        if record['tag'] == 'new-web':
            raise RuntimeError('web failed')
        return record
    monkeypatch.setattr(stable, 'launch', launch)
    monkeypatch.setattr(stable, 'healthy', lambda *args: {'status': 'ok'})
    with pytest.raises(RuntimeError, match='web failed'):
        stable.apply(plan_path, tmp_path, '/usr/bin/python3')
    assert stopped == ['old-web', 'old-api', 'new-api']
    assert launched[-2:] == ['old-api', 'old-web']
    assert json.loads((tmp_path / 'test.deployment.json').read_text())['status'] == 'rolled_back_after_start_failure'


def test_crashed_new_api_can_be_rolled_back_when_tasks_are_idle(tmp_path, monkeypatch):
    database = tmp_path / 'live.db'
    schema_db(database)
    record = {'id': 'test', 'status': 'healthy', 'new': {'api': {'pid': 1, 'dead': True}, 'web': {'pid': 2}}, 'old': {'api': {'tag': 'old-api'}, 'web': {'tag': 'old-web'}}, 'api_port': 8185, 'web_port': 3015, 'database': str(database), 'database_schema': ['old']}
    file = tmp_path / 'record.json'
    file.write_text(json.dumps(record))
    monkeypatch.setattr(stable, 'pid_on_port', lambda port, allow_missing=False: None if port == 8185 else 2)
    monkeypatch.setattr(stable, 'unchanged', lambda value: True)
    monkeypatch.setattr(stable, 'process_alive', lambda value: not value.get('dead', False))
    stopped, launched = [], []
    monkeypatch.setattr(stable, 'stop', lambda value: stopped.append(value['pid']))
    monkeypatch.setattr(stable, 'launch', lambda value, log: launched.append(value['tag']) or value)
    monkeypatch.setattr(stable, 'healthy', lambda *args: {'status': 'ok'})
    stable.rollback(file, tmp_path)
    assert stopped == [2]
    assert launched == ['old-api', 'old-web']


def test_rollback_refuses_an_unrelated_listener_without_signaling_it(tmp_path, monkeypatch):
    file = tmp_path / 'record.json'
    file.write_text(json.dumps({'status': 'healthy', 'new': {'api': {'pid': 1}, 'web': {'pid': 2}}, 'api_port': 8185, 'web_port': 3015}))
    monkeypatch.setattr(stable, 'pid_on_port', lambda *args, **kwargs: 999)
    calls = []
    monkeypatch.setattr(stable, 'stop', lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match='Another process owns'):
        stable.rollback(file, tmp_path)
    assert calls == []
