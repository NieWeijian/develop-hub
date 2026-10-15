from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from develop_hub.testing.firmware_browser import browse
from test_testing_execution import application, post  # noqa: F401


def test_browse_select_and_import_without_typed_file_paths(application, tmp_path, monkeypatch):
    root = tmp_path / 'SDK'; images = root / '输出 镜像'; images.mkdir(parents=True)
    for name in ('boot.img', 'system.img', 'oem.img'):
        (images / name).write_bytes(name.encode())
    (images / 'empty.img').touch()
    (root / '.credentials').mkdir()
    (root / 'private.txt').write_text('not listed')
    monkeypatch.setenv('DEVELOP_HUB_FIRMWARE_BROWSE_ROOTS', str(root))
    with TestClient(application) as client:
        top = client.get('/api/testing/firmware-directories').json()
        assert top['parent'] is None and top['path'] == ''
        assert [d['name'] for d in top['directories']] == ['输出 镜像']
        assert not top['files']
        nested = client.get('/api/testing/firmware-directories', params={'root_id': top['root_id'], 'path': top['directories'][0]['path']}).json()
        assert nested['parent'] == ''
        selected = [f for f in nested['files'] if f['size_bytes']]
        saved = post(client, '/firmware-imports', {'name': 'picked images', 'paths': [f['path'] for f in selected]})
        assert {f['path'] for f in saved['manifest']['files']} == {'boot.img', 'system.img', 'oem.img'}
        assert all(f['sha256'] for f in saved['manifest']['files'])
        assert not client.get('/api/testing/test-runs').json()


@pytest.mark.parametrize('path', ['../outside', '/etc', '.credentials', 'a/../../outside', r'a\..\outside'])
def test_browse_cannot_leave_roots_or_visit_hidden_paths(tmp_path, path):
    with pytest.raises(HTTPException) as error:
        browse([{'name': 'root', 'path': str(tmp_path)}], relative=path)
    assert error.value.status_code == 403


def test_symlink_boundaries_and_navigation_errors(tmp_path, monkeypatch):
    root = tmp_path / 'root'; root.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    (outside / 'private.img').write_bytes(b'secret')
    (root / 'out').symlink_to(outside, target_is_directory=True)
    (root / 'escape.img').symlink_to(outside / 'private.img')
    (root / 'images').mkdir(); (root / 'images' / 'boot.img').write_bytes(b'boot')
    (root / 'linked').symlink_to(root / 'images', target_is_directory=True)
    roots = [{'name': 'root', 'path': str(root)}]
    listing = browse(roots)
    assert {d['name'] for d in listing['directories']} == {'images', 'linked'}
    assert not listing['files']
    assert browse(roots, relative='linked')['files'][0]['name'] == 'boot.img'
    for relative, status in [('out', 403), ('missing', 404)]:
        with pytest.raises(HTTPException) as error: browse(roots, relative=relative)
        assert error.value.status_code == status
    with pytest.raises(HTTPException) as error: browse(roots, root_id='unknown')
    assert error.value.status_code == 404
    def denied(*args): raise PermissionError()
    monkeypatch.setattr('develop_hub.testing.firmware_browser.os.scandir', denied)
    with pytest.raises(HTTPException) as error: browse(roots)
    assert error.value.status_code == 403


def test_default_roots_are_registered_local_sdks(application, tmp_path, monkeypatch):
    monkeypatch.delenv('DEVELOP_HUB_FIRMWARE_BROWSE_ROOTS', raising=False)
    with TestClient(application) as client:
        assert client.get('/api/testing/firmware-directories').json()['roots'] == []
        post(client, '/build-environments', {'name': 'local SDK', 'node': {'kind': 'local'}, 'config': {'sdk_path': str(tmp_path)}})
        listing = client.get('/api/testing/firmware-directories').json()
        assert listing['roots'][0]['name'] == 'local SDK'
        assert listing['roots'][0]['path'] == str(tmp_path)
