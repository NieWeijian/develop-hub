from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from develop_hub.testing import sources


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).decode().strip()


def repository(root: Path):
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Snapshot Test")
    git(root, "config", "user.email", "snapshot@example.invalid")
    (root / "tracked.txt").write_text("committed\n")
    (root / "delete.txt").write_text("delete me\n")
    (root / "rename.txt").write_text("rename me\n")
    (root / ".gitignore").write_text("dependency.bin\noutput/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def original_state(root):
    return (git(root, "rev-parse", "HEAD"), git(root, "symbolic-ref", "HEAD"),
            (root / ".git/index").read_bytes(), git(root, "status", "--porcelain=v1"))


def test_multirepo_capture_preserves_dirty_binary_deleted_untracked_and_index(tmp_path):
    sdk = tmp_path / "sdk"
    app = repository(sdk / "app")
    kernel = repository(sdk / "kernel")
    (app / "tracked.txt").write_text("staged\n")
    git(app, "add", "tracked.txt")
    (app / "tracked.txt").write_text("unstaged wins in build\n")
    (app / "binary.dat").write_bytes(bytes(range(256)))
    git(app, "add", "binary.dat")
    (app / "binary.dat").write_bytes(b"\x00\xffunstaged")
    (app / "delete.txt").unlink()
    git(app, "mv", "rename.txt", "renamed.txt")
    (app / "new.txt").write_text("untracked source")
    (app / "dependency.bin").write_bytes(b"ignored but necessary")
    (app / ".env").write_text("SECRET=must-not-copy")
    (app / "output").mkdir()
    (app / "output/old.img").write_bytes(b"old firmware")
    (kernel / "tracked.txt").write_text("kernel local change")
    before = {path: original_state(path) for path in (app, kernel)}
    snapshot = sources.capture_source(sdk, tmp_path / "snapshot", {"include": ["app/dependency.bin"], "required": ["app/dependency.bin"]})
    tree = Path(snapshot["root_path"])
    assert (tree / "app/tracked.txt").read_text() == "unstaged wins in build\n"
    assert (tree / "app/binary.dat").read_bytes() == b"\x00\xffunstaged"
    assert not (tree / "app/delete.txt").exists()
    assert (tree / "app/renamed.txt").is_file()
    assert (tree / "app/new.txt").is_file()
    assert (tree / "app/dependency.bin").is_file()
    assert not (tree / "app/.env").exists()
    assert not (tree / "app/output").exists()
    assert (tree / "kernel/tracked.txt").read_text() == "kernel local change"
    assert all(original_state(path) == value for path, value in before.items())
    assert git(tree / "app", "rev-parse", "HEAD") == before[app][0]
    assert not git(tree / "app", "remote")
    assert snapshot["capture_window"]["atomic"] is False


def test_inspect_manifest_includes_local_projects_and_readable_status(tmp_path):
    sdk = tmp_path / "sdk"
    repository(sdk / "app")
    repository(sdk / "kernel")
    manifests = sdk / ".repo/manifests"
    manifests.mkdir(parents=True)
    (manifests / "base.xml").write_text('<manifest><include name="extra.xml"/><project name="application" path="app"/></manifest>')
    (manifests / "extra.xml").write_text('<manifest><project name="kernel" path="kernel"/></manifest>')
    (sdk / ".repo/manifest.xml").write_text((manifests / "base.xml").read_text())
    (sdk / ".repo/local_manifests").mkdir()
    (sdk / ".repo/local_manifests/local.xml").write_text('<manifest><project name="missing" path="vendor/missing"/></manifest>')
    (sdk / "app/untracked.txt").write_text("local")
    inspection = sources.inspect_sdk(sdk)
    assert inspection["manifest"]["missing_projects"] == ["vendor/missing"]
    app = next(r for r in inspection["repositories"] if r["path"] == "app")
    assert app["status"] == "untracked" and app["subject"] == "initial"
    assert app["untracked_files"] == ["untracked.txt"]


def test_ignored_dependency_requires_explicit_include(tmp_path):
    sdk = repository(tmp_path / "sdk")
    (sdk / "dependency.bin").write_bytes(b"required")
    with pytest.raises(sources.SourceError, match="缺少"):
        sources.capture_source(sdk, tmp_path / "snap", {"required": ["dependency.bin"]})
    assert not (tmp_path / "snap").exists()


def test_capture_detects_changes_after_copy_and_does_not_publish(tmp_path, monkeypatch):
    sdk = repository(tmp_path / "sdk")
    original_copy = sources._copy_entries

    def mutate(source, target, entries):
        original_copy(source, target, entries)
        (source / "tracked.txt").write_text("changed during capture")

    monkeypatch.setattr(sources, "_copy_entries", mutate)
    with pytest.raises(sources.SourceError) as error:
        sources.capture_source(sdk, tmp_path / "snapshot")
    assert error.value.code == "source_drift"
    assert not (tmp_path / "snapshot").exists()


def test_snapshot_destination_cannot_be_within_sdk(tmp_path):
    sdk = repository(tmp_path / "sdk")
    with pytest.raises(sources.SourceError) as error:
        sources.capture_source(sdk, sdk / "snapshot")
    assert error.value.code == "destination_overlap"


def test_symlink_internal_rebased_and_external_rejected(tmp_path):
    sdk = repository(tmp_path / "sdk")
    try:
        (sdk / "a-link.txt").symlink_to(sdk / "tracked.txt")
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    snapshot = sources.capture_source(sdk, tmp_path / "snapshot")
    tree = Path(snapshot["root_path"])
    assert (tree / "a-link.txt").read_text() == "committed\n"
    assert not Path(os.readlink(tree / "a-link.txt")).is_absolute()
    external = tmp_path / "external"
    external.write_text("mutable")
    (sdk / "external-link").symlink_to(external)
    with pytest.raises(sources.SourceError) as error:
        sources.capture_source(sdk, tmp_path / "snapshot2")
    assert error.value.code == "external_symlink"


def test_remote_override_changes_only_copy_and_preserves_selected_commit(tmp_path):
    sdk = tmp_path / "sdk"
    app = repository(sdk / "app")
    kernel = repository(sdk / "kernel")
    remote = repository(tmp_path / "remote")
    (remote / "tracked.txt").write_text("remote version")
    (remote / ".env").write_text("not distributable")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "remote version")
    commit = git(remote, "rev-parse", "HEAD")
    (app / "tracked.txt").write_text("local dirty remains")
    before = original_state(app)
    snapshot = sources.capture_source(sdk, tmp_path / "snapshot", repository_overrides=[{"path": "app", "url": str(remote), "ref": "HEAD", "commit": commit}])
    tree = Path(snapshot["root_path"])
    assert (tree / "app/tracked.txt").read_text() == "remote version"
    assert git(tree / "app", "rev-parse", "HEAD") == commit
    assert (tree / "kernel/tracked.txt").read_text() == "committed\n"
    assert original_state(app) == before
    assert not (tree / "app/.env").exists()
    assert snapshot["repository_overrides"][0]["local_changes_applied"] is False


