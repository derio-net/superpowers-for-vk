"""fr isolation attach / detach / status --session / up --session / down unbinds.

CLI wiring for session bindings (spec 2026-09-04 §5.A). Fixtures mirror
tests/unit/test_isolation_cmd.py on purpose (copied, not imported — each file
owns its own doubles).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fr.cli import app
from fr.commands import isolation_cmd
from fr.isolation.types import IsolationState, load_state, save_state
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_real_gc_spawn(monkeypatch: pytest.MonkeyPatch):
    """Never fork a real `fr isolation gc` during CLI tests (#354)."""
    monkeypatch.setattr(isolation_cmd, "_gc_spawner", lambda _root: None)


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch: pytest.MonkeyPatch):
    """Host-worktree mode for every test here: no docker, no profile gate.
    (The parent file pins devcontainer mode; this one only exercises the
    binding surface, which is mode-neutral.)"""
    monkeypatch.setenv("FR_ISOLATION_TARGET", "worktree")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FR_SESSIONS_DIR", str(tmp_path / "sessions"))
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "x").write_text("x")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        check=True,
    )
    return r


@pytest.fixture()
def fake_run(monkeypatch: pytest.MonkeyPatch):
    """git runs for real; gh answers "no PR" (empty), everything else is a no-op."""
    calls: list[list[str]] = []

    def run(argv, cwd=None, check=False, capture=True):
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, check=check, capture_output=True, text=True)
        calls.append(list(argv))
        rc = 1 if argv[0] == "gh" else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    monkeypatch.setattr(isolation_cmd, "_runner", run)
    return calls


def _workspace(tmp_path: Path, repo: Path, branch: str) -> IsolationState:
    wt = tmp_path / "wt" / branch.replace("/", "__")
    wt.mkdir(parents=True, exist_ok=True)
    state = IsolationState(
        repo_root=repo,
        branch=branch,
        worktree=wt,
        profile="host",
        created_at="2026-09-05T00:00:00+00:00",
    )
    save_state(state)
    return state


def _index(tmp_path: Path, sid: str) -> Path:
    return tmp_path / "sessions" / f"{sid}.json"


# (a) attach --branch
def test_attach_explicit_branch(tmp_path: Path, repo: Path) -> None:
    _workspace(tmp_path, repo, "feat/x")
    res = runner.invoke(
        app,
        ["isolation", "attach", "--session", "s1", "--repo", str(repo), "--branch", "feat/x"],
    )
    assert res.exit_code == 0, res.output
    assert "attached s1 -> feat/x" in res.stdout
    assert _index(tmp_path, "s1").is_file()
    state = load_state(repo, "feat/x")
    assert state is not None and state.sessions[0].session_id == "s1"


# (b) attach without --branch: ambiguous vs single
def test_attach_without_branch_ambiguous(tmp_path: Path, repo: Path) -> None:
    _workspace(tmp_path, repo, "feat/x")
    _workspace(tmp_path, repo, "feat/y")
    res = runner.invoke(app, ["isolation", "attach", "--session", "s1", "--repo", str(repo)])
    assert res.exit_code == 2
    assert "multiple isolation workspaces — specify --branch" in res.output


def test_attach_without_branch_single_workspace(tmp_path: Path, repo: Path) -> None:
    _workspace(tmp_path, repo, "feat/x")
    res = runner.invoke(app, ["isolation", "attach", "--session", "s1", "--repo", str(repo)])
    assert res.exit_code == 0, res.output
    assert "attached s1 -> feat/x" in res.stdout


# (c) attach with no state
def test_attach_no_workspace_exits_2(tmp_path: Path, repo: Path) -> None:
    res = runner.invoke(
        app,
        ["isolation", "attach", "--session", "s1", "--repo", str(repo), "--branch", "feat/none"],
    )
    assert res.exit_code == 2
    assert "no isolation workspace" in res.output


# (d) detach — idempotent
def test_detach_then_detach_again(tmp_path: Path, repo: Path) -> None:
    _workspace(tmp_path, repo, "feat/x")
    runner.invoke(
        app,
        ["isolation", "attach", "--session", "s1", "--repo", str(repo), "--branch", "feat/x"],
    )
    res = runner.invoke(app, ["isolation", "detach", "--session", "s1"])
    assert res.exit_code == 0, res.output
    assert "detached s1 <- feat/x" in res.stdout
    assert not _index(tmp_path, "s1").exists()
    res = runner.invoke(app, ["isolation", "detach", "--session", "s1"])
    assert res.exit_code == 0, res.output
    assert "detached s1 (no binding)" in res.stdout


