"""fr.isolation.sessions — session <-> workspace bindings (spec 2026-09-04 §5.A).

State (IsolationState.sessions) is the source of truth; the per-session index
under $FR_SESSIONS_DIR is derived. Decision d1: never the pipeline sentinel.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fr.isolation import sessions
from fr.isolation.types import IsolationError, IsolationState, load_state, save_state


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("FR_SESSIONS_DIR", str(tmp_path / "sessions"))
    return h


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
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


def _index_file(tmp_path: Path, session_id: str) -> Path:
    return tmp_path / "sessions" / f"{session_id}.json"


# (a) pre-feature state files keep loading
def test_state_without_sessions_key_loads_empty(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "repo_root": str(tmp_path / "r"),
            "branch": "feat/old",
            "worktree": str(tmp_path / "w"),
            "profile": "dev",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    assert IsolationState.model_validate_json(raw).sessions == []


# (b) attach writes state + index
def test_attach_binds_and_writes_index(tmp_path: Path, repo: Path) -> None:
    ws = _workspace(tmp_path, repo, "feat/x")
    state = sessions.attach(repo, "feat/x", "sess-1", harness="claude")
    assert len(state.sessions) == 1
    b = state.sessions[0]
    assert b.session_id == "sess-1"
    assert b.harness == "claude"
    assert "T" in b.attached_at and b.attached_at.endswith("+00:00")
    loaded = load_state(repo, "feat/x")
    assert loaded is not None and loaded.sessions == state.sessions
    assert sessions.read_session_index("sess-1") == {
        "session_id": "sess-1",
        "harness": "claude",
        "repo_root": str(repo),
        "branch": "feat/x",
        "worktree": str(ws.worktree),
        "profile": "host",
        "attached_at": b.attached_at,
    }


# (c) idempotent
def test_attach_is_idempotent(tmp_path: Path, repo: Path) -> None:
    _workspace(tmp_path, repo, "feat/x")
    first = sessions.attach(repo, "feat/x", "sess-1")
    second = sessions.attach(repo, "feat/x", "sess-1")
    assert [b.session_id for b in second.sessions] == ["sess-1"]
    assert second.sessions[0].attached_at >= first.sessions[0].attached_at
    loaded = load_state(repo, "feat/x")
    assert loaded is not None and len(loaded.sessions) == 1


# (d) single binding per session — moving it drops the old one
def test_attach_moves_binding_between_branches(tmp_path: Path, repo: Path) -> None:
    _workspace(tmp_path, repo, "feat/x")
    _workspace(tmp_path, repo, "feat/y")
    sessions.attach(repo, "feat/x", "sess-1")
    sessions.attach(repo, "feat/y", "sess-1")
    x = load_state(repo, "feat/x")
    y = load_state(repo, "feat/y")
    assert x is not None and x.sessions == []
    assert y is not None and [b.session_id for b in y.sessions] == ["sess-1"]
    idx = sessions.read_session_index("sess-1")
    assert idx is not None and idx["branch"] == "feat/y"


# (e) detach — idempotent, removes index
def test_detach_removes_binding_and_index(tmp_path: Path, repo: Path) -> None:
    _workspace(tmp_path, repo, "feat/y")
    sessions.attach(repo, "feat/y", "sess-1")
    assert sessions.detach("sess-1") == ["feat/y"]
    y = load_state(repo, "feat/y")
    assert y is not None and y.sessions == []
    assert not _index_file(tmp_path, "sess-1").exists()
    assert sessions.detach("sess-1") == []


# (f) detach_all — down's helper
def test_detach_all_clears_every_session(tmp_path: Path, repo: Path) -> None:
    _workspace(tmp_path, repo, "feat/x")
    sessions.attach(repo, "feat/x", "sess-1")
    state = sessions.attach(repo, "feat/x", "sess-2")
    assert sorted(sessions.detach_all(state)) == ["sess-1", "sess-2"]
    assert not _index_file(tmp_path, "sess-1").exists()
    assert not _index_file(tmp_path, "sess-2").exists()
    x = load_state(repo, "feat/x")
    assert x is not None and x.sessions == []


# (g) stale index detection — pure classification
def test_stale_session_indexes(tmp_path: Path, repo: Path) -> None:
    gone = _workspace(tmp_path, repo, "feat/gone")
    _workspace(tmp_path, repo, "feat/unlisted")
    _workspace(tmp_path, repo, "feat/ok")
    sessions.attach(repo, "feat/gone", "sess-gone")
    sessions.attach(repo, "feat/unlisted", "sess-unlisted")
    sessions.attach(repo, "feat/ok", "sess-ok")
    shutil.rmtree(gone.worktree)
    # state no longer lists sess-unlisted, but its index file survives
    unlisted = load_state(repo, "feat/unlisted")
    assert unlisted is not None
    save_state(unlisted.model_copy(update={"sessions": []}))

    stale = sessions.stale_session_indexes()
    names = sorted(p.name for p, _ in stale)
    assert names == ["sess-gone.json", "sess-unlisted.json"]
    assert all(isinstance(d, dict) for _, d in stale)


# (h) index path refuses traversal
@pytest.mark.parametrize("bad", ["../x", "", "a/b", ".", ".."])
def test_session_index_path_rejects_bad_ids(home: Path, bad: str) -> None:
    with pytest.raises(IsolationError):
        sessions.session_index_path(bad)
