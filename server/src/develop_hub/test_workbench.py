from __future__ import annotations

import asyncio
import codecs
import time
from contextvars import ContextVar
import hashlib
import fcntl
import os
import signal
import shutil
from contextlib import contextmanager, nullcontext
from urllib.parse import urlsplit
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from .config import Settings
from .db import SessionLocal
from .models import (
    ControlSequence,
    Repository,
    SdkRepository,
    TestBench,
    TestProduct,
    TestProductStatus,
    TestRound,
    TestRoundEvent,
    TestRoundStatus,
)


class TestWorkbenchError(RuntimeError):
    pass


@contextmanager
def sdk_lease(settings: Settings, sdk_path: str):
    lock_root = settings.artifact_root / 'sdk-locks'
    lock_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(Path(sdk_path).resolve()).encode()).hexdigest()
    with (lock_root / key).open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TestWorkbenchError('SDK is busy with another baseline or Test Round') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def repository_source(repository: Repository, sdk: SdkRepository) -> dict[str, Any]:
    snapshot = await inspect_repository(repository, sdk)
    relative = Path(snapshot['relative_path'])
    if relative == Path('.') or any(part.lower() in {'kernel', 'u-boot', 'uboot', 'rkbin'} or part.lower().startswith('kernel-') for part in relative.parts):
        raise TestWorkbenchError('Kernel, U-Boot and RKBin are fixed SDK baselines, not tested applications')
    root = Path(snapshot['path'])
    if Path(await git_value(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise TestWorkbenchError('Tested Repo must be an exact Git repository root')
    names = (await git_value(root, 'remote')).splitlines()
    remote = next((name for name in ('gerrit', 'origin', 'rk') if name in names), names[0] if len(names) == 1 else None)
    if remote is None:
        raise TestWorkbenchError('No unambiguous Git remote for the tested repository')
    url = await git_value(root, 'remote', 'get-url', remote)
    if '://' in url and urlsplit(url).password:
        raise TestWorkbenchError('Credential-bearing Git URLs cannot be snapshotted')
    snapshot.update(remote=remote, remote_url=url)
    return snapshot


def source_matches_project(source: dict[str, Any], project: str) -> bool:
    url = str(source['remote_url']).rstrip('/').removesuffix('.git')
    path = urlsplit(url).path if '://' in url else url.split(':', 1)[-1]
    return path.lstrip('/') == project or path.endswith('/' + project)


async def verify_patchset(repository: Repository, sdk: SdkRepository, project: str, ref: str, commit: str) -> dict[str, Any]:
    source = await repository_source(repository, sdk)
    if not source_matches_project(source, project):
        raise TestWorkbenchError('Registered Repo remote does not match the locked Gerrit Project')
    result = await run_command(['git', 'ls-remote', source['remote_url'], ref], Path(source['path']), 60)
    entries = [line.split() for line in result['stdout_tail'].splitlines() if line.strip()]
    if result['exit_code'] != 0 or [commit.lower(), ref] not in entries:
        raise TestWorkbenchError('Gerrit Patchset ref/commit is not present in the locked project')
    return source


def now() -> datetime:
    return datetime.now(timezone.utc)


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_command_progress: ContextVar[Any] = ContextVar('command_progress', default=None)


def baseline_progress(product_id: str, stage: str, message: str, chunk: str = '', command: str | None = None) -> None:
    with SessionLocal() as db:
        product = db.get(TestProduct, product_id)
        if not product:
            return
        result = dict(product.baseline_result or {})
        progress = dict(result.get('progress') or {})
        timestamp = now().isoformat()
        progress.setdefault('started_at', timestamp)
        progress.update(stage=stage, message=message, updated_at=timestamp)
        progress['elapsed_seconds'] = max(0, int((now() - datetime.fromisoformat(progress['started_at'])).total_seconds()))
        if command:
            progress['command'] = command
        if chunk:
            # Strip terminal control sequences; keep a bounded live tail in the database.
            chunk = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', chunk).replace('\r', '\n')
            progress['logs'] = (progress.get('logs', '') + chunk)[-24000:]
            progress['last_output_at'] = timestamp
        result['progress'] = progress
        product.baseline_result = result
        db.commit()


def round_progress(round_id: str, artifact_root: Path, chunk: str, command: str) -> None:
    with SessionLocal() as db:
        row = db.get(TestRound, round_id)
        # Child monitor tasks inherit ContextVars. Their SSH output is not build output.
        if not row or row.status not in {TestRoundStatus.CHECKING_OUT, TestRoundStatus.BUILDING}:
            return
        clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', chunk).replace('\r', '\n')
        folder = artifact_root / 'test-rounds' / round_id
        folder.mkdir(parents=True, exist_ok=True)
        if clean:
            with (folder / 'build.log').open('a', encoding='utf-8') as out:
                out.write(clean)
        result = dict(row.build_result or {})
        progress = dict(result.get('progress') or {})
        progress.update(command=command, updated_at=now().isoformat(), logs=(progress.get('logs','')+clean)[-24000:])
        result['progress'] = progress
        row.build_result = result
        db.commit()


async def run_command(command: list[str], cwd: Path, timeout: int, input_text: str | None = None) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *command, cwd=cwd, start_new_session=True,
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    tails = {'stdout': '', 'stderr': ''}
    line_count = 0
    pending: list[str] = []
    sink = _command_progress.get()
    async def read(stream, name):
        nonlocal line_count
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while True:
            data = await stream.read(4096)
            text = decoder.decode(data, final=not data)
            tails[name] = (tails[name] + text)[-12000:]
            if name == 'stdout':
                line_count += text.count('\n')
            if sink and text:
                pending.append(text)
            if not data:
                break
    async def flush():
        while True:
            if sink:
                sink(''.join(pending), shlex.join(command))
                pending.clear()
            await asyncio.sleep(0.5)
    readers = [asyncio.create_task(read(process.stdout, 'stdout')), asyncio.create_task(read(process.stderr, 'stderr'))]
    reporter = asyncio.create_task(flush())
    try:
        if process.stdin:
            process.stdin.write(input_text.encode())
            await process.stdin.drain()
            process.stdin.close()
        await asyncio.wait_for(asyncio.gather(process.wait(), *readers), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise TestWorkbenchError(f'Command timed out after {timeout}s: {shlex.join(command)}') from exc
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, *readers, return_exceptions=True)
        if sink:
            sink(''.join(pending), shlex.join(command))
    return {'command': shlex.join(command), 'cwd': str(cwd), 'exit_code': process.returncode,
            'stdout_tail': tails['stdout'], 'stdout_line_count': line_count, 'stderr_tail': tails['stderr']}


async def board_config_choices(sdk: SdkRepository) -> list[dict[str, Any]]:
    """Read menu entries in the exact C-locale order used for execution; never run lunch to discover them."""
    root = Path(sdk.root_path).resolve()
    hook = root / 'device/rockchip/common/build-hooks/00-config.sh'
    if hook.is_file():
        directory = (root / 'device/rockchip/.chip').resolve()
        if not directory.is_dir() or not inside(directory, root):
            raise TestWorkbenchError('SDK chip directory is unavailable; configure the SDK chip first')
        listing = await run_command(['bash', '-c', 'export LC_ALL=C; ls rockchip_defconfig 2>/dev/null || true; ls *_defconfig 2>/dev/null | grep -v rockchip_defconfig || true'], directory, 20)
        names = listing['stdout_tail'].splitlines()
        first = 1
        link = root / 'output/defconfig'
    elif 'RK_TARGET_BOARD_ARRAY' in (root / 'build.sh').read_text(errors='replace'):
        directory = root / 'project/cfg'
        listing = await run_command(['bash', '-c', 'export LC_ALL=C; ls BoardConfig*.mk BoardConfig_*/BoardConfig*.mk 2>/dev/null | sort'], directory, 20)
        names = listing['stdout_tail'].splitlines()
        first = 0
        link = root / '.BoardConfig.mk'
    else:
        raise TestWorkbenchError('This SDK lunch menu adapter is not supported')
    choices = []
    for registered in sdk.board_configs:
        matches = [(index + first, name) for index, name in enumerate(names) if name == registered or Path(name).name == registered]
        if len(matches) != 1:
            raise TestWorkbenchError(f'Board Config missing or ambiguous in SDK menu: {registered}')
        index, name = matches[0]
        path = (directory / name).resolve()
        if not path.is_file() or not inside(path, root):
            raise TestWorkbenchError('Board Config resolves outside the SDK or is missing')
        choices.append({'board_config': registered, 'menu_index': index, 'command': './build.sh lunch',
                        'input': str(index), 'display_command': f'./build.sh lunch → 输入 {index}',
                        'config_path': str(path), 'selection_link': str(link)})
    return choices


async def configure_board(sdk: SdkRepository, board: str, timeout: int) -> dict[str, Any]:
    choice = next((item for item in await board_config_choices(sdk) if item['board_config'] == board), None)
    if choice is None:
        raise TestWorkbenchError('Board Config is not registered for this SDK')
    result = await run_command(['env', 'LC_ALL=C', './build.sh', 'lunch'], Path(sdk.root_path), timeout, input_text=choice['input'] + '\n')
    result['board_selection'] = choice
    result['selection_verified'] = Path(choice['selection_link']).is_symlink() and Path(choice['selection_link']).resolve() == Path(choice['config_path'])
    if result['exit_code'] == 0 and not result['selection_verified']:
        result['exit_code'] = 1
        result['selection_error'] = 'lunch exited successfully but the selected Board Config does not match'
    return result


async def git_value(root: Path, *args: str) -> str:
    result = await run_command(["git", *args], root, 30)
    if result["exit_code"] != 0:
        raise TestWorkbenchError(result["stderr_tail"] or f"git {' '.join(args)} failed")
    return str(result["stdout_tail"]).strip()


async def inspect_manifest(sdk: SdkRepository) -> dict[str, Any]:
    root = Path(sdk.root_path).resolve()
    manifest_link = root / ".repo" / "manifest.xml"
    manifests = root / ".repo" / "manifests"
    if not root.is_dir() or not (root / "build.sh").is_file():
        raise TestWorkbenchError(f"SDK root must contain build.sh: {root}")
    if not manifest_link.exists() or not manifests.is_dir():
        raise TestWorkbenchError(f"SDK root does not contain a repo manifest checkout: {root}")
    manifest_path = manifest_link.resolve()
    if not inside(manifest_path, root / ".repo"):
        raise TestWorkbenchError("Manifest file resolves outside the SDK .repo directory")
    projects: list[ElementTree.Element] = []
    seen_manifests: set[Path] = set()

    def collect_projects(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen_manifests:
            return
        if not inside(resolved, root / ".repo"):
            raise TestWorkbenchError("Included manifest resolves outside the SDK .repo directory")
        seen_manifests.add(resolved)
        try:
            tree = ElementTree.parse(resolved)
        except ElementTree.ParseError as exc:
            raise TestWorkbenchError(f"Invalid manifest XML: {exc}") from exc
        projects.extend(tree.findall(".//project"))
        for include in tree.findall(".//include"):
            name = include.get("name")
            if name:
                collect_projects(manifests / name)

    collect_projects(manifest_path)
    project_heads: dict[str, str] = {}
    missing_projects: list[str] = []
    for project in projects:
        relative = project.get('path') or project.get('name')
        if not relative:
            continue
        directory = (root / relative).resolve()
        if not inside(directory, root) or not (directory / '.git').exists():
            missing_projects.append(relative)
            continue
        project_heads[relative] = await git_value(directory, 'rev-parse', 'HEAD')
    changes: list[str] = []
    manifest_status = await run_command(["git", "status", "--porcelain"], manifests, 20)
    if manifest_status["exit_code"] != 0:
        changes.append(".repo/manifests: status unavailable")
    elif str(manifest_status["stdout_tail"]).strip():
        changes.append(".repo/manifests")
    repo_list = await run_command(["repo", "list"], root, 120)
    repo_status = await run_command(
        [
            "repo", "forall", "-c",
            'if test -n "$(git status --porcelain)"; then printf "%s\\n" "$REPO_PATH"; fi',
        ],
        root,
        120,
    )
    if repo_list["exit_code"] == 0:
        project_count = repo_list["stdout_line_count"]
    else:
        project_count = len(projects)
    if repo_status["exit_code"] == 0:
        changes.extend(line.strip() for line in str(repo_status["stdout_tail"]).splitlines() if line.strip())
    else:
        for project in projects:
            project_relpath = project.get("path") or project.get("name")
            if not project_relpath:
                continue
            project_root = (root / project_relpath).resolve()
            if not inside(project_root, root) or not (project_root / ".git").exists():
                continue
            status = await run_command(["git", "status", "--porcelain"], project_root, 20)
            if status["exit_code"] != 0:
                changes.append(f"{project_relpath}: status unavailable")
            elif str(status["stdout_tail"]).strip():
                changes.append(project_relpath)
    branch = await git_value(manifests, "branch", "--show-current")
    commit = await git_value(manifests, "rev-parse", "HEAD")
    tracking = await run_command(["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"], manifests, 20)
    ahead = behind = 0
    if tracking["exit_code"] == 0:
        try:
            ahead, behind = (int(item) for item in str(tracking["stdout_tail"]).split())
        except (TypeError, ValueError):
            ahead = behind = 0
    if missing_projects:
        sync_status = "incomplete"
    elif changes:
        sync_status = "local_changes"
    elif ahead and behind:
        sync_status = f"diverged ({ahead} ahead / {behind} behind)"
    elif behind:
        sync_status = f"behind {behind}"
    elif ahead:
        sync_status = f"ahead {ahead}"
    else:
        sync_status = "local_clean" if tracking["exit_code"] == 0 else "tracking_unknown"
    return {
        "sdk_path": str(root),
        "manifest_file": str(manifest_path.relative_to(root)),
        "manifest_branch": branch or "detached",
        "manifest_commit": commit,
        "sync_status": sync_status,
        "project_count": project_count,
        "local_changes": list(dict.fromkeys(changes)),
        "inspected_at": now().isoformat(),
        "project_heads": project_heads,
        "project_names": {item.get("path") or item.get("name"): item.get("name") for item in projects},
        "missing_projects": missing_projects,
    }


async def inspect_repository(repository: Repository, sdk: SdkRepository) -> dict[str, Any]:
    root = Path(repository.root_path).resolve()
    sdk_root = Path(sdk.root_path).resolve()
    if not root.is_dir() or not inside(root, sdk_root):
        raise TestWorkbenchError("The tested repository must be a registered Git repository inside the selected SDK")
    branch = await git_value(root, "branch", "--show-current")
    head = await git_value(root, "rev-parse", "HEAD")
    porcelain = await git_value(root, "status", "--porcelain")
    tracked = await git_value(root, "status", "--porcelain", "--untracked-files=no")
    untracked = [line[3:] for line in porcelain.splitlines() if line.startswith('?? ')]
    return {
        "path": str(root),
        "relative_path": str(root.relative_to(sdk_root)),
        "branch": branch or "detached",
        "head": head,
        "git_status": "dirty" if tracked else "untracked_only" if untracked else "clean",
        "tracked_changes": tracked.splitlines(),
        "untracked_files": untracked,
        "tracked_clean": not bool(tracked),
        "local_changes": porcelain.splitlines(),
    }


def validate_sdk_paths(root_path: str, artifact_root: str) -> tuple[Path, Path]:
    root = Path(root_path).expanduser().resolve()
    artifacts = Path(artifact_root).expanduser().resolve()
    if not root.is_dir() or not (root / "build.sh").is_file():
        raise TestWorkbenchError(f"SDK root must be an existing directory containing build.sh: {root}")
    if not inside(artifacts, root):
        raise TestWorkbenchError("Artifact root must stay inside the registered SDK root")
    return root, artifacts


def discover_sdk_registration(
    root_path: str,
    board_config_path: str = "project/cfg",
    board_config_pattern: str = "BoardConfig*.mk",
) -> dict[str, Any]:
    """Inspect a local SDK before it is admitted to the QA registry.

    This is deliberately server-side: the browser only presents the discovered
    choices and never decides whether a path is a valid SDK workspace.
    """

    root = Path(root_path).expanduser().resolve()
    if not root.is_dir() or not (root / "build.sh").is_file():
        raise TestWorkbenchError(f"SDK root must be an existing directory containing build.sh: {root}")

    requested_config_root = Path(board_config_path).expanduser()
    config_root = (
        requested_config_root if requested_config_root.is_absolute() else root / requested_config_root
    ).resolve()
    if not inside(config_root, root):
        raise TestWorkbenchError("Board Config directory must stay inside the SDK root")
    if not config_root.is_dir():
        raise TestWorkbenchError(f"Board Config directory does not exist: {config_root}")
    pattern_path = Path(board_config_pattern)
    if pattern_path.name != board_config_pattern or board_config_pattern in {".", ".."}:
        raise TestWorkbenchError("Board Config pattern must be a filename pattern without directory traversal")
    board_configs = sorted(
        {
            path.name
            for path in config_root.rglob(board_config_pattern)
            if path.is_file() and inside(path.resolve(), root)
        }
    )
    if not board_configs:
        raise TestWorkbenchError(
            f"No files matching {board_config_pattern!r} were found under: {config_root}"
        )

    expected_roots = [
        root / "output" / "image",
        root / "output" / "firmware",
        root / "rockdev",
        root / "images",
    ]
    discovered_roots: list[Path] = [path for path in expected_roots if path.is_dir()]
    for scan_root in (root / "output", root / "rockdev"):
        if not scan_root.is_dir():
            continue
        for image in scan_root.rglob("*.img"):
            parent = image.parent.resolve()
            if inside(parent, root) and parent not in discovered_roots:
                discovered_roots.append(parent)
    if not discovered_roots:
        # The directory may be created by the first build. Keep the conventional
        # location selectable while validate_sdk_paths still enforces SDK scope.
        discovered_roots.append(root / "output" / "image")

    return {
        "root_path": str(root),
        "suggested_name": root.name,
        "board_config_path": str(config_root),
        "board_config_pattern": board_config_pattern,
        "board_configs": board_configs,
        "artifact_roots": [str(path) for path in discovered_roots],
    }


def validate_sdk_manifest_checkout(root: Path) -> None:
    """Perform the fast structural manifest check required at registration.

    Project status inspection is intentionally deferred to product baseline
    verification because `repo forall` can take close to a minute on a full SDK.
    """

    manifest_link = root / ".repo" / "manifest.xml"
    manifests = root / ".repo" / "manifests"
    if not manifest_link.exists() or not manifests.is_dir():
        raise TestWorkbenchError(f"SDK root does not contain a repo manifest checkout: {root}")
    manifest_path = manifest_link.resolve()
    if not inside(manifest_path, root / ".repo"):
        raise TestWorkbenchError("Manifest file resolves outside the SDK .repo directory")
    try:
        ElementTree.parse(manifest_path)
    except ElementTree.ParseError as exc:
        raise TestWorkbenchError(f"Invalid manifest XML: {exc}") from exc


def build_command(board_config: str) -> list[str]:
    # IPC SDK defaults to allsave, which creates a release archive per invocation.
    return ['./build.sh', 'all'] if 'rv1126b' in board_config.lower() else ['./build.sh']


async def verify_product_baseline(product_id: str, settings: Settings, *, lease_held: bool = False) -> None:
    with SessionLocal() as db:
        product = db.get(TestProduct, product_id)
        if not product:
            raise TestWorkbenchError('Test product not found')
        sdk_path = product.sdk_repository.root_path
    with (nullcontext() if lease_held else sdk_lease(settings, sdk_path)):
        stage = ['inspecting', '正在检查 Manifest 和被测 Repo']
        token = _command_progress.set(lambda chunk, command: baseline_progress(product_id, stage[0], stage[1], chunk, command))
        try:
            await _verify_product_baseline(product_id, settings, stage)
        finally:
            _command_progress.reset(token)


async def _verify_product_baseline(product_id: str, settings: Settings, stage: list[str]) -> None:
    with SessionLocal() as db:
        product = db.scalar(
            select(TestProduct)
            .options(selectinload(TestProduct.sdk_repository), selectinload(TestProduct.tested_repository))
            .where(TestProduct.id == product_id)
        )
        if not product:
            raise TestWorkbenchError("Test product not found")
        product.status = TestProductStatus.VERIFYING
        product.baseline_result = {"passed": False, "progress": {"stage": "inspecting", "message": "正在检查 Manifest 和被测 Repo", "started_at": now().isoformat(), "logs": ""}}
        db.commit()
        sdk = product.sdk_repository
        board_config = product.board_config
    try:
        manifest = await inspect_manifest(sdk)
        repository = await repository_source(product.tested_repository, sdk)
        if not source_matches_project(repository, product.gerrit_project):
            raise TestWorkbenchError("Application remote no longer matches the product")
        if manifest.get("missing_projects"):
            raise TestWorkbenchError("SDK has missing projects")
        if not repository["tracked_clean"]:
            raise TestWorkbenchError("Tested repository contains tracked modifications or staged changes")
        started = now()
        previous_artifacts = TestRoundWorkflow._artifact_versions(sdk)
        stage[:] = ['configuring', '正在选择 Board Config']
        baseline_progress(product_id, *stage)
        lunch = await configure_board(sdk, board_config, settings.validation_timeout_seconds)
        build = None
        if lunch['exit_code'] == 0:
            stage[:] = ['building', '正在编译固件']
            baseline_progress(product_id, *stage)
            build = await run_command(build_command(board_config), Path(sdk.root_path), settings.validation_timeout_seconds)
        artifacts: list[dict[str, Any]] = []
        artifact_error: str | None = None
        if lunch["exit_code"] == 0 and build is not None and build["exit_code"] == 0:
            try:
                stage[:] = ['validating', '正在校验镜像大小和 SHA-256']
                baseline_progress(product_id, *stage)
                artifacts = await asyncio.to_thread(TestRoundWorkflow._collect_artifacts, sdk, previous_artifacts)
            except TestWorkbenchError as exc:
                artifact_error = str(exc)
        passed = lunch["exit_code"] == 0 and build is not None and build["exit_code"] == 0 and bool(artifacts)
        result = {
            "passed": passed,
            "started_at": started.isoformat(),
            "finished_at": now().isoformat(),
            "configuration": lunch,
            "build": build,
            "artifacts": artifacts,
            "artifact_error": artifact_error,
        }
        with SessionLocal() as db:
            current = db.get(TestProduct, product_id)
            assert current
            current.manifest_snapshot = manifest
            current.repository_snapshot = repository
            result['progress'] = (current.baseline_result or {}).get('progress', {})
            result['progress'].update(stage='completed' if passed else 'failed', message='基线验证通过' if passed else '基线验证失败', updated_at=now().isoformat())
            current.baseline_result = result
            current.status = TestProductStatus.READY if passed else TestProductStatus.INVALID
            db.commit()
        if not passed:
            raise TestWorkbenchError("Baseline build failed according to command exit codes")
    except Exception as exc:
        with SessionLocal() as db:
            current = db.get(TestProduct, product_id)
            if current:
                current.status = TestProductStatus.INVALID
                result = dict(current.baseline_result or {})
                result["passed"] = False
                result["error"] = str(exc)
                result['progress'] = {**result.get('progress', {}), 'stage': 'failed', 'message': str(exc), 'updated_at': now().isoformat()}
                current.baseline_result = result
                db.commit()
        raise


class TestRoundWorkflow:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._bench_tasks: dict[str, asyncio.Task] = {}
        self._baseline_tasks: dict[str, asyncio.Task] = {}

    async def start_baseline(self, product_id: str) -> None:
        with SessionLocal() as db:
            product = db.get(TestProduct, product_id)
            if not product:
                raise TestWorkbenchError('Test product not found')
            # Reserve the SDK before replacing the last valid baseline result.
            # A busy SDK is a rejected request, not an invalid product baseline.
            lease = sdk_lease(self.settings, product.sdk_repository.root_path)
            lease.__enter__()
            try:
                claimed = db.execute(update(TestProduct).where(TestProduct.id == product_id, TestProduct.status != TestProductStatus.VERIFYING).values(
                    status=TestProductStatus.VERIFYING,
                    baseline_result={'passed': False, 'progress': {'stage': 'queued', 'message': '已提交，正在准备检查', 'started_at': now().isoformat(), 'logs': ''}},
                ))
                if claimed.rowcount != 1:
                    raise TestWorkbenchError('Baseline verification is already running')
                db.commit()
            except BaseException:
                lease.__exit__(None, None, None)
                raise
        async def work():
            try:
                await verify_product_baseline(product_id, self.settings, lease_held=True)
            except Exception as exc:
                baseline_progress(product_id, 'failed', str(exc))
                with SessionLocal() as db:
                    product = db.get(TestProduct, product_id)
                    product.status = TestProductStatus.INVALID
                    result = dict(product.baseline_result or {})
                    result.update(passed=False, error=str(exc))
                    product.baseline_result = result
                    db.commit()
        task = asyncio.create_task(work(), name=f'baseline-{product_id}')
        self._baseline_tasks[product_id] = task
        def finished(done):
            lease.__exit__(None, None, None)
            self._baseline_tasks.pop(product_id, None)
        task.add_done_callback(finished)

    async def recover_interrupted_rounds(self) -> None:
        with SessionLocal() as db:
            for product in db.scalars(select(TestProduct).where(TestProduct.status == TestProductStatus.VERIFYING)):
                product.status = TestProductStatus.INVALID
                result = dict(product.baseline_result or {})
                result.update(passed=False, error='服务重启，基线构建已中断，请重新验证')
                result['progress'] = {**result.get('progress', {}), 'stage': 'interrupted', 'message': result['error']}
                product.baseline_result = result
            db.commit()
        transient = {
            TestRoundStatus.CHECKING_OUT,
            TestRoundStatus.BUILDING,
            TestRoundStatus.PACKAGING,
            TestRoundStatus.TRANSFERRING,
            TestRoundStatus.VERIFYING,
        }
        with SessionLocal() as db:
            rounds = list(db.scalars(select(TestRound).where(TestRound.status.in_(transient))))
            for test_round in rounds:
                test_round.status = TestRoundStatus.BLOCKED
                test_round.completed_at = now()
                test_round.last_error = "175 Server restarted during a controlled test-round step; inspect history before retrying."
                self._event(db, test_round, "recovery", "blocked", test_round.last_error)
            db.commit()
        with SessionLocal() as db:
            pending = list(db.scalars(select(TestRound.id).where(TestRound.status == TestRoundStatus.BENCH_RUNNING)))
        for round_id in pending:
            try:
                await self._dispatch_bench(round_id)
            except Exception:
                self.monitor_bench(round_id)

    def monitor_bench(self, round_id: str) -> None:
        task = self._bench_tasks.get(round_id)
        if task and not task.done():
            return
        task = asyncio.create_task(self._monitor_bench(round_id))
        self._bench_tasks[round_id] = task
        task.add_done_callback(lambda done: self._bench_tasks.pop(round_id, None))

    async def _monitor_bench(self, round_id: str) -> None:
        while True:
            try:
                with SessionLocal() as db:
                    current = db.get(TestRound, round_id)
                    if not current or current.status != TestRoundStatus.BENCH_RUNNING:
                        return
                    bench = TestBench(**current.bench_snapshot)
                    path = current.remote_path
                response = await self._remote(bench, 'cat -- ' + shlex.quote(path + '/result.json'), 20)
                if response['exit_code'] == 0:
                    report = json.loads(response['stdout_tail'])
                    with SessionLocal() as db:
                        current = db.get(TestRound, round_id)
                        expected = f'TEST-{current.display_number:06d}'
                        expected_digest = next(item['sha256'] for item in current.delivery_result['files'] if item['filename']=='test-run.json')
                        if report.get('round_id') != expected or report.get('config_sha256') != expected_digest:
                            raise TestWorkbenchError('Controller result identity does not match delivered configuration')
                        previous = current.controller_result or {}
                        if report.get('revision',0) < previous.get('revision',0):
                            raise TestWorkbenchError('Controller result revision regressed')
                        current.controller_result = {**report, 'received_at': now().isoformat()}
                        if report.get('state') in {'finished','rejected','interrupted'}:
                            if any(c.get('code','').startswith('SWITCH-') for c in current.test_case_snapshot):
                                from .evidence_store import archive
                                try:
                                    await archive(self,bench,path,current.display_number)
                                    current.controller_result={**current.controller_result,'evidence_archived':True}
                                except Exception as exc:
                                    current.controller_result={**current.controller_result,'evidence_archived':False,'evidence_archive_error':str(exc)}
                            passed = report.get('state')=='finished' and report.get('outcome')=='passed' and report.get('phase') in {'flash_complete','tests_complete'} and report.get('flash_exit_code')==0
                            if current.product_snapshot.get('execute_test_cases'):
                                from .basic_cases import report_passed
                                if current.product_snapshot.get('developer_mode'):
                                    from .developer_tests import passed as quick_passed
                                    passed = quick_passed(report, [case['id'] for case in current.test_case_snapshot])
                                else:
                                    passed = report_passed(report, [case['id'] for case in current.test_case_snapshot])
                            current.status = (TestRoundStatus.TEST_COMPLETE if passed else TestRoundStatus.TEST_FAILED) if current.product_snapshot.get('execute_test_cases') else (TestRoundStatus.FLASH_COMPLETE if passed else TestRoundStatus.BENCH_FAILED)
                            current.completed_at = now()
                            if not passed: current.last_error = report.get('message','Controller reported failure')
                            self._event(db,current,'bench_result','succeeded' if passed else 'failed', report.get('message','Bench finished'), report)
                        elif previous.get('phase') != report.get('phase'):
                            self._event(db,current,'bench_'+report.get('phase','unknown'),'running',report.get('message','Bench running'))
                        db.commit()
                else:
                    raise TestWorkbenchError('等待 Controller 状态文件或 SSH 连接恢复')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with SessionLocal() as db:
                    current = db.get(TestRound, round_id)
                    if current:
                        current.controller_result = {**(current.controller_result or {}), 'transport_error': str(exc), 'checked_at': now().isoformat()}
                        db.commit()
            await asyncio.sleep(2)

    async def _dispatch_bench(self, round_id: str) -> None:
        with SessionLocal() as db:
            current = db.get(TestRound, round_id)
            bench = TestBench(**current.bench_snapshot)
            remote = current.remote_path
            digest = next(item['sha256'] for item in current.delivery_result['files'] if item['filename']=='test-run.json')
            current.status = TestRoundStatus.BENCH_RUNNING
            current.completed_at = None
            self._event(db,current,'bench_queued','running','任务已交付，等待测试台 Controller 接收')
            db.commit()
        marker = json.dumps({'config_sha256': digest})
        command = 'set -eu; test -f ' + shlex.quote(remote+'/READY') + ' || (printf %s ' + shlex.quote(marker) + ' > ' + shlex.quote(remote+'/READY.tmp') + '; mv ' + shlex.quote(remote+'/READY.tmp') + ' ' + shlex.quote(remote+'/READY') + ')'
        result = await self._remote(bench,command)
        if result['exit_code'] != 0:
            raise TestWorkbenchError('Failed to publish Controller READY marker')
        self.monitor_bench(round_id)

    async def start(self, round_id: str) -> None:
        async with self._lock:
            task = self._tasks.get(round_id)
            if task and not task.done():
                raise TestWorkbenchError("This Test Round already has an active task")
            with SessionLocal() as db:
                claimed = db.execute(update(TestRound).where(TestRound.id == round_id, TestRound.status == TestRoundStatus.CREATED).values(status=TestRoundStatus.CHECKING_OUT, started_at=now()))
                if claimed.rowcount != 1:
                    raise TestWorkbenchError('Only a newly created Test Round may start; create a new round to retry')
                db.commit()
            with SessionLocal() as db:
                quick = db.get(TestRound, round_id).product_snapshot.get("developer_mode")
            from .developer_tests import dispatch
            task = asyncio.create_task(dispatch(self,round_id) if quick else self._pipeline(round_id), name=f"test-round-{round_id}")
            self._tasks[round_id] = task
            task.add_done_callback(lambda _task: self._tasks.pop(round_id, None))

    @staticmethod
    def _event(
        db: Any,
        test_round: TestRound,
        event_type: str,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        # Advancing a stage only happens after its server-side checks pass.
        # Terminal failures close the active stage without claiming success.
        if event_type != "restore_checkout":
            for previous in db.scalars(select(TestRoundEvent).where(
                TestRoundEvent.test_round_id == test_round.id, TestRoundEvent.status == "running"
            )):
                previous.status = "succeeded" if status in {"running", "succeeded"} else status
                previous.details = {**(previous.details or {}), "finished_at": now().isoformat()}
        db.flush()
        sequence = (db.scalar(select(func.max(TestRoundEvent.sequence)).where(TestRoundEvent.test_round_id == test_round.id)) or 0) + 1
        db.add(TestRoundEvent(
            test_round_id=test_round.id,
            sequence=sequence,
            event_type=event_type,
            status=status,
            message=message,
            details=details or {},
        ))

    def _set_status(self, round_id: str, status: TestRoundStatus, message: str, details: dict[str, Any] | None = None) -> None:
        with SessionLocal() as db:
            test_round = db.get(TestRound, round_id)
            assert test_round
            test_round.status = status
            self._event(db, test_round, status.value, "running", message, details)
            db.commit()

    async def _pipeline(self, round_id: str) -> None:
        with SessionLocal() as db:
            test_round = db.get(TestRound, round_id)
            if not test_round:
                return
            sdk_path = test_round.product_snapshot.get('sdk_path', '')
        try:
            with sdk_lease(self.settings, sdk_path):
                token = _command_progress.set(lambda chunk, command: round_progress(round_id, self.settings.artifact_root, chunk, command))
                try:
                    await self._pipeline_locked(round_id)
                finally:
                    _command_progress.reset(token)
        except TestWorkbenchError as exc:
            with SessionLocal() as db:
                current = db.get(TestRound, round_id)
                current.status = TestRoundStatus.BLOCKED
                current.last_error = str(exc)
                current.completed_at = now()
                self._event(db, current, 'sdk_lease', 'blocked', str(exc))
                db.commit()

    async def _pipeline_locked(self, round_id: str) -> None:
        original_ref = None
        repo_root = None
        try:
            with SessionLocal() as db:
                test_round = db.scalar(
                    select(TestRound)
                    .options(
                        selectinload(TestRound.product).selectinload(TestProduct.sdk_repository),
                        selectinload(TestRound.product).selectinload(TestProduct.tested_repository),
                        selectinload(TestRound.test_point),
                        selectinload(TestRound.bench),
                    )
                    .where(TestRound.id == round_id)
                )
                if not test_round or test_round.status != TestRoundStatus.CHECKING_OUT:
                    raise TestWorkbenchError("Test Round is not ready to start")
                test_round.started_at = now()
                product = test_round.product
                source_ref = f"refs/changes/{test_round.change_number % 100:02d}/{test_round.change_number}/{test_round.patchset_number}"
                sdk = product.sdk_repository
                repository = product.tested_repository
                if not test_round.bench_snapshot:
                    raise TestWorkbenchError('Historical round lacks a bench snapshot; create a new round')
                bench = TestBench(**test_round.bench_snapshot)
                frozen_sdk = test_round.product_snapshot.get('sdk_configuration')
                if not frozen_sdk:
                    raise TestWorkbenchError('Round lacks a fixed SDK configuration')
                sdk = SdkRepository(**frozen_sdk)
                db.commit()

            repo_root = Path(test_round.product_snapshot['tested_repository_path']).resolve()
            repository = Repository(root_path=str(repo_root))
            sdk_root = Path(sdk.root_path).resolve()
            if not inside(repo_root, sdk_root):
                raise TestWorkbenchError("Locked tested repository is outside the product SDK")
            self._set_status(round_id, TestRoundStatus.CHECKING_OUT, "Fetching and checking out the locked Gerrit Patchset")
            manifest = await inspect_manifest(sdk)
            baseline = test_round.product_snapshot['manifest']
            if manifest['manifest_commit'] != test_round.manifest_commit or manifest['manifest_file'] != baseline['manifest_file'] or manifest.get('missing_projects'):
                raise TestWorkbenchError('SDK Manifest baseline changed or projects are missing')
            source = await verify_patchset(repository, sdk, test_round.gerrit_project, source_ref, test_round.commit_sha)
            if source.get('remote_url') != test_round.product_snapshot['repository'].get('remote_url'):
                raise TestWorkbenchError('Application Git remote changed after baseline verification')
            if not source['tracked_clean']:
                raise TestWorkbenchError('Tested repository contains tracked modifications or staged changes')
            for path, head in baseline.get('project_heads', {}).items():
                if path != source['relative_path'] and manifest.get('project_heads', {}).get(path) != head:
                    raise TestWorkbenchError(f'Fixed SDK project changed: {path}')
            original_ref = await git_value(repo_root, 'symbolic-ref', '--quiet', '--short', 'HEAD') if source['branch'] != 'detached' else source['head']
            fetch = await run_command(['git', 'fetch', source['remote_url'], source_ref], repo_root, 300)
            if fetch['exit_code'] != 0:
                raise TestWorkbenchError('Gerrit Patchset fetch failed')
            fetched = await git_value(repo_root, 'rev-parse', 'FETCH_HEAD')
            if fetched.lower() != test_round.commit_sha.lower():
                raise TestWorkbenchError('Fetched Patchset does not match the locked commit')
            checkout = await run_command(['git', 'checkout', '-b', test_round.work_branch, fetched], repo_root, 60)
            if checkout['exit_code'] != 0:
                raise TestWorkbenchError(checkout['stderr_tail'] or 'Patchset checkout failed')
            if await git_value(repo_root, 'rev-parse', 'HEAD') != fetched:
                raise TestWorkbenchError('Checked out HEAD does not match the locked commit')

            self._set_status(round_id, TestRoundStatus.BUILDING, "Running the server-controlled SDK build")
            build_started = now()
            previous_artifacts = self._artifact_versions(sdk)
            lunch = await configure_board(sdk, test_round.board_config, self.settings.validation_timeout_seconds)
            application_rebuild = None
            if lunch['exit_code'] == 0 and test_round.gerrit_project == 'linux/app/struct_light' and 'rk3572' in test_round.board_config.lower():
                application_rebuild = await run_command(['./build.sh', 'bmake:struct_light-reconfigure'], sdk_root, self.settings.validation_timeout_seconds)
                identity_file = sdk_root / 'buildroot/output/rootfs_latest/build/struct_light/git_build_id.txt'
                identity = identity_file.read_text().strip() if identity_file.is_file() else ''
                application_rebuild['application_build_id'] = identity
                application_rebuild['expected_commit'] = fetched
                if application_rebuild['exit_code'] == 0 and fetched[:7] not in identity:
                    application_rebuild['exit_code'] = 1
                    application_rebuild['stderr_tail'] = 'Rebuilt application identity does not match locked Patchset: ' + identity
            prerequisite = application_rebuild or lunch
            build = await run_command(build_command(test_round.board_config), sdk_root, self.settings.validation_timeout_seconds) if prerequisite['exit_code'] == 0 else dict(prerequisite)
            if build['exit_code'] == 0 and 'rv1126b' in test_round.board_config.lower():
                identity_file = repo_root / 'build/git_build_id.txt'
                identity = identity_file.read_text().strip() if identity_file.is_file() else ''
                build['application_build_id'] = identity
                build['expected_commit'] = fetched
                if fetched[:7] not in identity:
                    build['exit_code'] = 1
                    build['stderr_tail'] = 'Built application identity does not match locked Patchset: ' + identity
            build['application_rebuild'] = application_rebuild
            build['configuration'] = lunch
            build["started_at"] = build_started.isoformat()
            build["finished_at"] = now().isoformat()
            build["command_passed"] = build["exit_code"] == 0
            build["artifact_validation_passed"] = False
            build["passed"] = False
            test_round.build_result = build
            with SessionLocal() as db:
                current = db.get(TestRound, round_id)
                assert current
                build['progress'] = (current.build_result or {}).get('progress', {})
                current.build_result = build
                db.commit()
            if build["exit_code"] != 0:
                with SessionLocal() as db:
                    current = db.get(TestRound, round_id)
                    assert current
                    current.completed_at = now()
                    current.status = TestRoundStatus.BUILD_FAILED
                    current.last_error = "Build command returned a non-zero exit code"
                    self._event(db, current, "build", "failed", current.last_error, build)
                    db.commit()
                return

            self._set_status(round_id, TestRoundStatus.PACKAGING, "Validating refreshed images and generating test-run.json")
            try:
                artifacts = self._collect_artifacts(sdk, previous_artifacts)
            except TestWorkbenchError as exc:
                build["artifact_error"] = str(exc)
                with SessionLocal() as db:
                    current = db.get(TestRound, round_id)
                    assert current
                    current.completed_at = now()
                    current.status = TestRoundStatus.BUILD_FAILED
                    current.last_error = str(exc)
                    current.build_result = build
                    self._event(db, current, "artifact_validation", "failed", str(exc), build)
                    db.commit()
                return
            build["artifact_validation_passed"] = True
            build["passed"] = True
            test_round.build_result = build
            artifact_dir = self.settings.artifact_root / "test-rounds" / round_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            frozen_images = artifact_dir / 'firmware'
            frozen_images.mkdir(exist_ok=True)
            for item in artifacts:
                destination = frozen_images / item['filename']
                shutil.copy2(item['local_path'], destination)
                if sha256_file(destination) != item['sha256']:
                    raise TestWorkbenchError('Image changed while freezing build artifacts')
                item['local_path'] = str(destination)
            config = self._config(test_round, artifacts, bench)
            config_path = artifact_dir / "test-run.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            with SessionLocal() as db:
                current = db.get(TestRound, round_id)
                assert current
                current.image_snapshot = artifacts
                current.build_result = build
                current.generated_config = config
                db.commit()

            self._set_status(round_id, TestRoundStatus.TRANSFERRING, "Transferring JSON and verified images to the selected bench")
            remote_path = await self._transfer(test_round, bench, config_path, artifacts)
            self._set_status(round_id, TestRoundStatus.VERIFYING, "Verifying remote files and SHA-256 digests")
            delivery = await self._verify_remote(bench, remote_path, artifacts, config_path)
            with SessionLocal() as db:
                current = db.get(TestRound, round_id)
                assert current
                current.status = TestRoundStatus.REMOTE_READY
                current.remote_path = remote_path
                current.delivery_result = delivery
                current.completed_at = now()
                self._event(db, current, "remote_ready", "succeeded", "JSON and images passed remote file verification")
                db.commit()
            if test_round.product_snapshot.get('flash', {}).get('enabled'):
                await self._dispatch_bench(round_id)
        except Exception as exc:
            with SessionLocal() as db:
                test_round = db.get(TestRound, round_id)
                if test_round and test_round.status != TestRoundStatus.BUILD_FAILED:
                    test_round.status = TestRoundStatus.BLOCKED
                    test_round.completed_at = now()
                    test_round.last_error = str(exc)
                    self._event(db, test_round, "pipeline", "failed", str(exc))
                    db.commit()

        finally:
            if original_ref is not None and repo_root is not None:
                restored = await run_command(['git', 'checkout', original_ref], repo_root, 60)
                with SessionLocal() as db:
                    current = db.get(TestRound, round_id)
                    self._event(db, current, 'restore_checkout', 'succeeded' if restored['exit_code'] == 0 else 'failed', 'Restore original application checkout', restored)
                    db.commit()

    @staticmethod
    def _artifact_versions(sdk: SdkRepository) -> dict[str, int]:
        root = Path(sdk.artifact_root).resolve()
        versions: dict[str, int] = {}
        if not root.is_dir():
            return versions
        for pattern in sdk.artifact_patterns:
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                continue
            for path in root.glob(pattern):
                if path.is_file():
                    versions[str(path.resolve())] = path.stat().st_mtime_ns
        return versions

    @staticmethod
    def _collect_artifacts(sdk: SdkRepository, previous_versions: dict[str, int]) -> list[dict[str, Any]]:
        root = Path(sdk.artifact_root).resolve()
        sdk_root = Path(sdk.root_path).resolve()
        if not inside(root, sdk_root) or not root.is_dir():
            raise TestWorkbenchError("Configured artifact root is unavailable or outside the SDK")
        files: dict[Path, None] = {}
        for pattern in sdk.artifact_patterns:
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise TestWorkbenchError(f"Unsafe artifact pattern: {pattern}")
            for path in root.glob(pattern):
                resolved = path.resolve()
                if resolved.is_file() and inside(resolved, root):
                    files[path] = None
        if not files:
            raise TestWorkbenchError("Build exited successfully but no configured firmware artifacts were found")
        artifacts = []
        filenames: set[str] = set()
        for path in sorted(files):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.name):
                raise TestWorkbenchError("Artifact filename is unsafe for transfer")
            if path.name in filenames:
                raise TestWorkbenchError(f"Artifact patterns produce duplicate remote filename: {path.name}")
            filenames.add(path.name)
            previous_mtime = previous_versions.get(str(path.resolve()))
            if previous_mtime is not None and path.stat().st_mtime_ns <= previous_mtime:
                raise TestWorkbenchError(f"Artifact was not refreshed by this build: {path.name}")
            if path.stat().st_size == 0:
                raise TestWorkbenchError(f"Empty firmware artifact: {path.name}")
            digest = sha256_file(path)
            artifacts.append({
                "filename": path.name,
                "local_path": str(path),
                "sha256": digest,
                "size_bytes": path.stat().st_size,
                "verified_at": now().isoformat(),
            })
        return artifacts

    @staticmethod
    def _config(test_round: TestRound, artifacts: list[dict[str, Any]], bench: TestBench) -> dict[str, Any]:
        return {
            "schema_version": 2 if test_round.product_snapshot.get("flash", {}).get("enabled") else "1.0",
            "round_id": f"TEST-{test_round.display_number:06d}",
            "product": test_round.product_snapshot,
            "baseline": {
                "manifest_commit": test_round.manifest_commit,
                "board_config": test_round.board_config,
            },
            "source": {
                "type": "gerrit",
                "project": test_round.gerrit_project,
                "change": test_round.change_number,
                "patchset": test_round.patchset_number,
                "commit": test_round.commit_sha,
                **test_round.product_snapshot.get("test_point", {}),
            },
            "build": test_round.build_result,
            "artifacts": [{key: item[key] for key in ("filename", "sha256", "size_bytes")} for item in artifacts],
            "target": test_round.bench_snapshot,
            "test_cases": test_round.test_case_snapshot,
            "flash": test_round.product_snapshot.get("flash", {}),
            "execute_test_cases": test_round.product_snapshot.get("execute_test_cases",False),
            "execution_boundary": "firmware_flash" if test_round.product_snapshot.get("flash", {}).get("enabled") else "remote_file_verification",
        }

    def _ssh_base(self, bench: TestBench) -> list[str]:
        return [
            "-i", str(self.settings.qa_ssh_key), "-p", str(bench.port),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
        ]

    async def _remote(self, bench: TestBench, command: str, timeout: int = 120) -> dict[str, Any]:
        return await run_command(
            ["ssh", *self._ssh_base(bench), f"{bench.user}@{bench.host}", command],
            Path.cwd(), timeout,
        )

    async def _transfer(
        self,
        test_round: TestRound,
        bench: TestBench,
        config_path: Path,
        artifacts: list[dict[str, Any]],
    ) -> str:
        if not re.fullmatch(r"[0-9a-f-]{36}", test_round.id):
            raise TestWorkbenchError("Invalid Test Round id")
        remote_path = f"{bench.remote_root.rstrip('/')}/jobs/TEST-{test_round.display_number:06d}"
        prepared = await self._remote(bench, f"umask 077; mkdir -p {shlex.quote(remote_path)}/firmware {shlex.quote(remote_path)}/evidence")
        if prepared["exit_code"] != 0:
            raise TestWorkbenchError(prepared["stderr_tail"] or "Failed to prepare remote Test Round directory")
        uploads = [(config_path, f"{remote_path}/test-run.json")]
        uploads.extend((Path(item["local_path"]), f"{remote_path}/firmware/{item['filename']}") for item in artifacts)
        scp_options = self._ssh_base(bench)
        scp_options[scp_options.index("-p")] = "-P"
        for local, remote in uploads:
            result = await run_command(
                ["scp", *scp_options, "--", str(local), f"{bench.user}@{bench.host}:{remote}"],
                Path.cwd(), 900,
            )
            if result["exit_code"] != 0:
                raise TestWorkbenchError(result["stderr_tail"] or f"Failed to transfer {local.name}")
        return remote_path

    async def _verify_remote(self, bench: TestBench, remote_path: str, artifacts: list[dict[str, Any]], config_path: Path) -> dict[str, Any]:
        checks = [f"test -s {shlex.quote(remote_path + '/test-run.json')}"]
        config_digest = sha256_file(config_path)
        files = [{"filename": 'test-run.json', "sha256": config_digest, "size_bytes": config_path.stat().st_size}, *[{**item, 'filename': 'firmware/' + item['filename']} for item in artifacts]]
        for item in files:
            remote_file = f"{remote_path}/{item['filename']}"
            checks.append(
                f"test \"$(sha256sum {shlex.quote(remote_file)} | cut -d' ' -f1)\" = {shlex.quote(item['sha256'])}"
            )
        result = await self._remote(bench, "set -eu; " + "; ".join(checks))
        if result["exit_code"] != 0:
            raise TestWorkbenchError(result["stderr_tail"] or "Remote file verification failed")

        return {'passed': True, 'exit_code': result['exit_code'], 'verified_at': now().isoformat(), 'files': [{k: item[k] for k in ('filename', 'sha256', 'size_bytes')} for item in files]}
