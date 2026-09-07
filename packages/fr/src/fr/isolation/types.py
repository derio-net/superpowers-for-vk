"""State, profiles, and the Target protocol for fr isolation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, Field


def _warn_legacy(what: str, path: Path) -> None:
    """Loud dual-read fallback warning (#272). Removed one minor after 3.1."""
    print(f"[fr] WARNING: legacy {what} at {path} — run `fr init migrate`", file=sys.stderr)


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


class IsolationError(Exception):
    """User-facing isolation failure; CLI maps it to exit 2."""


class SessionBinding(BaseModel):
    """One agent session attached to a workspace (spec 2026-09-04 §5.A)."""

    session_id: str
    harness: str = "unknown"  # claude | hermes | opencode | unknown
    attached_at: str  # ISO-8601 UTC

    model_config = {"frozen": True}


class IsolationState(BaseModel):
    """Everything needed to re-address an isolation workspace later."""

    repo_root: Path
    branch: str
    worktree: Path
    profile: str
    created_at: str
    # Sessions bound to this workspace (spec 2026-09-04 §5.A). Default keeps
    # pre-feature state files loadable; frozen models still `model_copy(update=)`.
    sessions: list[SessionBinding] = Field(default_factory=list)

    model_config = {"frozen": True}


def _sanitize(branch: str) -> str:
    return branch.replace("/", "__")


def _git_common_dir(repo_root: Path) -> Path:
    """Shared .git dir, resolved for main checkouts AND linked worktrees.

    In a linked worktree <repo>/.git is a gitfile (a `gitdir:` pointer), not a
    dir; `--git-common-dir` returns the real shared dir (<main>/.git) that all
    worktrees of the repo share — the correct key for isolation state (state is
    repo+branch, not per-worktree). For a main checkout it returns ".git", so
    this is byte-identical to the legacy literal there. #292

    Resolves its own input so the result is caller-independent: an unresolved
    vs resolved (or symlinked, e.g. macOS /tmp -> /private/tmp) repo_root key
    to the SAME state dir, instead of two string-distinct paths to one inode.
    """
    repo_root = Path(repo_root).resolve()
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Not a git repo (e.g. a bare tmp path in a unit test) — degrade to the
        # legacy literal; there is no worktree to be blind to here.
        return repo_root / ".git"
    p = Path(out)
    return p if p.is_absolute() else (repo_root / p)


def repo_cache_name(repo_root: Path) -> str:
    """Folder under ~/.cache/fr/worktrees for this repo: the MAIN checkout's
    basename, resolved through the git common dir, so `up` from inside a
    linked worktree (native agent worktree, nested fr worktree) files under the
    repo — never under `agent-…` or a branch slug (spec 2026-09-04 §5.C).

    Bare / `--separate-git-dir` layouts (common dir not named ".git") and
    non-git paths fall back to the basename of `repo_root` itself."""
    common = _git_common_dir(repo_root)
    return common.parent.name if common.name == ".git" else Path(repo_root).name


def state_dir(repo_root: Path) -> Path:
    return _git_common_dir(repo_root) / "fr" / "isolation"


def _legacy_state_dir(repo_root: Path) -> Path:
    return _git_common_dir(repo_root) / "vk" / "isolation"


def state_path(repo_root: Path, branch: str) -> Path:
    return state_dir(repo_root) / f"{_sanitize(branch)}.json"


def save_state(state: IsolationState) -> Path:
    p = state_path(state.repo_root, state.branch)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.model_dump_json(indent=2) + "\n")
    return p


def delete_state(repo_root: Path, branch: str) -> None:
    """Unlink fr and legacy copies — down() must retire both spellings."""
    name = f"{_sanitize(branch)}.json"
    (state_dir(repo_root) / name).unlink(missing_ok=True)
    (_legacy_state_dir(repo_root) / name).unlink(missing_ok=True)


def load_state(repo_root: Path, branch: str) -> IsolationState | None:
    p = state_path(repo_root, branch)
    if not p.is_file():
        legacy = _legacy_state_dir(repo_root) / p.name
        if not legacy.is_file():
            return None
        _warn_legacy("isolation state", legacy)
        p = legacy
    return IsolationState.model_validate_json(p.read_text())


def list_states(repo_root: Path) -> list[IsolationState]:
    files: dict[str, Path] = {}
    legacy_dir = _legacy_state_dir(repo_root)
    if legacy_dir.is_dir():
        for f in legacy_dir.glob("*.json"):
            files[f.name] = f
            _warn_legacy("isolation state", f)
    d = state_dir(repo_root)
    if d.is_dir():
        for f in d.glob("*.json"):
            files[f.name] = f  # fr copy wins for the same branch
    return [IsolationState.model_validate_json(f.read_text()) for _, f in sorted(files.items())]


