"""Session <-> workspace bindings — traceability (spec 2026-09-04 §5.A).

State (IsolationState.sessions, per repo in the git common dir) is the
source of truth. ~/.cache/fr/sessions/<session_id>.json is an INDEX so a
status line answers "which workspace is this session in" from one small
file. The pipeline sentinel dir is untouched: its PRESENCE arms the Bash
guard, so bindings must not live there (decision d1 refinement).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .types import (
    IsolationError,
    IsolationState,
    SessionBinding,
    _home,
    list_states,
    load_state,
    save_state,
    state_path,
)


def sessions_dir() -> Path:
    return Path(os.environ.get("FR_SESSIONS_DIR", str(_home() / ".cache" / "fr" / "sessions")))


def session_index_path(session_id: str) -> Path:
    if not session_id or "/" in session_id or session_id in (".", ".."):
        raise IsolationError(f"invalid session id {session_id!r}")
    return sessions_dir() / f"{session_id}.json"


def read_session_index(session_id: str) -> dict[str, Any] | None:
    p = session_index_path(session_id)
    if not p.is_file():
        return None
    try:
        return cast("dict[str, Any]", json.loads(p.read_text()))
    except json.JSONDecodeError:
        return None


def _write_index(state: IsolationState, b: SessionBinding) -> Path:
    p = session_index_path(b.session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "session_id": b.session_id,
                "harness": b.harness,
                "repo_root": str(state.repo_root),
                "branch": state.branch,
                "worktree": str(state.worktree),
                "profile": state.profile,
                "attached_at": b.attached_at,
            },
            indent=2,
        )
        + "\n"
    )
    return p


def _without(state: IsolationState, session_id: str) -> IsolationState:
    return state.model_copy(
        update={"sessions": [b for b in state.sessions if b.session_id != session_id]}
    )


def _drop_binding(repo_root: Path, branch: str, session_id: str) -> bool:
    state = load_state(repo_root, branch)
    if state is None or all(b.session_id != session_id for b in state.sessions):
        return False
    save_state(_without(state, session_id))
    return True


def attach(
    repo_root: Path, branch: str, session_id: str, harness: str = "unknown"
) -> IsolationState:
    """Bind `session_id` to the workspace (idempotent). A session holds at most ONE
    binding: any previous one — in this repo or, via the index, another — is dropped."""
    state = load_state(repo_root, branch)
    if state is None:
        raise IsolationError(
            f"no isolation workspace for branch {branch!r} — run `fr isolation up` first."
        )
    prev = read_session_index(session_id)
    if prev and (Path(prev.get("repo_root", "")) != repo_root or prev.get("branch") != branch):
        _drop_binding(Path(prev["repo_root"]), prev["branch"], session_id)
    for other in list_states(repo_root):
        if other.branch != branch:
            _drop_binding(repo_root, other.branch, session_id)
    b = SessionBinding(
        session_id=session_id, harness=harness, attached_at=datetime.now(UTC).isoformat()
    )
    base = _without(state, session_id)
    new = base.model_copy(update={"sessions": [*base.sessions, b]})
    save_state(new)
    _write_index(new, b)
    return new


def detach(session_id: str, repo_root: Path | None = None, branch: str | None = None) -> list[str]:
    """Unbind the session wherever it is bound (index + optional explicit target).
    Returns the branches it was detached from; [] when it held no binding."""
    targets: list[tuple[Path, str]] = []
    idx = read_session_index(session_id)
    if idx and idx.get("repo_root") and idx.get("branch"):
        targets.append((Path(idx["repo_root"]), idx["branch"]))
    if repo_root is not None:
        branches = [branch] if branch else [s.branch for s in list_states(repo_root)]
        targets.extend((repo_root, b) for b in branches)
    detached: list[str] = []
    for r, b in dict.fromkeys(targets):
        if _drop_binding(r, b, session_id):
            detached.append(b)
    session_index_path(session_id).unlink(missing_ok=True)
    return detached


def detach_all(state: IsolationState) -> list[str]:
    """`down`: unbind every session of this workspace and drop their index files.

    Safe to call AFTER the workspace was torn down: the state file is only
    rewritten when it still exists, so a retired workspace is never resurrected
    with an empty `sessions` list (the CLI detaches on successful `down` only —
    a refused `down` keeps its bindings, spec §5.A "the workspace it removes")."""
    ids = [b.session_id for b in state.sessions]
    for sid in ids:
        idx = read_session_index(sid)
        if (
            idx
            and idx.get("branch") == state.branch
            and Path(idx.get("repo_root", "")) == state.repo_root
        ):
            session_index_path(sid).unlink(missing_ok=True)
    if ids and state_path(state.repo_root, state.branch).is_file():
        save_state(state.model_copy(update={"sessions": []}))
    return ids


def stale_session_indexes() -> list[tuple[Path, dict[str, Any]]]:
    """Index files whose worktree is gone, whose state no longer lists the
    session, or which do not parse. Pure — gc decides what to do."""
    d = sessions_dir()
    if not d.is_dir():
        return []
    stale: list[tuple[Path, dict[str, Any]]] = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            stale.append((p, {}))
            continue
        wt = Path(data.get("worktree", ""))
        state = (
            load_state(Path(data["repo_root"]), data["branch"])
            if data.get("repo_root") and data.get("branch")
            else None
        )
        listed = state is not None and any(
            b.session_id == data.get("session_id") for b in state.sessions
        )
        if not wt.is_dir() or not listed:
            stale.append((p, data))
    return stale
