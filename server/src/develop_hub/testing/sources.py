"""Read-only SDK capture and isolated, explicitly configured builds.

These synchronous functions run on the node owning the paths. Callers provide
resource locking and execute them in a worker thread. This is not a code sandbox.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any
import xml.etree.ElementTree as ET


class SourceError(ValueError):
    def __init__(self, message: str, code: str = "invalid_source", **details: Any):
        super().__init__(message)
        self.code, self.details = code, details


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _relative(value: str, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value or ":" in value:
        raise SourceError("必须使用 SDK 内的相对路径", path=value)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or (str(path) == "." and not allow_dot):
        raise SourceError("路径不能越出 SDK", path=value)
    return str(path)


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=root,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
                            capture_output=True, timeout=300)
    if check and result.returncode:
        raise SourceError("Git 操作失败", "git_error", command=args[0], path=str(root),
                          stderr=result.stderr.decode("utf-8", "replace")[-2000:])
    return result.stdout.decode("utf-8", "surrogateescape").strip("\r\n")


def _manifest(root: Path) -> dict:
    main = root / ".repo/manifest.xml"
    if not main.exists():
        return {"present": False, "projects": [], "files": []}
    projects: dict[str, dict] = {}
    files: list[str] = []
    seen: set[Path] = set()

    def read(path: Path) -> None:
        resolved = path.resolve()
        if not _within(resolved, root):
            raise SourceError("Manifest 链接指向 SDK 外部", path=str(path))
        if resolved in seen:
            return
        seen.add(resolved)
        try:
            document = ET.parse(resolved).getroot()
        except (ET.ParseError, OSError) as exc:
            raise SourceError("无法解析 SDK Manifest", path=str(path)) from exc
        files.append(path.relative_to(root).as_posix())
        for element in document:
            if element.tag == "include":
                name = _relative(element.get("name", ""))
                read(root / ".repo/manifests" / name)
            elif element.tag == "remove-project":
                for key in list(projects):
                    if projects[key]["name"] == element.get("name"):
                        del projects[key]
            elif element.tag == "project":
                name = element.get("name", "")
                relative = _relative(element.get("path") or name)
                projects[relative] = {"path": relative, "name": name, "revision": element.get("revision", "")}
            elif element.tag == "extend-project":
                for project in projects.values():
                    if project["name"] == element.get("name") and (not element.get("path") or project["path"] == element.get("path")):
                        if element.get("revision"):
                            project["revision"] = element.get("revision")
        # Remote/default XML remains in captured files; project list is for UI.

    read(main)
    for local in sorted((root / ".repo/local_manifests").glob("*.xml")):
        read(local)
    return {"present": True, "projects": list(projects.values()), "files": files}


_CACHE_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".cache", ".venv", "venv"}
_OUTPUT_DIRS = {"output", "out", "dist", "build", "rockdev", "image", "images"}
_SECRET_NAMES = {".env", ".netrc", ".git-credentials", "id_rsa", "id_ed25519", "id_ecdsa", "credentials.json"}


def _repositories(root: Path, manifest: dict) -> list[Path]:
    found: set[Path] = set()
    for current, dirs, _ in os.walk(root, followlinks=False):
        directory = Path(current)
        if (directory / ".git").exists():
            found.add(directory)
        dirs[:] = [name for name in dirs if name not in _CACHE_DIRS | _OUTPUT_DIRS
                   and not (directory.name == ".repo" and name in {"projects", "project-objects", "repo"})
                   and not (directory / name).is_symlink()]
    for item in manifest["projects"]:
        path = root / item["path"]
        if (path / ".git").exists():
            found.add(path)
    return sorted(found, key=lambda item: item.relative_to(root).as_posix())


def _repo_status(root: Path, repo: Path) -> dict:
    # Porcelain -z preserves filenames containing spaces and line breaks.
    entries = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
    changes, untracked = [], []
    offset = 0
    while offset < len(entries):
        value = entries[offset]
        offset += 1
        if not value:
            continue
        state, path = value[:2], value[3:]
        if state == "??":
            untracked.append(path)
        else:
            item = {"path": path, "index": state[0], "worktree": state[1]}
            if "R" in state or "C" in state:
                item["original_path"] = entries[offset]
                offset += 1
            changes.append(item)
    return {"path": repo.relative_to(root).as_posix(),
            "branch": _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) or "detached",
            "head": _git(repo, "rev-parse", "--verify", "HEAD", check=False),
            "subject": _git(repo, "log", "-1", "--format=%s", check=False),
            "status": "modified" if changes else "untracked" if untracked else "clean",
            "tracked_changes": changes, "untracked_files": untracked}


def inspect_sdk(path: str | Path) -> dict:
    root = Path(path).resolve()
    if not root.is_dir():
        raise SourceError("SDK 目录不存在", path=str(root))
    manifest = _manifest(root)
    repositories = [_repo_status(root, repo) for repo in _repositories(root, manifest)]
    known = {repo["path"] for repo in repositories}
    manifest["missing_projects"] = [p["path"] for p in manifest["projects"] if p["path"] not in known]
    return {"sdk_path": str(root), "inspected_at": _now(), "manifest": manifest, "repositories": repositories}


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) or (pattern.endswith("/**") and path == pattern[:-3]) for pattern in patterns)


def _entry(root: Path, path: Path) -> dict:
    info = path.lstat()
    relative = path.relative_to(root).as_posix()
    if stat.S_ISLNK(info.st_mode):
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SourceError("符号链接目标不存在或形成循环", "invalid_symlink", path=relative) from exc
        if not _within(target, root):
            raise SourceError("符号链接依赖 SDK 外部文件，请先将依赖纳入 SDK", "external_symlink", path=relative, target=str(target))
        # Rebase absolute links so neither sealed nor restored trees read source.
        link = os.path.relpath(target, path.parent).replace("\\", "/")
        return {"path": relative, "kind": "symlink", "target": link}
    if not stat.S_ISREG(info.st_mode):
        raise SourceError("源码包含非普通文件", path=relative)
    return {"path": relative, "kind": "file", "size_bytes": info.st_size,
            "mode": stat.S_IMODE(info.st_mode), "sha256": _sha(path)}


def _inventory(root: Path, rules: dict, repositories: list[dict]) -> tuple[list[dict], list[dict]]:
    include = rules.get("include", rules.get("include_ignored", []))
    exclude = rules.get("exclude", [])
    ignored: set[str] = set()
    for repo in repositories:
        location = root / repo["path"]
        for relative in _git(location, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0"):
            if relative:
                ignored.add((PurePosixPath(repo["path"]) / relative).as_posix())
    selected, excluded = [], []
    for current, directories, names in os.walk(root, followlinks=False):
        base = Path(current)
        for name in sorted(list(directories) + names):
            path = base / name
            relative = path.relative_to(root).as_posix()
            directory = path.is_dir() and not path.is_symlink()
            reason = None
            secret = name in _SECRET_NAMES or name in {".ssh", ".aws"} or name.endswith((".key", ".pem")) or name.startswith(".env.")
            metadata = name == ".git" or relative.startswith((".repo/projects", ".repo/project-objects", ".repo/repo"))
            if secret:
                reason = "credential"
            elif metadata:
                reason = "git_metadata_rebuilt"
            elif _matches(relative, exclude):
                reason = "configured_exclusion"
            elif not _matches(relative, include):
                if name in _CACHE_DIRS | _OUTPUT_DIRS:
                    # An explicit nested include may need traversal through output.
                    if not directory or not any(p.startswith(relative + "/") for p in include):
                        reason = "output_or_cache"
                if relative in ignored:
                    reason = "ignored_not_included"
            if reason:
                excluded.append({"path": relative, "reason": reason})
                if name in directories:
                    directories.remove(name)
                continue
            if directory:
                continue
            selected.append(_entry(root, path))
            if name in directories:  # os.walk lists symlinks to directories here.
                directories.remove(name)
    selected.sort(key=lambda item: item["path"])
    names = {item["path"] for item in selected}
    for pattern in rules.get("required", []):
        if not any(_matches(name, [pattern]) for name in names):
            raise SourceError("缺少构建必需输入", "missing_input", pattern=pattern)
    maximum = rules.get("max_bytes", 100 * 1024**3)
    if sum(item.get("size_bytes", 0) for item in selected) > maximum:
        raise SourceError("源码快照超过空间预算", "source_too_large")
    return selected, sorted(excluded, key=lambda item: item["path"])


def _copy_entries(source: Path, target: Path, entries: list[dict]) -> None:
    for item in entries:
        src, dst = source / item["path"], target / item["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        if item["kind"] == "symlink":
            dst.symlink_to(item["target"], target_is_directory=src.is_dir())
        else:
            shutil.copy2(src, dst)
    for item in entries:
        if _entry(target, target / item["path"]) != item:
            raise SourceError("复制内容与采集记录不同，请重试", "source_drift", path=item["path"])


def _all_entries(root: Path) -> list[dict]:
    entries = []
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(current) / name
            if path.is_symlink():
                entries.append(_entry(root, path))
                dirs.remove(name)
        entries.extend(_entry(root, Path(current) / name) for name in files)
    return sorted(entries, key=lambda entry: entry["path"])


def _new_destination(destination: str | Path, source: Path | None = None) -> tuple[Path, Path]:
    target = Path(destination).resolve()
    if source and (_within(target, source) or _within(source, target)):
        raise SourceError("快照或构建目标必须与 SDK 目录分离", "destination_overlap")
    if target.exists():
        raise SourceError("目标已存在，禁止覆盖已封存内容", "destination_exists", path=str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".rk-capture-", dir=target.parent))
    return target, stage


def _preserve_git(source: Path, tree: Path, repositories: list[dict], stage: Path) -> None:
    for ordinal, repo in enumerate(repositories):
        if not repo["head"]:
            continue
        clone = stage / f"git-{ordinal}"
        # no-local avoids copying alternates that could retain source dependencies.
        _git(stage, "clone", "--no-local", "--no-hardlinks", "--no-checkout", "--", str(source / repo["path"]), str(clone))
        if _git(clone, "rev-parse", "HEAD") != repo["head"]:
            raise SourceError("复制 Git 元数据时源码发生变化", "source_drift", path=repo["path"])
        _git(clone, "remote", "remove", "origin")
        _git(clone, "read-tree", repo["head"])
        destination = tree / repo["path"] / ".git"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(clone / ".git"), destination)
        shutil.rmtree(clone)


def _override(tree: Path, item: dict, stage: Path, ordinal: int, repositories: list[dict]) -> dict:
    relative = _relative(item.get("path", ""), allow_dot=True)
    if relative not in {r["path"] for r in repositories}:
        raise SourceError("覆盖目标必须是已发现的 Git 仓库", path=relative)
    if any(r["path"].startswith(relative.rstrip("/") + "/") for r in repositories if r["path"] != relative):
        raise SourceError("覆盖仓库包含嵌套仓库，请分别配置覆盖", path=relative)
    url, ref = item.get("url"), item.get("ref") or item.get("commit")
    if not isinstance(url, str) or not url or url.startswith("-") or not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise SourceError("远端覆盖必须提供合法 url 和 ref")
    from urllib.parse import urlsplit
    if "://" in url and urlsplit(url).password:
        raise SourceError("远端地址不能包含密码")
    clone = stage / f"override-{ordinal}"
    clone.mkdir()
    _git(clone, "init")
    _git(clone, "fetch", "--no-tags", "--", url, ref)
    commit = _git(clone, "rev-parse", "FETCH_HEAD^{commit}")
    if item.get("commit") and commit.lower() != item["commit"].lower():
        raise SourceError("远端版本与指定 commit 不一致", "commit_mismatch")
    _git(clone, "checkout", "--detach", commit)
    # Inspect every link before the clone is installed into the SDK copy.
    _all_entries(clone)
    destination = (tree / relative).resolve()
    if not _within(destination, tree):
        raise SourceError("覆盖路径越界")
    if destination == tree:
        for child in list(tree.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in list(clone.iterdir()):
            shutil.move(str(child), tree / child.name)
        clone.rmdir()
    else:
        shutil.rmtree(destination)
        shutil.move(str(clone), destination)
    return {"path": relative, "url": url, "ref": ref, "commit": commit, "local_changes_applied": False}


def capture_source(sdk_path: str | Path, destination: str | Path,
                   input_rules: dict | None = None, repository_overrides: list[dict] | None = None) -> dict:
    source = Path(sdk_path).resolve()
    before = inspect_sdk(source)
    rules, overrides = input_rules or {}, repository_overrides or []
    if set(rules) - {"include", "include_ignored", "exclude", "required", "max_bytes"}:
        raise SourceError("不支持的源码输入规则")
    for key in ("include", "include_ignored", "exclude", "required"):
        if key in rules and (not isinstance(rules[key], list) or any(not isinstance(v, str) or ".." in PurePosixPath(v).parts or v.startswith("/") for v in rules[key])):
            raise SourceError("输入规则必须是相对路径模式列表", field=key)
    if "max_bytes" in rules and (type(rules["max_bytes"]) is not int or rules["max_bytes"] <= 0):
        raise SourceError("源码空间预算必须为正整数字节数")
    target, stage = _new_destination(destination, source)
    try:
        started = _now()
        entries, excluded = _inventory(source, rules, before["repositories"])
        tree = stage / "tree"
        tree.mkdir()
        _copy_entries(source, tree, entries)
        _preserve_git(source, tree, before["repositories"], stage)
        after = inspect_sdk(source)
        ending, ending_excluded = _inventory(source, rules, after["repositories"])
        if entries != ending or excluded != ending_excluded or before["repositories"] != after["repositories"] or before["manifest"] != after["manifest"]:
            raise SourceError("源码在采集期间发生变化，请重试", "source_drift")
        # Copy check includes all original inputs, not only their names or mtimes.
        for item in entries:
            if _entry(tree, tree / item["path"]) != item:
                raise SourceError("快照内容与源码指纹不同", "source_drift", path=item["path"])
        resolved = [_override(tree, item, stage, i, before["repositories"]) for i, item in enumerate(overrides)]
        if len({item["path"] for item in resolved}) != len(resolved):
            raise SourceError("同一仓库不能重复覆盖")
        if resolved:
            _, omitted = _inventory(tree, rules, before["repositories"])
            for item in sorted(omitted, key=lambda entry: len(entry["path"]), reverse=True):
                if item["reason"] == "git_metadata_rebuilt":
                    continue
                path = tree / item["path"]
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif path.exists() or path.is_symlink():
                    path.unlink()
                excluded.append({**item, "after_override": True})
        files = _all_entries(tree)
        if sum(item.get("size_bytes", 0) for item in files) > rules.get("max_bytes", 100 * 1024**3):
            raise SourceError("包含独立 Git 对象的源码快照超过空间预算", "source_too_large")
        result = {"source_schema": "rk-source-snapshot/1", "source_path": str(source),
                  "root_path": str(target / "tree"), "manifest_path": str(target / "snapshot.json"),
                  "repositories": before["repositories"], "manifest": before["manifest"],
                  "repository_overrides": resolved, "files": files, "excluded": excluded,
                  "input_rules": rules, "capture_window": {"started_at": started, "completed_at": _now(), "method": "copy_and_verify", "atomic": False}}
        result["digest"] = _digest({"files": files, "overrides": resolved})
        (stage / "snapshot.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        stage.rename(target)
        return result
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def import_firmware(paths: list[str | Path | dict], destination: str | Path) -> dict:
    if not paths:
        raise SourceError("至少选择一个镜像", "empty_firmware")
    target, stage = _new_destination(destination)
    try:
        files, names = [], set()
        for raw in paths:
            item = raw if isinstance(raw, dict) else {"path": str(raw)}
            source = Path(item["path"]).resolve()
            name = _relative(item.get("name", source.name))
            if name.casefold() == "bundle.json":
                raise SourceError("bundle.json 是固件存储保留名称")
            if name.casefold() in names:
                raise SourceError("镜像文件名重复", path=name)
            names.add(name.casefold())
            if not source.is_file() or source.stat().st_size == 0:
                raise SourceError("镜像不存在或为空", path=str(source))
            expected = _sha(source)
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            if _sha(dest) != expected or _sha(source) != expected:
                raise SourceError("镜像导入期间发生变化", "source_drift")
            files.append({"path": name, "size_bytes": dest.stat().st_size, "sha256": expected})
        files.sort(key=lambda item: item["path"])
        result = {"bundle_schema": "rk-firmware-bundle/1", "digest": _digest(files), "root_path": str(target), "files": files}
        (stage / "bundle.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        stage.rename(target)
        return result
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def build_snapshot(snapshot: dict, recipe: dict, outroot: str | Path) -> dict:
    source = Path(snapshot["root_path"]).resolve()
    if snapshot.get("source_schema") != "rk-source-snapshot/1" or _all_entries(source) != snapshot["files"]:
        raise SourceError("封存源码校验失败", "snapshot_changed")
    argv = recipe.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
        raise SourceError("构建命令必须是非空 argv 数组")
    cwd_relative = _relative(recipe.get("cwd", "."), allow_dot=True)
    timeout = recipe.get("timeout_seconds", 3600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
        raise SourceError("构建超时必须在 0 到 86400 秒之间")
    patterns = recipe.get("artifacts", [])
    if not isinstance(patterns, list) or not patterns:
        raise SourceError("构建配方必须指定镜像相对路径或 glob")
    for pattern in patterns:
        _relative(pattern)
    original = Path(snapshot["source_path"]).resolve() if snapshot.get("source_path") else None
    requested_output = Path(outroot).resolve()
    if original and (_within(requested_output, original) or _within(original, requested_output)):
        raise SourceError("构建输出不能位于原 SDK 内或覆盖其上级目录", "destination_overlap")
    if original and any(str(original) in arg for arg in argv):
        raise SourceError("构建命令引用原 SDK，请使用 {source_root} 指向独立副本", "source_path_reference")
    target, stage = _new_destination(outroot, source)
    # Build directory must retain its final name while commands run.
    stage.rename(target)
    tree, logs = target / "worktree", target / "logs"
    logs.mkdir()
    shutil.copytree(source, tree, symlinks=True)
    if _all_entries(tree) != snapshot["files"]:
        raise SourceError("独立构建目录校验失败", "snapshot_changed")
    cwd = (tree / cwd_relative).resolve()
    if not cwd.is_dir() or not _within(cwd, tree):
        raise SourceError("构建工作目录不在独立副本内")
    replacements = {"{source_root}": str(tree), "{output_root}": str(target / "output")}

    def expand(value: str) -> str:
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        return value

    extra_env = recipe.get("env", {})
    if not isinstance(extra_env, dict) or any(not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v or "=" in k for k, v in extra_env.items()):
        raise SourceError("构建环境变量必须是字符串字典")
    (target / "output").mkdir()
    command = [expand(arg) for arg in argv]
    stdout, stderr = logs / "stdout.log", logs / "stderr.log"
    build = {"argv": command, "cwd": str(cwd), "started_at": _now(), "stdout_path": str(stdout), "stderr_path": str(stderr), "timed_out": False}
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(command, cwd=cwd, env={**os.environ, **{k: expand(v) for k, v in extra_env.items()}},
                                   stdout=out, stderr=err, start_new_session=os.name != "nt")
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            build["timed_out"] = True
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            code = process.wait()
        except BaseException:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    build.update(exit_code=code, completed_at=_now())
    (target / "build.json").write_text(json.dumps(build, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"build": build, "firmware_bundle": None}
    if code != 0:
        return result
    artifacts = {}
    for pattern in patterns:
        matches = [path for path in tree.glob(pattern) if path.is_file()]
        if not matches:
            raise SourceError("构建结束但缺少指定产物", "missing_artifact", pattern=pattern, build=build)
        for path in matches:
            if not _within(path.resolve(), tree):
                raise SourceError("产物指向构建目录之外", "external_artifact")
            artifacts[path.relative_to(tree).as_posix()] = path
    result["firmware_bundle"] = import_firmware([{"path": str(path), "name": name} for name, path in sorted(artifacts.items())], target / "firmware")
    return result
