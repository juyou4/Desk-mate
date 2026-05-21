"""Pure-Python git branch lookup tests (V10 Phase 14-ii)."""

from __future__ import annotations

from pathlib import Path

from deskmate_agent.git_branch import current_branch


def _make_repo(root: Path, head_content: str) -> None:
    git = root / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(head_content)


def test_returns_none_outside_any_git_repo(tmp_path: Path) -> None:
    assert current_branch(tmp_path) is None


def test_reads_branch_from_standard_ref(tmp_path: Path) -> None:
    _make_repo(tmp_path, "ref: refs/heads/main\n")
    assert current_branch(tmp_path) == "main"


def test_walks_up_from_subdirectory(tmp_path: Path) -> None:
    _make_repo(tmp_path, "ref: refs/heads/feat/island\n")
    nested = tmp_path / "src" / "deep" / "nested"
    nested.mkdir(parents=True)
    assert current_branch(nested) == "feat/island"


def test_detached_head_returns_short_sha(tmp_path: Path) -> None:
    _make_repo(tmp_path, "abcdef1234567890\n")
    assert current_branch(tmp_path) == "abcdef1"


def test_worktree_gitfile_redirects_to_real_gitdir(tmp_path: Path) -> None:
    real_gitdir = tmp_path / "repo" / ".git" / "worktrees" / "feature"
    real_gitdir.mkdir(parents=True)
    (real_gitdir / "HEAD").write_text("ref: refs/heads/feature\n")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {real_gitdir}\n")
    assert current_branch(worktree) == "feature"


def test_malformed_head_returns_none(tmp_path: Path) -> None:
    _make_repo(tmp_path, "")
    assert current_branch(tmp_path) is None


def test_branch_with_trailing_whitespace_is_trimmed(tmp_path: Path) -> None:
    _make_repo(tmp_path, "ref: refs/heads/main  \n")
    assert current_branch(tmp_path) == "main"