def test_build_is_separate_and_seals_new_firmware(tmp_path):
    sdk = repository(tmp_path / "sdk")
    snapshot = sources.capture_source(sdk, tmp_path / "snapshot")
    script = "from pathlib import Path; p=Path('build'); p.mkdir(); (p/'update.img').write_bytes(Path('tracked.txt').read_bytes()); Path('tracked.txt').write_text('build modified copy')"
    result = sources.build_snapshot(snapshot, {"argv": [sys.executable, "-c", script], "artifacts": ["build/*.img"]}, tmp_path / "build-run")
    assert result["build"]["exit_code"] == 0
    bundle = result["firmware_bundle"]
    assert (Path(bundle["root_path"]) / "build/update.img").read_bytes() == b"committed\n"
    assert (sdk / "tracked.txt").read_text() == "committed\n"
    assert (Path(snapshot["root_path"]) / "tracked.txt").read_text() == "committed\n"
    assert (tmp_path / "build-run/worktree/tracked.txt").read_text() == "build modified copy"


def test_snapshot_tampering_prevents_build(tmp_path):
    sdk = repository(tmp_path / "sdk")
    snapshot = sources.capture_source(sdk, tmp_path / "snapshot")
    (Path(snapshot["root_path"]) / "tracked.txt").write_text("tampered")
    with pytest.raises(sources.SourceError) as error:
        sources.build_snapshot(snapshot, {"argv": [sys.executable, "-V"], "artifacts": ["*.img"]}, tmp_path / "build")
    assert error.value.code == "snapshot_changed"


def test_build_rejects_original_sdk_destination_and_command_reference(tmp_path):
    sdk = repository(tmp_path / "sdk")
    snapshot = sources.capture_source(sdk, tmp_path / "snapshot")
    recipe = {"argv": [sys.executable, "-V"], "artifacts": ["*.img"]}
    with pytest.raises(sources.SourceError) as error:
        sources.build_snapshot(snapshot, recipe, sdk / "build-output")
    assert error.value.code == "destination_overlap"
    with pytest.raises(sources.SourceError) as error:
        sources.build_snapshot(snapshot, {**recipe, "argv": ["bash", str(sdk / "build.sh")]}, tmp_path / "build-output")
    assert error.value.code == "source_path_reference"


def test_git_clone_has_no_alternate_object_dependency(tmp_path):
    original = repository(tmp_path / "original")
    sdk = tmp_path / "sdk"
    subprocess.check_call(["git", "clone", "-q", "--shared", str(original), str(sdk)])
    assert (sdk / ".git/objects/info/alternates").exists()
    snapshot = sources.capture_source(sdk, tmp_path / "snapshot")
    tree = Path(snapshot["root_path"])
    assert not (tree / ".git/objects/info/alternates").exists()
    assert git(tree, "rev-parse", "HEAD") == git(original, "rev-parse", "HEAD")


@pytest.mark.parametrize("script,timeout", [("raise SystemExit(7)", 10), ("import time; time.sleep(10)", 0.1)])
def test_failed_or_timedout_build_has_no_firmware(tmp_path, script, timeout):
    sdk = repository(tmp_path / "sdk")
    snapshot = sources.capture_source(sdk, tmp_path / "snapshot")
    result = sources.build_snapshot(snapshot, {"argv": [sys.executable, "-c", script], "timeout_seconds": timeout, "artifacts": ["*.img"]}, tmp_path / "build")
    assert result["build"]["exit_code"] != 0
    assert result["firmware_bundle"] is None
    assert result["build"]["timed_out"] == (timeout < 1)


def test_firmware_import_freezes_content_and_rejects_collisions(tmp_path):
    image = tmp_path / "update.img"
    image.write_bytes(b"firmware-one")
    bundle = sources.import_firmware([image], tmp_path / "bundle")
    image.write_bytes(b"firmware-two")
    assert (Path(bundle["root_path"]) / "update.img").read_bytes() == b"firmware-one"
    assert bundle["files"][0]["sha256"] == hashlib.sha256(b"firmware-one").hexdigest()
    with pytest.raises(sources.SourceError, match="重复"):
        sources.import_firmware([image, image], tmp_path / "duplicate")
    with pytest.raises(sources.SourceError, match="保留"):
        sources.import_firmware([{"path": str(image), "name": "bundle.json"}], tmp_path / "reserved")
