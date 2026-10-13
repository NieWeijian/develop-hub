from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from develop_hub.git_workspace import GitWorkspaceManager, WorkspaceError


def make_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "workspace@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Workspace Test"], cwd=path, check=True)
    (path / "README.md").write_text("# workspace fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "branch", "stable"], cwd=path, check=True)


@pytest.mark.asyncio
async def test_create_branch_in_clean_existing_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    make_repository(repository)
    manager = GitWorkspaceManager(tmp_path / "managed-worktrees")

    destination = await manager.create_branch_in_existing_worktree(
        repository_path=str(repository),
        worktree_path=str(repository),
        initial_branch="main",
        source_branch="stable",
        work_branch="codex/run-reuse-fixture",
    )

    assert destination == repository.resolve()
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert branch == "codex/run-reuse-fixture"
    assert await manager.validate_branch(str(repository), branch)

    (repository / "untracked.txt").write_text("preserved\n", encoding="utf-8")
    await manager.create_branch_in_existing_worktree(
        repository_path=str(repository),
        worktree_path=str(repository),
        initial_branch=branch,
        source_branch="stable",
        work_branch="codex/run-reuse-second",
    )
    assert (repository / "untracked.txt").read_text(encoding="utf-8") == "preserved\n"

    (repository / "README.md").write_text("tracked change\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="tracked, staged, or conflicted changes"):
        await manager.create_branch_in_existing_worktree(
            repository_path=str(repository),
            worktree_path=str(repository),
            initial_branch="codex/run-reuse-second",
            source_branch="stable",
            work_branch="codex/run-reuse-third",
        )


@pytest.mark.asyncio
async def test_controlled_merge_commits_only_declared_files(tmp_path: Path) -> None:
    repository = tmp_path / "merge-repository"
    make_repository(repository)
    manager = GitWorkspaceManager(tmp_path / "managed-worktrees")
    await manager.create_branch_in_existing_worktree(
        repository_path=str(repository),
        worktree_path=str(repository),
        initial_branch="main",
        source_branch="main",
        work_branch="codex/run-merge-fixture",
    )
    (repository / "README.md").write_text("# delivered change\n", encoding="utf-8")
    (repository / "delivery.txt").write_text("new delivery\n", encoding="utf-8")
    (repository / "operator-notes.txt").write_text("must stay untracked\n", encoding="utf-8")

    result = await manager.merge_completed_branch(
        repository_path=str(repository),
        source_worktree_path=str(repository),
        source_branch="codex/run-merge-fixture",
        target_branch="main",
        declared_files=["README.md", "delivery.txt"],
        commit_subject="src: test: apply controlled delivery",
        commit_body=["- update the declared fixture files", "- preserve unrelated operator notes"],
    )

    assert result.commit_created is True
    assert result.committed_files == ["README.md", "delivery.txt"]
    assert result.source_worktree_branch_after == "main"
    assert result.merge_commit == result.source_commit
    assert subprocess.run(
        ["git", "branch", "--show-current"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip() == "main"
    assert subprocess.run(
        ["git", "show", "main:delivery.txt"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout == "new delivery\n"
    assert (repository / "operator-notes.txt").read_text(encoding="utf-8") == "must stay untracked\n"
    assert "operator-notes.txt" in subprocess.run(
        ["git", "status", "--short"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout
    commit_text = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout
    assert commit_text.startswith("src: test: apply controlled delivery\n\n- update the declared fixture files")
    assert "Signed-off-by: Workspace Test <workspace@example.com>" in commit_text
    assert "Merge branch" not in commit_text


@pytest.mark.asyncio
async def test_controlled_merge_rejects_undeclared_tracked_changes(tmp_path: Path) -> None:
    repository = tmp_path / "merge-reject-repository"
    make_repository(repository)
    manager = GitWorkspaceManager(tmp_path / "managed-worktrees")
    await manager.create_branch_in_existing_worktree(
        repository_path=str(repository),
        worktree_path=str(repository),
        initial_branch="main",
        source_branch="main",
        work_branch="codex/run-merge-reject-fixture",
    )
    (repository / "README.md").write_text("undeclared tracked change\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="outside the Agent-declared delivery"):
        await manager.merge_completed_branch(
            repository_path=str(repository),
            source_worktree_path=str(repository),
            source_branch="codex/run-merge-reject-fixture",
            target_branch="main",
            declared_files=[],
            commit_subject="src: test: must not happen",
            commit_body=["- reject undeclared tracked changes"],
        )
