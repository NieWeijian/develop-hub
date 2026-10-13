from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class GitWorktree:
    path: Path
    branch: str | None
    commit: str
    is_main: bool


@dataclass(frozen=True)
class MergeResult:
    source_branch: str
    target_branch: str
    source_commit: str
    target_commit_before: str
    merge_commit: str
    committed_files: list[str]
    commit_created: bool
    integration_worktree: str
    source_worktree_branch_after: str
    output: str


async def run_command(*args: str, cwd: Path | None = None) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode(errors="replace").strip(),
        stderr=stderr.decode(errors="replace").strip(),
    )


class GitWorkspaceManager:
    def __init__(self, worktree_root: Path) -> None:
        self.worktree_root = worktree_root.resolve()

    async def validate_repository(self, root_path: str) -> Path:
        root = Path(root_path).expanduser().resolve()
        if not root.is_dir():
            raise WorkspaceError(f"Repository path does not exist: {root}")
        result = await run_command("git", "rev-parse", "--show-toplevel", cwd=root)
        if result.returncode != 0:
            raise WorkspaceError(result.stderr or f"Not a Git repository: {root}")
        return Path(result.stdout).resolve()

    async def list_branches(self, root_path: str) -> list[str]:
        repository = await self.validate_repository(root_path)
        result = await run_command(
            "git",
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
            "refs/remotes",
            cwd=repository,
        )
        if result.returncode != 0:
            raise WorkspaceError(result.stderr or "Unable to list repository branches")
        branches = {
            branch.strip()
            for branch in result.stdout.splitlines()
            if branch.strip() and not branch.strip().endswith("/HEAD")
        }
        return sorted(branches, key=lambda branch: ("/" in branch, branch))

    async def validate_branch(self, root_path: str, branch: str) -> str:
        repository = await self.validate_repository(root_path)
        normalized = branch.strip()
        if not normalized:
            raise WorkspaceError("Source branch cannot be empty")
        branches = await self.list_branches(str(repository))
        if normalized not in branches:
            available = ", ".join(branches[:12]) if branches else "none (the repository may have no commits)"
            raise WorkspaceError(f"Source branch '{normalized}' does not exist. Available branches: {available}")
        result = await run_command(
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{normalized}^{{commit}}",
            cwd=repository,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.splitlines()[0]

        available = ", ".join(branches[:12]) if branches else "none (the repository may have no commits)"
        raise WorkspaceError(
            f"Source branch '{normalized}' does not resolve to a commit. Available branches: {available}"
        )

    async def list_worktrees(self, root_path: str) -> list[GitWorktree]:
        repository = await self.validate_repository(root_path)
        result = await run_command("git", "worktree", "list", "--porcelain", cwd=repository)
        if result.returncode != 0:
            raise WorkspaceError(result.stderr or "Unable to list repository worktrees")

        worktrees: list[GitWorktree] = []
        for block in result.stdout.split("\n\n"):
            values: dict[str, str] = {}
            detached = False
            for line in block.splitlines():
                if line == "detached":
                    detached = True
                elif " " in line:
                    key, value = line.split(" ", 1)
                    values[key] = value
            if "worktree" not in values:
                continue
            path = Path(values["worktree"]).expanduser().resolve()
            branch_ref = values.get("branch")
            branch = None if detached or not branch_ref else branch_ref.removeprefix("refs/heads/")
            worktrees.append(
                GitWorktree(
                    path=path,
                    branch=branch,
                    commit=values.get("HEAD", ""),
                    is_main=path == repository,
                )
            )
        return worktrees

    async def validate_existing_worktree(
        self,
        repository_path: str,
        worktree_path: str,
        expected_branch: str | None = None,
    ) -> GitWorktree:
        destination = Path(worktree_path).expanduser().resolve()
        worktrees = await self.list_worktrees(repository_path)
        selected = next((worktree for worktree in worktrees if worktree.path == destination), None)
        if not selected:
            raise WorkspaceError(f"Worktree is not registered in this repository: {destination}")
        if not destination.is_dir():
            raise WorkspaceError(f"Worktree path does not exist: {destination}")
        if not selected.branch:
            raise WorkspaceError("Detached HEAD worktrees cannot be used for a Coding Run")
        if expected_branch and selected.branch != expected_branch:
            raise WorkspaceError(
                f"Worktree branch changed from '{expected_branch}' to '{selected.branch}'; refresh and select it again"
            )
        return selected

    async def validate_switchable_worktree(
        self,
        repository_path: str,
        worktree_path: str,
    ) -> GitWorktree:
        selected = await self.validate_existing_worktree(repository_path, worktree_path)
        status = await run_command(
            "git", "status", "--porcelain=v1", "--untracked-files=no", cwd=selected.path
        )
        if status.returncode != 0:
            raise WorkspaceError(status.stderr or "Unable to inspect the selected worktree")
        if status.stdout:
            raise WorkspaceError(
                "Selected worktree has tracked, staged, or conflicted changes; commit or stash them before switching branches"
            )
        return selected

    @staticmethod
    def _path_names(result: CommandResult, action: str) -> set[str]:
        if result.returncode != 0:
            raise WorkspaceError(result.stderr or f"Unable to {action}")
        return {name for name in result.stdout.split("\0") if name}

    async def merge_completed_branch(
        self,
        *,
        repository_path: str,
        source_worktree_path: str,
        source_branch: str,
        target_branch: str,
        declared_files: list[str],
        commit_subject: str,
        commit_body: list[str],
    ) -> MergeResult:
        repository = await self.validate_repository(repository_path)
        source_worktree = await self.validate_existing_worktree(
            str(repository), source_worktree_path, expected_branch=source_branch
        )
        await self.validate_branch(str(repository), source_branch)
        await self.validate_branch(str(repository), target_branch)
        if source_branch == target_branch:
            raise WorkspaceError("Source branch is already the repository default branch")

        safe_declared: list[str] = []
        for raw_path in declared_files:
            relative = Path(raw_path)
            resolved = (source_worktree.path / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or (
                resolved != source_worktree.path and source_worktree.path not in resolved.parents
            ):
                raise WorkspaceError(f"Agent declared an unsafe changed file path: {raw_path}")
            normalized = relative.as_posix()
            if normalized not in safe_declared:
                safe_declared.append(normalized)
        declared = set(safe_declared)

        conflicted = self._path_names(
            await run_command("git", "diff", "--name-only", "--diff-filter=U", "-z", cwd=source_worktree.path),
            "inspect merge conflicts",
        )
        if conflicted:
            raise WorkspaceError(f"Source worktree has unresolved conflicts: {', '.join(sorted(conflicted))}")
        staged = self._path_names(
            await run_command("git", "diff", "--cached", "--name-only", "-z", cwd=source_worktree.path),
            "inspect staged changes",
        )
        if staged:
            raise WorkspaceError(
                "Source worktree already has staged changes; commit or unstage them before using the merge gate"
            )
        tracked = self._path_names(
            await run_command("git", "diff", "--name-only", "-z", cwd=source_worktree.path),
            "inspect tracked changes",
        )
        outside = tracked - declared
        if outside:
            raise WorkspaceError(
                "Tracked changes outside the Agent-declared delivery must be handled first: "
                + ", ".join(sorted(outside))
            )
        untracked = self._path_names(
            await run_command("git", "ls-files", "--others", "--exclude-standard", "-z", cwd=source_worktree.path),
            "inspect untracked files",
        )
        changed_files = sorted(tracked | (untracked & declared))
        commit_created = False
        outputs: list[str] = []
        if changed_files:
            staged_result = await run_command(
                "git", "add", "-A", "--", *changed_files, cwd=source_worktree.path
            )
            if staged_result.returncode != 0:
                raise WorkspaceError(staged_result.stderr or staged_result.stdout or "Unable to stage declared delivery files")
            commit = await run_command(
                "git", "commit", "--signoff", "-m", commit_subject, "-m", "\n".join(commit_body),
                cwd=source_worktree.path,
            )
            if commit.returncode != 0:
                await run_command("git", "restore", "--staged", "--", *changed_files, cwd=source_worktree.path)
                raise WorkspaceError(commit.stderr or commit.stdout or "Unable to commit declared delivery files")
            commit_created = True
            outputs.append(commit.stdout or commit.stderr)

        source_commit_result = await run_command("git", "rev-parse", "HEAD", cwd=source_worktree.path)
        if source_commit_result.returncode != 0:
            raise WorkspaceError(source_commit_result.stderr or "Unable to resolve source commit")
        source_commit = source_commit_result.stdout

        worktrees = await self.list_worktrees(str(repository))
        target_worktree = next((item for item in worktrees if item.branch == target_branch), None)
        switched_source_worktree = target_worktree is None
        integration = source_worktree if switched_source_worktree else target_worktree
        assert integration is not None
        await self.validate_switchable_worktree(str(repository), str(integration.path))

        target_before_result = await run_command(
            "git", "rev-parse", f"{target_branch}^{{commit}}", cwd=repository
        )
        if target_before_result.returncode != 0:
            raise WorkspaceError(target_before_result.stderr or "Unable to resolve target commit")
        target_before = target_before_result.stdout

        if switched_source_worktree:
            checkout = await run_command("git", "checkout", target_branch, cwd=integration.path)
            if checkout.returncode != 0:
                raise WorkspaceError(checkout.stderr or checkout.stdout or "Unable to checkout target branch")
            outputs.append(checkout.stdout or checkout.stderr)

        merged = await run_command("git", "merge", "--ff-only", source_branch, cwd=integration.path)
        if merged.returncode != 0:
            if switched_source_worktree:
                await run_command("git", "checkout", source_branch, cwd=integration.path)
            raise WorkspaceError(
                merged.stderr or merged.stdout
                or "Fast-forward merge failed; update the task branch from the default branch in a new Coding Run"
            )
        outputs.append(merged.stdout or merged.stderr)

        merge_commit_result = await run_command("git", "rev-parse", "HEAD", cwd=integration.path)
        if merge_commit_result.returncode != 0:
            raise WorkspaceError(merge_commit_result.stderr or "Unable to resolve merge commit")
        return MergeResult(
            source_branch=source_branch,
            target_branch=target_branch,
            source_commit=source_commit,
            target_commit_before=target_before,
            merge_commit=merge_commit_result.stdout,
            committed_files=changed_files,
            commit_created=commit_created,
            integration_worktree=str(integration.path),
            source_worktree_branch_after=target_branch if switched_source_worktree else source_branch,
            output="\n".join(item for item in outputs if item),
        )

    async def create_branch_in_existing_worktree(
        self,
        repository_path: str,
        worktree_path: str,
        initial_branch: str,
        source_branch: str,
        work_branch: str,
    ) -> Path:
        repository = await self.validate_repository(repository_path)
        selected = await self.validate_existing_worktree(str(repository), worktree_path)
        if selected.branch == work_branch:
            await self.validate_switchable_worktree(str(repository), worktree_path)
            return selected.path
        if selected.branch != initial_branch:
            raise WorkspaceError(
                f"Worktree branch changed from '{initial_branch}' to '{selected.branch}'; refresh and select it again"
            )
        await self.validate_switchable_worktree(str(repository), worktree_path)
        await self.validate_branch(str(repository), source_branch)
        existing = await run_command(
            "git", "show-ref", "--verify", "--quiet", f"refs/heads/{work_branch}", cwd=repository
        )
        if existing.returncode == 0:
            raise WorkspaceError(f"Target work branch already exists: {work_branch}")
        switched = await run_command(
            "git", "checkout", "-b", work_branch, source_branch, cwd=selected.path
        )
        if switched.returncode != 0:
            raise WorkspaceError(switched.stderr or switched.stdout or "Unable to create the work branch")
        return selected.path

    async def ensure_worktree(
        self,
        repository_path: str,
        source_branch: str,
        work_branch: str,
        run_id: str,
    ) -> Path:
        repository = await self.validate_repository(repository_path)
        await self.validate_branch(str(repository), source_branch)
        destination = (self.worktree_root / run_id).resolve()
        if self.worktree_root not in destination.parents:
            raise WorkspaceError("Resolved worktree path escaped the configured root")

        if destination.exists():
            marker = destination / ".git"
            if marker.exists():
                return destination
            raise WorkspaceError(f"Worktree destination is occupied: {destination}")

        branch_check = await run_command(
            "git", "show-ref", "--verify", "--quiet", f"refs/heads/{work_branch}", cwd=repository
        )
        if branch_check.returncode == 0:
            args = ("git", "worktree", "add", str(destination), work_branch)
        else:
            args = ("git", "worktree", "add", "-b", work_branch, str(destination), source_branch)

        result = await run_command(*args, cwd=repository)
        if result.returncode != 0:
            raise WorkspaceError(result.stderr or result.stdout or "git worktree add failed")
        return destination

    async def remove_worktree(
        self,
        repository_path: str,
        worktree_path: str,
        work_branch: str,
        source_branch: str,
    ) -> None:
        repository = await self.validate_repository(repository_path)
        destination = Path(worktree_path).expanduser().resolve()
        if destination == self.worktree_root or self.worktree_root not in destination.parents:
            raise WorkspaceError("Refusing to remove a worktree outside the configured worktree root")

        listed = await run_command("git", "worktree", "list", "--porcelain", cwd=repository)
        if listed.returncode != 0:
            raise WorkspaceError(listed.stderr or "Unable to inspect registered Git worktrees")
        registered_paths = {
            Path(line.removeprefix("worktree ")).resolve()
            for line in listed.stdout.splitlines()
            if line.startswith("worktree ")
        }
        if destination in registered_paths:
            removed = await run_command("git", "worktree", "remove", "--force", str(destination), cwd=repository)
            if removed.returncode != 0:
                raise WorkspaceError(removed.stderr or removed.stdout or "git worktree remove failed")
        elif destination.exists():
            raise WorkspaceError("Worktree directory exists but is not registered in the repository")

        if work_branch.startswith("codex/run-"):
            branch_tip = await run_command("git", "rev-parse", "--verify", "--quiet", f"{work_branch}^{{commit}}", cwd=repository)
            source_tip = await run_command("git", "rev-parse", "--verify", "--quiet", f"{source_branch}^{{commit}}", cwd=repository)
            if (
                branch_tip.returncode == 0
                and source_tip.returncode == 0
                and branch_tip.stdout == source_tip.stdout
            ):
                deleted = await run_command("git", "branch", "-D", "--", work_branch, cwd=repository)
                if deleted.returncode != 0:
                    raise WorkspaceError(deleted.stderr or deleted.stdout or "Unable to delete unused work branch")
