"""fr-statusline-segment.sh — golden outputs (spec 2026-09-04 §5.E, decision d4).

The segment reads Claude Code status-line JSON on stdin and prints exactly two
lines: the iso segment (``iso: <bound worktree>`` | ``iso: ? (branches)`` |
empty) and the worktree gauge (``worktrees (N): branch:rel, ...`` | empty).
Shell + jq + git only — the fr CLI is never invoked (4 s, spec §5.E budget is
60 ms). Filed under the acceptance level ``int`` (subprocess; see journal
62c39ba6fb84).

Fixture: a repo ``home/Docs/acme`` (branch main) with two linked worktrees —
``home/Docs/acme/.worktrees/blog`` (docs/blog) and
``home/.cache/fr/worktrees/acme/feat__x`` (feat/x, an fr workspace with a
state file) — and, per case, a session index written by ``sessions.attach``.
Gauge order is git's own: ``git worktree list`` prints the main checkout first
and linked worktrees sorted by path, so ``~/.cache/...`` precedes ``.worktrees``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fr.isolation import sessions
from fr.isolation.types import IsolationState, save_state

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="segment script requires jq")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "plugins" / "super-fr" / "scripts" / "fr-statusline-segment.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass
class World:
    home: Path
    repo: Path
    blog: Path
    featx: Path
    sessions_dir: Path

    def bind(self, sid: str = "sess-1") -> None:
        sessions.attach(self.repo, "feat/x", sid, "claude")

    def env(self, **extra: str) -> dict[str, str]:
        env = {**os.environ, "HOME": str(self.home), "FR_SESSIONS_DIR": str(self.sessions_dir)}
        env.pop("STATUSLINE_WT_WIDTH", None)
        env.update(extra)
        return env

    def run(
        self, cwd: Path | str, sid: str | None = "sess-1", **extra: str
    ) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {"workspace": {"current_dir": str(cwd)}, "cwd": str(cwd)}
        if sid is not None:
            payload["session_id"] = sid
        return subprocess.run(
            ["bash", str(SCRIPT)],
            input=json.dumps(payload),
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    home = tmp_path.resolve() / "home"
    repo = home / "Docs" / "acme"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    blog = repo / ".worktrees" / "blog"
    _git(repo, "worktree", "add", "-q", "-b", "docs/blog", str(blog))
    featx = home / ".cache" / "fr" / "worktrees" / "acme" / "feat__x"
    _git(repo, "worktree", "add", "-q", "-b", "feat/x", str(featx))
    save_state(
        IsolationState(
            repo_root=repo,
            branch="feat/x",
            worktree=featx,
            profile="host",
            created_at="2026-09-05T00:00:00+00:00",
        )
    )
    sessions_dir = home / ".cache" / "fr" / "sessions"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FR_SESSIONS_DIR", str(sessions_dir))
    return World(home=home, repo=repo, blog=blog, featx=featx, sessions_dir=sessions_dir)


def _lines(res: subprocess.CompletedProcess[str]) -> list[str]:
    assert res.returncode == 0, res.stderr
    assert res.stdout.endswith("\n"), res.stdout
    lines = res.stdout[:-1].split("\n")
    assert len(lines) == 2, f"stdout must be exactly two lines: {res.stdout!r}"
    return lines


# (a) bound session, cwd = base clone: iso shows the full bound path; gauge
#     excludes both the cwd's own worktree and the bound one, N counts all.
def test_bound_from_base_clone(world: World) -> None:
    world.bind()
    iso, gauge = _lines(world.run(world.repo))
    assert iso == f"iso: {world.home}/.cache/fr/worktrees/acme/feat__x"
    assert gauge == "worktrees (3): docs/blog:.worktrees/blog"


# (b) unbound session (no index), cwd = base clone: dimmed "?" with the repo's
#     workspace branches; the fr worktree is outside main so it is ~-relative.
def test_unbound_from_base_clone(world: World) -> None:
    iso, gauge = _lines(world.run(world.repo))
    assert iso == "iso: ? (feat/x)"
    assert (
        gauge
        == "worktrees (3): feat/x:~/.cache/fr/worktrees/acme/feat__x, docs/blog:.worktrees/blog"
    )


# (c) bound session, cwd = the workspace itself: no iso line (cwd already is
#     the workspace); cwd is outside the main checkout so every path is
#     ~-relative, even the one that lives under main.
def test_bound_inside_workspace(world: World) -> None:
    world.bind()
    iso, gauge = _lines(world.run(world.featx))
    assert iso == ""
    assert gauge == "worktrees (3): main:~/Docs/acme, docs/blog:~/Docs/acme/.worktrees/blog"


# (d) cwd is not a git repo: both lines empty, exit 0.
def test_not_a_git_repo(world: World, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    iso, gauge = _lines(world.run(plain))
    assert (iso, gauge) == ("", "")


# (d') cwd does not exist at all: still two empty lines, exit 0.
def test_missing_cwd(world: World, tmp_path: Path) -> None:
    iso, gauge = _lines(world.run(tmp_path / "nope"))
    assert (iso, gauge) == ("", "")


# (e) missing session_id: treated as unbound even though an index exists.
def test_missing_session_id_is_unbound(world: World) -> None:
    world.bind()
    iso, gauge = _lines(world.run(world.repo, sid=None))
    assert iso == "iso: ? (feat/x)"
    assert gauge.startswith("worktrees (3): feat/x:")


# (e') session_id with no index (down / gc pruned it): unbound, no error.
def test_unknown_session_id_is_unbound(world: World) -> None:
    world.bind()
    iso, _ = _lines(world.run(world.repo, sid="sess-gone"))
    assert iso == "iso: ? (feat/x)"


# (f) a detached-HEAD worktree (native agent worktree) lists as <7-char sha>:<rel>.
def test_detached_worktree_shows_short_sha(world: World) -> None:
    agent = world.repo / ".claude" / "worktrees" / "agent-1"
    _git(world.repo, "worktree", "add", "-q", "--detach", str(agent))
    sha = _git(world.repo, "rev-parse", "HEAD")[:7]
    world.bind()
    _, gauge = _lines(world.run(world.repo))
    assert gauge == f"worktrees (4): {sha}:.claude/worktrees/agent-1, docs/blog:.worktrees/blog"


# (g) width bound (STATUSLINE_WT_WIDTH): the first token always shows, the rest
#     collapse into "+K more" once the budget is exceeded.
def test_width_budget_truncates(world: World) -> None:
    _, gauge = _lines(world.run(world.repo, STATUSLINE_WT_WIDTH="10"))
    assert gauge == "worktrees (3): feat/x:~/.cache/fr/worktrees/acme/feat__x, +1 more"


# (h) a single-worktree repo prints no gauge (nothing to list).
def test_single_worktree_no_gauge(world: World, tmp_path: Path) -> None:
    solo = tmp_path / "solo"
    solo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(solo)], check=True)
    _git(solo, "commit", "-q", "--allow-empty", "-m", "init")
    iso, gauge = _lines(world.run(solo))
    assert (iso, gauge) == ("", "")


# Timing guard: spec budget is 60 ms warm on the operator Mac; CI gets a
# generous 0.5 s so a slow runner never flakes but a stray fr call (4 s) fails.
def test_runs_well_under_budget(world: World) -> None:
    world.bind()
    world.run(world.repo)  # warm caches
    t0 = time.perf_counter()
    _lines(world.run(world.repo))
    assert time.perf_counter() - t0 < 0.5


# The script must never reach for the fr CLI: a trap fr first on PATH must not
# be invoked (and would blow the timing budget if it were).
def test_never_invokes_fr(world: World, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trap = bin_dir / "fr"
    trap.write_text('#!/bin/bash\necho TRAPPED >> "$FR_TRAP"\nexit 1\n')
    trap.chmod(0o755)
    log = tmp_path / "trap.log"
    world.bind()
    _lines(
        world.run(
            world.repo,
            PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
            FR_TRAP=str(log),
        )
    )
    assert not log.exists()
