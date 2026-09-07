"""Repo key = the MAIN checkout basename (spec 2026-09-04 §5.C).

`fr isolation up` run from inside a linked worktree — a native Claude agent
worktree (`<repo>/.claude/worktrees/agent-…`) or a nested fr worktree — must
file the new workspace under `~/.cache/fr/worktrees/<main-checkout>/…`, never
under `agent-…` or a branch slug. `repo_cache_name` resolves through the git
common dir; `_worktree_up_core` uses it for the default path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fr.isolation.hostworktree import HostWorktreeTarget
from fr.isolation.local import subprocess_runner
from fr.isolation.types import repo_cache_name


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def layout(tmp_path: Path) -> dict[str, Path]:
    """Main checkout `acme` + two linked worktrees: one nested inside the main
    checkout in Claude's native agent shape, one elsewhere in a branch-slug dir."""
    main = tmp_path / "acme"
    main.mkdir()
    _git("init", "-q", "-b", "main", str(main))
    (main / "README.md").write_text("x\n")
    _git("-C", str(main), "add", "-A")
    _git("-C", str(main), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    agent = main / ".claude" / "worktrees" / "agent-abc"
    agent.parent.mkdir(parents=True)
    _git("-C", str(main), "worktree", "add", "-q", "--detach", str(agent), "HEAD")
    nested = tmp_path / "elsewhere" / "feat__nested"
    nested.parent.mkdir(parents=True)
    _git("-C", str(main), "worktree", "add", "-q", "--detach", str(nested), "HEAD")
    return {"main": main, "agent": agent, "nested": nested}


def test_repo_cache_name_resolves_to_main_checkout(layout: dict[str, Path]) -> None:
    assert repo_cache_name(layout["main"]) == "acme"
    assert repo_cache_name(layout["agent"]) == "acme"
    assert repo_cache_name(layout["nested"]) == "acme"


def test_repo_cache_name_falls_back_to_basename_outside_git(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert repo_cache_name(plain) == "not-a-repo"


def _fake_run(
    argv: list[str], cwd: Path | None = None, check: bool = False, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    if argv and argv[0] == "git":
        return subprocess_runner(argv, cwd=cwd, check=check, capture=capture)
    return subprocess.CompletedProcess(argv, 0, "", "")


def test_up_from_inside_agent_worktree_files_under_main_checkout(
    layout: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FR_ISOLATION_TARGET", "worktree")
    target = HostWorktreeTarget(layout["agent"], runner=_fake_run, gc_spawner=lambda _r: None)

    state = target.up(profile=None, branch="feat/from-inside", base="HEAD")

    expected = home / ".cache" / "fr" / "worktrees" / "acme" / "feat__from-inside"
    assert state.worktree == expected
    assert expected.is_dir()
    assert not (home / ".cache" / "fr" / "worktrees" / "agent-abc").exists()
