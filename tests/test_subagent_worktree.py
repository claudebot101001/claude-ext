"""Tests for extensions/subagent/worktree.py — git worktree utilities.

Each test creates a temporary git repo (with an initial commit) so that
worktree operations have a valid base to work from.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from extensions.subagent.worktree import (
    check_clean,
    cleanup_worktree,
    create_worktree,
    get_current_branch,
    get_worktree_diff,
    get_worktree_diff_stat_only,
    is_git_repo,
    squash_merge,
)


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with an initial commit on 'main'."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*cmd):
        subprocess.run(cmd, cwd=str(repo), check=True, capture_output=True)

    run("git", "init", "-b", "main")
    run("git", "config", "user.email", "test@test.com")
    run("git", "config", "user.name", "Test")
    # Initial commit
    (repo / "README.md").write_text("# Test Repo\n")
    run("git", "add", "README.md")
    run("git", "commit", "-m", "Initial commit")

    return str(repo)


@pytest.fixture
def worktree_base(tmp_path):
    """Base directory for worktrees."""
    d = tmp_path / "worktrees"
    d.mkdir()
    return str(d)


# -- is_git_repo -------------------------------------------------------------


class TestIsGitRepo:
    def test_valid_repo(self, git_repo):
        assert _run(is_git_repo(git_repo))

    def test_non_repo(self, tmp_path):
        d = tmp_path / "not-a-repo"
        d.mkdir()
        assert not _run(is_git_repo(str(d)))


# -- get_current_branch ------------------------------------------------------


class TestGetCurrentBranch:
    def test_main_branch(self, git_repo):
        branch = _run(get_current_branch(git_repo))
        assert branch == "main"


# -- create_worktree ---------------------------------------------------------


class TestCreateWorktree:
    def test_creates_worktree(self, git_repo, worktree_base):
        wt_path, branch, parent = _run(
            create_worktree(git_repo, worktree_base, "test-worker", "abc12345")
        )
        assert branch == "subagent/test-worker-abc12345"
        assert parent == "main"
        assert Path(wt_path).exists()
        assert (Path(wt_path) / "README.md").exists()

    def test_worktree_branch_separate(self, git_repo, worktree_base):
        """Worktree should be on a new branch independent of main."""

        async def _test():
            wt_path, branch, _ = await create_worktree(
                git_repo, worktree_base, "isolated", "def67890"
            )
            wt_branch = await get_current_branch(wt_path)
            assert wt_branch == branch
            main_branch = await get_current_branch(git_repo)
            assert main_branch == "main"

        asyncio.run(_test())

    def test_name_sanitization(self, git_repo, worktree_base):
        """Special characters in name should be sanitized."""
        _wt_path, branch, _ = _run(
            create_worktree(git_repo, worktree_base, "hello world!@#", "x1y2z3w4")
        )
        assert "subagent/" in branch
        assert " " not in branch
        assert "@" not in branch


# -- check_clean -------------------------------------------------------------


class TestCheckClean:
    def test_clean_repo(self, git_repo):
        assert _run(check_clean(git_repo))

    def test_dirty_repo(self, git_repo):
        (Path(git_repo) / "new_file.txt").write_text("dirty")
        assert not _run(check_clean(git_repo))


# -- diff operations ---------------------------------------------------------


class TestDiffOperations:
    def test_diff_no_changes(self, git_repo, worktree_base):
        async def _test():
            wt_path, _, parent = await create_worktree(
                git_repo, worktree_base, "no-changes", "00000000"
            )
            result = await get_worktree_diff(wt_path, parent)
            assert result["diff"] == ""
            assert result["truncated"] is False

        asyncio.run(_test())

    def test_diff_with_changes(self, git_repo, worktree_base):
        async def _test():
            wt_path, _, parent = await create_worktree(
                git_repo, worktree_base, "with-changes", "11111111"
            )
            (Path(wt_path) / "new_file.py").write_text("print('hello')\n")
            subprocess.run(
                ["git", "add", "new_file.py"],
                cwd=wt_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Add new file"],
                cwd=wt_path,
                check=True,
                capture_output=True,
            )
            result = await get_worktree_diff(wt_path, parent)
            assert "new_file.py" in result["diff"]
            assert "new_file.py" in result["stat"]
            assert result["truncated"] is False

        asyncio.run(_test())

    def test_diff_stat_only(self, git_repo, worktree_base):
        async def _test():
            wt_path, _, parent = await create_worktree(
                git_repo, worktree_base, "stat-test", "22222222"
            )
            (Path(wt_path) / "change.txt").write_text("changed\n")
            subprocess.run(
                ["git", "add", "change.txt"],
                cwd=wt_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Add change"],
                cwd=wt_path,
                check=True,
                capture_output=True,
            )
            stat = await get_worktree_diff_stat_only(wt_path, parent)
            assert "change.txt" in stat

        asyncio.run(_test())


# -- squash_merge ------------------------------------------------------------


class TestSquashMerge:
    def test_merge_success(self, git_repo, worktree_base):
        async def _test():
            wt_path, branch, parent = await create_worktree(
                git_repo, worktree_base, "merge-test", "33333333"
            )
            (Path(wt_path) / "feature.py").write_text("def feature(): pass\n")
            subprocess.run(
                ["git", "add", "feature.py"],
                cwd=wt_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Add feature"],
                cwd=wt_path,
                check=True,
                capture_output=True,
            )
            result = await squash_merge(git_repo, branch, parent)
            assert "error" not in result
            assert "feature.py" in result["staged_files"]

        asyncio.run(_test())

    def test_merge_wrong_branch(self, git_repo, worktree_base):
        async def _test():
            _, branch, _ = await create_worktree(
                git_repo, worktree_base, "wrong-branch", "44444444"
            )
            result = await squash_merge(git_repo, branch, "nonexistent-branch")
            assert "error" in result
            assert "nonexistent-branch" in result["error"]

        asyncio.run(_test())

    def test_merge_dirty_workdir(self, git_repo, worktree_base):
        async def _test():
            _, branch, parent = await create_worktree(
                git_repo, worktree_base, "dirty-merge", "55555555"
            )
            (Path(git_repo) / "dirty.txt").write_text("uncommitted")
            result = await squash_merge(git_repo, branch, parent)
            assert "error" in result
            assert "uncommitted" in result["error"]

        asyncio.run(_test())


# -- cleanup_worktree --------------------------------------------------------


class TestCleanupWorktree:
    def test_cleanup(self, git_repo, worktree_base):
        async def _test():
            wt_path, branch, _ = await create_worktree(
                git_repo, worktree_base, "cleanup-test", "66666666"
            )
            assert Path(wt_path).exists()
            result = await cleanup_worktree(git_repo, wt_path, branch)
            assert result["removed"] is True
            assert not Path(wt_path).exists()

        asyncio.run(_test())