def sentinel_dir() -> Path:
    """Directory holding pipeline session sentinels.

    Shared contract with the two bash hooks: fr-pipeline-sentinel.sh writes one
    `<session_id>.json` here per active fr-goal / fr-brainstorming / fr-execute
    session ({"repo_root": ...}); fr-isolation-guard.sh reads it to gate
    base-repo commands. `$FR_SENTINEL_DIR` overrides the default (both hooks
    honour the same env var).
    """
    return Path(os.environ.get("FR_SENTINEL_DIR", str(_home() / ".cache" / "fr" / "sentinels")))


def clear_repo_sentinels(repo_root: Path) -> int:
    """Remove pipeline sentinels pointing at `repo_root`; return the count.

    The explicit "drop session state" lever behind `fr isolation down --all`
    (#341 Task 2A). Foreign-repo sentinels are left alone; a malformed /
    unreadable file is skipped, never removed (it isn't ours to interpret). The
    guard's own self-heal (fail open + clear when no worktree survives) is the
    lazy backstop; this is the eager path.
    """
    d = sentinel_dir()
    if not d.is_dir():
        return 0
    target = str(Path(repo_root).resolve())
    removed = 0
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        root = data.get("repo_root") if isinstance(data, dict) else None
        if root and str(Path(root).resolve()) == target:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def discover_profiles(repo_root: Path) -> list[str]:
    base = repo_root / ".devcontainer"
    return sorted(p.parent.name for p in base.glob("*/devcontainer.json"))


def profiles_config(repo_root: Path) -> dict[str, Any]:
    base = repo_root / ".devcontainer"
    for name in ("fr-profiles.yaml", "vk-profiles.yaml"):
        cfg = base / name
        if cfg.is_file():
            if name.startswith("vk-"):
                _warn_legacy("vk-profiles.yaml", cfg)
            return yaml.safe_load(cfg.read_text()) or {}
    return {}


def secrets_env_file(repo_name: str, profile: str) -> Path:
    """Canonical (fr) host-side secrets env-file for a repo/profile."""
    return _home() / ".config" / "fr" / "secrets" / repo_name / f"{profile}.env"


def harden_secret_file(env_file: Path) -> None:
    """Make a host secrets env-file private: 0600 on the file, 0700 on its dir
    chain up to (and including) the ``~/.config/<store>`` secrets root.

    Called after every scaffold / ensure so the store is never world-readable —
    a 0644 store under 0755 dirs once exposed a live cluster-admin kube token —
    and so pre-existing loose perms self-heal on the next isolation run. Errors
    (e.g. a not-yet-created file) are swallowed: hardening is best-effort.
    """
    if env_file.is_file():
        env_file.chmod(0o600)
    d = env_file.parent
    for _ in range(4):  # <repo>/, secrets/, <store>/ — bounded; never past .config
        try:
            d.chmod(0o700)
        except OSError:
            pass
        if d.parent.name == ".config" or d.parent == d:
            break
        d = d.parent


def resolve_profile(repo_root: Path, name: str | None) -> str:
    """Resolve the requested (or default) profile, or explain how to get one.

    Hard requirement by design: no devcontainer profile → refuse with a
    pointer at fr-init. There is no unisolated fallback.
    """
    available = discover_profiles(repo_root)
    if not available:
        raise IsolationError(
            "no devcontainer profiles found (.devcontainer/<profile>/devcontainer.json). "
            "Run the fr-init skill to scaffold one — isolation never degrades to unisolated."
        )
    if name is None:
        default = profiles_config(repo_root).get("default")
        if default and default in available:
            return str(default)
        if len(available) == 1:
            return available[0]
        raise IsolationError(
            f"multiple profiles ({', '.join(available)}) and no default in "
            ".devcontainer/fr-profiles.yaml — pass --profile or set a default via fr-init."
        )
    if name not in available:
        raise IsolationError(f"unknown profile {name!r}; available: {', '.join(available)}")
    return name


class Target(Protocol):
    """Pluggable isolation backend (local worktree+devcontainer now; remote later)."""

    def up(
        self,
        profile: str | None,
        branch: str,
        path: Path | None = None,
        base: str | None = None,
        no_fetch: bool = False,
    ) -> IsolationState: ...

    def exec(self, state: IsolationState, argv: list[str]) -> int: ...

    def restart(self, state: IsolationState, force: bool = False) -> str: ...

    def stats(self, state: IsolationState) -> dict[str, str] | None: ...

    def status(self, state: IsolationState) -> dict[str, Any]: ...

    def down(self, state: IsolationState, force: bool = False) -> None: ...
