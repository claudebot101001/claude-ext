"""Git worktree utility functions for sub-agent isolation.

All functions are async (subprocess-based) and run in the main process.

Worktree storage layout::

    {state_dir}/worktrees/{repo_basename}/{branch_name}/

Branch naming convention: ``subagent/{safe-name}-{hex8}``
"""

import asyncio
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Max diff size returned to avoid blowing up MCP responses.
MAX_DIFF_CHARS = 50_000


async def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


def _safe_name(name: str) -> str:
    """Sanitize a name for use in git branch names."""
    s = re.sub(r"[^a-zA-Z0-9_-]", "-", name.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:40] or "worker"


async def is_git_repo(path: str) -> bool:
    """Check whether *path* is inside a git repository."""
    rc, _, _ = await _run(["git", "rev-parse", "--git-dir"], cwd=path)
    return rc == 0


async def get_current_branch(repo_dir: str) -> str:
    """Return the current branch name (or HEAD hash if detached)."""
    rc, out, _ = await _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    if rc != 0:
        raise RuntimeError(f"git rev-parse failed in {repo_dir}")
    return out


async def create_worktree(
    repo_dir: str,
    base_dir: str,
    name: str,
    short_id: str,
) -> tuple[str, str, str]:
    """Create a git worktree with a new branch.

    Args:
        repo_dir: Path to the main git repository.
        base_dir: Base directory for worktrees (e.g. ``{state_dir}/worktrees``).
        name: Human-readable name (sanitized for branch name).
        short_id: Short identifier (e.g. uuid4 hex[:8]).

    Returns:
        Tuple of (worktree_path, branch_name, parent_branch).
    """
    parent_branch = await get_current_branch(repo_dir)

    safe = _safe_name(name)
    branch = f"subagent/{safe}-{short_id}"

    # Organize worktrees by repo basename
    repo_name = Path(repo_dir).name
    wt_path = str(Path(base_dir) / repo_name / f"{safe}-{short_id}")

    # Ensure parent directory exists
    Path(wt_path).parent.mkdir(parents=True, exist_ok=True)

    rc, _out, err = await _run(
        ["git", "worktree", "add", "-b", branch, wt_path],
        cwd=repo_dir,
    )
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {err}")

    log.info("Created worktree: %s (branch %s from %s)", wt_path, branch, parent_branch)
    return wt_path, branch, parent_branch


async def get_worktree_diff(worktree_path: str, parent_branch: str) -> dict:
    """Get the full diff between worktree HEAD and the parent branch.

    Returns dict with ``stat`` (summary) and ``diff`` (full, possibly truncated).
    """
    stat = await get_worktree_diff_stat_only(worktree_path, parent_branch)

    _rc, diff_out, _ = await _run(
        ["git", "diff", f"{parent_branch}...HEAD"],
        cwd=worktree_path,
    )
    truncated = False
    if len(diff_out) > MAX_DIFF_CHARS:
        diff_out = diff_out[:MAX_DIFF_CHARS] + "\n... [truncated]"
        truncated = True

    return {
        "stat": stat,
        "diff": diff_out,
        "truncated": truncated,
    }


async def get_worktree_diff_stat_only(worktree_path: str, parent_branch: str) -> str:
    """Get diff --stat summary between worktree HEAD and parent branch."""
    _rc, out, _ = await _run(
        ["git", "diff", "--stat", f"{parent_branch}...HEAD"],
        cwd=worktree_path,
    )
    return out


async def check_clean(repo_dir: str) -> bool:
    """Check if the working directory is clean (no uncommitted changes)."""
    rc, out, _ = await _run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
    )
    return rc == 0 and not out


async def squash_merge(repo_dir: str, worktree_branch: str, parent_branch: str) -> dict:
    """Squash-merge a worktree branch into the current branch.

    Does NOT commit or checkout. Changes are left staged.

    Args:
        repo_dir: Path to the main repository.
        worktree_branch: Branch to merge from.
        parent_branch: Expected current branch (safety check).

    Returns:
        Dict with ``staged_files`` list or ``error`` string.
    """
    # Verify current branch matches expected parent
    current = await get_current_branch(repo_dir)
    if current != parent_branch:
        return {
            "error": (
                f"Current branch is '{current}' but expected '{parent_branch}'. "
                f"Please checkout '{parent_branch}' first."
            )
        }

    # Verify working directory is clean
    if not await check_clean(repo_dir):
        return {"error": "Working directory has uncommitted changes. Please commit or stash first."}

    # Perform squash merge (no commit)
    rc, _out, err = await _run(
        ["git", "merge", "--squash", worktree_branch],
        cwd=repo_dir,
    )
    if rc != 0:
        return {"error": f"Merge failed: {err}"}

    # Get list of staged files
    _rc2, staged_out, _ = await _run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo_dir,
    )
    staged_files = [f for f in staged_out.splitlines() if f]

    return {"staged_files": staged_files}


async def cleanup_worktree(repo_dir: str, worktree_path: str, branch: str) -> dict:
    """Remove a worktree and delete its branch.

    Returns dict with ``removed`` (bool) and optional ``errors`` list.
    """
    errors = []

    # Remove worktree
    rc, _, err = await _run(
        ["git", "worktree", "remove", "--force", worktree_path],
        cwd=repo_dir,
    )
    if rc != 0:
        errors.append(f"worktree remove: {err}")

    # Delete branch
    rc, _, err = await _run(
        ["git", "branch", "-D", branch],
        cwd=repo_dir,
    )
    if rc != 0:
        errors.append(f"branch delete: {err}")

    if errors:
        log.warning("Worktree cleanup issues for %s: %s", branch, "; ".join(errors))

    return {"removed": len(errors) == 0, "errors": errors}