# (e) status: sessions column / json array / --session filter
def test_status_shows_sessions(tmp_path: Path, repo: Path, fake_run: list) -> None:
    _workspace(tmp_path, repo, "feat/x")
    _workspace(tmp_path, repo, "feat/y")
    runner.invoke(
        app,
        ["isolation", "attach", "--session", "s1", "--repo", str(repo), "--branch", "feat/x"],
    )

    res = runner.invoke(app, ["isolation", "status", "--repo", str(repo), "--format", "json"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.stdout)
    by_branch = {r["branch"]: r for r in rows}
    assert by_branch["feat/x"]["sessions"][0]["session_id"] == "s1"
    assert set(by_branch["feat/x"]["sessions"][0]) == {"session_id", "harness", "attached_at"}
    assert by_branch["feat/y"]["sessions"] == []

    res = runner.invoke(app, ["isolation", "status", "--repo", str(repo)])
    assert res.exit_code == 0, res.output
    lines = {ln.split(":")[0]: ln for ln in res.stdout.splitlines() if ln.startswith("feat/")}
    assert lines["feat/x"].endswith(" sessions=s1")
    assert lines["feat/y"].endswith(" sessions=none")

    res = runner.invoke(app, ["isolation", "status", "--repo", str(repo), "--session", "s1"])
    assert res.exit_code == 0, res.output
    branches = [ln.split(":")[0] for ln in res.stdout.splitlines() if ln.startswith("feat/")]
    assert branches == ["feat/x"]


# (f) up --session --harness attaches in the same call
def test_up_with_session_attaches(tmp_path: Path, repo: Path, fake_run: list) -> None:
    res = runner.invoke(
        app,
        [
            "isolation",
            "up",
            "--repo",
            str(repo),
            "--branch",
            "feat/z",
            "--session",
            "s9",
            "--harness",
            "claude",
        ],
    )
    assert res.exit_code == 0, res.output
    state = load_state(repo, "feat/z")
    assert state is not None
    assert state.sessions[0].session_id == "s9"
    assert state.sessions[0].harness == "claude"
    assert _index(tmp_path, "s9").is_file()


# (g) down warns about still-attached sessions and unbinds them
def test_down_warns_and_unbinds(tmp_path: Path, repo: Path, fake_run: list) -> None:
    res = runner.invoke(
        app,
        ["isolation", "up", "--repo", str(repo), "--branch", "feat/z", "--session", "s9"],
    )
    assert res.exit_code == 0, res.output
    assert _index(tmp_path, "s9").is_file()

    res = runner.invoke(app, ["isolation", "down", "--repo", str(repo), "--branch", "feat/z"])
    assert res.exit_code == 0, res.output
    assert "warning: sessions still attached: s9" in res.stderr
    assert not _index(tmp_path, "s9").exists()
    assert load_state(repo, "feat/z") is None


def test_down_refused_by_open_pr_keeps_bindings(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §5.A: down detaches the sessions of the workspace it REMOVES. A
    refused teardown (open PR, no --force) keeps the workspace — and its
    bindings, so a later status/attach still sees the session."""

    def run(argv, cwd=None, check=False, capture=True):
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, check=check, capture_output=True, text=True)
        out = '{"state": "OPEN", "url": "u"}' if argv[0] == "gh" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(isolation_cmd, "_runner", run)
    runner.invoke(
        app,
        ["isolation", "up", "--repo", str(repo), "--branch", "feat/z", "--session", "s9"],
    )
    res = runner.invoke(app, ["isolation", "down", "--repo", str(repo), "--branch", "feat/z"])
    assert res.exit_code == 2
    assert "still open" in res.output
    state = load_state(repo, "feat/z")
    assert state is not None and [b.session_id for b in state.sessions] == ["s9"]
    assert _index(tmp_path, "s9").is_file()


def test_down_with_own_session_does_not_warn(tmp_path: Path, repo: Path, fake_run: list) -> None:
    runner.invoke(
        app,
        ["isolation", "up", "--repo", str(repo), "--branch", "feat/z", "--session", "s9"],
    )
    res = runner.invoke(
        app,
        ["isolation", "down", "--repo", str(repo), "--branch", "feat/z", "--session", "s9"],
    )
    assert res.exit_code == 0, res.output
    assert "still attached" not in res.stderr
    assert not _index(tmp_path, "s9").exists()


# ---- phase 4: up --print-path / down --worktree (spec 2026-09-04 §5.A, §5.B.3/4)


def _last_nonempty_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# (a) --print-path: the LAST non-empty stdout line is the worktree path; the
# human "isolation up:" line moves to stderr.
def test_up_print_path_last_stdout_line_is_worktree(
    tmp_path: Path, repo: Path, fake_run: list
) -> None:
    res = runner.invoke(
        app, ["isolation", "up", "--repo", str(repo), "--branch", "feat/p", "--print-path"]
    )
    assert res.exit_code == 0, res.output
    state = load_state(repo, "feat/p")
    assert state is not None
    assert _last_nonempty_line(res.stdout) == str(state.worktree)
    assert Path(_last_nonempty_line(res.stdout)).is_dir()
    assert "isolation up:" not in res.stdout
    assert "isolation up:" in res.stderr


# (b) idempotent for an existing branch+worktree: same path, exit 0
def test_up_print_path_twice_same_path(tmp_path: Path, repo: Path, fake_run: list) -> None:
    argv = ["isolation", "up", "--repo", str(repo), "--branch", "feat/p", "--print-path"]
    first = runner.invoke(app, argv)
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, argv)
    assert second.exit_code == 0, second.output
    assert _last_nonempty_line(second.stdout) == _last_nonempty_line(first.stdout)


# (c) down --worktree <path> from a cwd OUTSIDE the repo
def test_down_by_worktree_path_from_outside_repo(
    tmp_path: Path, repo: Path, fake_run: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = runner.invoke(
        app, ["isolation", "up", "--repo", str(repo), "--branch", "feat/p", "--print-path"]
    )
    assert res.exit_code == 0, res.output
    wt = _last_nonempty_line(res.stdout)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    res = runner.invoke(app, ["isolation", "down", "--worktree", wt])
    assert res.exit_code == 0, res.output
    assert load_state(repo, "feat/p") is None
    assert not Path(wt).exists()


# (d) down --worktree on a path with no workspace → exit 2, never a traceback
def test_down_by_worktree_path_unknown_exits_2(
    tmp_path: Path, repo: Path, fake_run: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["isolation", "down", "--worktree", "/nonexistent/wt"])
    assert res.exit_code == 2, res.output
    assert "no isolation workspace at" in res.output
