"""gc hygiene verdicts (spec 2026-09-04 §5.A "gc" + §5.C).

- `empty-repo-dir`: a child of `~/.cache/fr/worktrees` with no subdirectories
  (the `agent-…` / branch-slug orphans left behind before the repo key was
  fixed) is removed; `--dry-run` reports `would-remove`.
- `stale-session`: a per-session index whose worktree is gone, or whose
  workspace state no longer lists the session, is unlinked. A healthy index is
  untouched.

Host-worktree mode throughout: no docker, no profile gate; git is real, `gh`
is faked to "no PR" so every live workspace classifies as `no-pr / warned`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fr.isolation import sessions
from fr.isolation.hostworktree import HostWorktreeTarget
from fr.isolation.local import GcAction, subprocess_runner
from fr.isolation.types import IsolationState, load_state, save_state

from tests.unit.test_isolation import make_repo


def _fake_run(
    argv: list[str], cwd: Path | None = None, check: bool = False, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    if argv and argv[0] == "git":
        return subprocess_runner(argv, cwd=cwd, check=check, capture=capture)
    if argv and argv[0] == "gh":
        return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("FR_SESSIONS_DIR", str(h / ".cache" / "fr" / "sessions"))
    monkeypatch.setenv("FR_ISOLATION_TARGET", "worktree")
    return h


@pytest.fixture()
def target(tmp_path: Path, home: Path) -> HostWorktreeTarget:
    repo = make_repo(tmp_path)
    return HostWorktreeTarget(repo, runner=_fake_run, gc_spawner=lambda _r: None)


def _cache(home: Path) -> Path:
    return home / ".cache" / "fr" / "worktrees"


# ---------- (a) empty-repo-dir ----------


def test_gc_dry_run_reports_empty_repo_dirs(home: Path, target: HostWorktreeTarget) -> None:
    orphan = _cache(home) / "agent-deadbeef"
    orphan.mkdir(parents=True)
    live = _cache(home) / "repo" / "feat__live"
    live.mkdir(parents=True)
    (live / "README.md").write_text("x\n")

    actions = target.gc(dry_run=True)

    assert GcAction(str(orphan), None, "empty-repo-dir", "would-remove") in actions
    repo_dir = str(_cache(home) / "repo")
    assert not [a for a in actions if a.verdict == "empty-repo-dir" and a.worktree == repo_dir]
    assert orphan.is_dir(), "dry-run mutates nothing"


def test_gc_removes_empty_repo_dirs_and_keeps_populated_ones(
    home: Path, target: HostWorktreeTarget
) -> None:
    orphan = _cache(home) / "agent-deadbeef"
    orphan.mkdir(parents=True)
    live = _cache(home) / "repo" / "feat__live"
    live.mkdir(parents=True)
    (live / "README.md").write_text("x\n")

    actions = target.gc()

    assert GcAction(str(orphan), None, "empty-repo-dir", "removed") in actions
    assert not orphan.exists()
    assert live.is_dir() and (live / "README.md").is_file()


def test_gc_treats_repo_dir_with_only_files_as_empty(
    home: Path, target: HostWorktreeTarget
) -> None:
    """Spec §5.C: "a directory with no subdirectories" — stray files do not
    make a repo dir a live cache entry."""
    junk = _cache(home) / "feat__slug"
    junk.mkdir(parents=True)
    (junk / ".DS_Store").write_text("")

    target.gc()

    assert not junk.exists()


def test_gc_without_cache_dir_reports_no_hygiene_actions(
    home: Path, target: HostWorktreeTarget
) -> None:
    assert not [a for a in target.gc() if a.verdict in ("empty-repo-dir", "stale-session")]


# ---------- (b) stale-session ----------


def _workspace(target: HostWorktreeTarget, branch: str) -> IsolationState:
    return target.up(profile=None, branch=branch, base="HEAD")


def test_gc_dry_run_reports_stale_session_index(home: Path, target: HostWorktreeTarget) -> None:
    state = _workspace(target, "feat/x")
    sessions.attach(target.repo_root, "feat/x", "sess-gone", harness="claude")
    index = sessions.session_index_path("sess-gone")
    assert index.is_file()
    shutil.rmtree(state.worktree)

    actions = target.gc(dry_run=True)

    assert (
        GcAction(str(state.worktree), "feat/x", "stale-session", "would-remove", str(index))
        in actions
    )
    assert index.is_file(), "dry-run mutates nothing"


def test_gc_unlinks_stale_session_index_and_keeps_healthy_one(
    home: Path, target: HostWorktreeTarget
) -> None:
    gone = _workspace(target, "feat/x")
    _workspace(target, "feat/y")
    sessions.attach(target.repo_root, "feat/x", "sess-gone", harness="claude")
    sessions.attach(target.repo_root, "feat/y", "sess-live", harness="claude")
    stale_index = sessions.session_index_path("sess-gone")
    live_index = sessions.session_index_path("sess-live")
    shutil.rmtree(gone.worktree)

    actions = target.gc()

    assert GcAction(str(gone.worktree), "feat/x", "stale-session", "removed", str(stale_index)) in (
        actions
    )
    assert not stale_index.exists()
    assert live_index.is_file()
    assert not [a for a in actions if a.verdict == "stale-session" and a.branch == "feat/y"]
    live_state = load_state(target.repo_root, "feat/y")
    assert live_state is not None
    assert [b.session_id for b in live_state.sessions] == ["sess-live"]


def test_gc_unlinks_index_whose_state_no_longer_lists_the_session(
    home: Path, target: HostWorktreeTarget
) -> None:
    """Worktree still there, but the state dropped the binding (e.g. a detach
    that lost the race with the index write) — the index is stale by the
    'state wins' rule and is pruned."""
    state = _workspace(target, "feat/x")
    sessions.attach(target.repo_root, "feat/x", "sess-orphan", harness="claude")
    index = sessions.session_index_path("sess-orphan")
    save_state(state.model_copy(update={"sessions": []}))

    actions = target.gc()

    expected = GcAction(str(state.worktree), "feat/x", "stale-session", "removed", str(index))
    assert expected in actions
    assert not index.exists()
    assert state.worktree.is_dir()
