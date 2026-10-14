import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from develop_hub.testing import safety, service, execution, managed_devices

@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setenv('DEVELOP_HUB_ENV', 'isolated-development')
    monkeypatch.setenv('DEVELOP_HUB_DEVICE_MODE', 'simulation-only')

@pytest.mark.parametrize('kind', ['local', 'ssh', 'invalid'])
def test_registration_rejects_real_nodes(isolated, kind):
    with pytest.raises(HTTPException) as error:
        service.validate_node({'kind': kind})
    assert error.value.status_code == 403

def test_simulation_is_allowed(isolated):
    service.validate_node({'kind': 'simulation'})

def test_managed_station_cannot_bypass_restriction(isolated):
    with pytest.raises(HTTPException):
        managed_devices.bind('production', '1234567890', SimpleNamespace(chip='RV1126B'))

@pytest.mark.parametrize('kind', ['local', 'ssh'])
def test_transport_is_blocked_before_any_job_file(isolated, tmp_path, kind):
    folder = tmp_path / 'blocked'
    with pytest.raises(HTTPException):
        asyncio.run(execution.execute_step({'kind': kind}, folder, {}))
    assert not folder.exists()

def test_environment_mode_cannot_be_disabled_by_device_flag(isolated, monkeypatch):
    monkeypatch.setenv('DEVELOP_HUB_DEVICE_MODE', 'enabled')
    with pytest.raises(HTTPException):
        safety.require_simulation({'kind': 'ssh'})

def test_startup_refuses_production_data_paths(isolated, monkeypatch):
    monkeypatch.setenv('DEVELOP_HUB_LEGACY_ENABLED', 'false')
    settings = SimpleNamespace(database_url='sqlite:////home/nwj/develop-hub-redesign/var/preview/testing.db', artifact_root=Path('/var/dev/artifacts'), worktree_root=Path('/var/dev/worktrees'), qa_plugin_root=Path('/var/dev/plugins'))
    with pytest.raises(RuntimeError):
        safety.validate_isolated_paths(settings)
