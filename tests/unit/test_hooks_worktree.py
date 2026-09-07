"""fr-worktree-create.sh (WorktreeCreate) and fr-worktree-remove.sh (WorktreeRemove).

Spec 2026-09-04 §5.B.3 / §5.B.4: a native Claude session worktree in an
fr-enabled repo becomes an fr workspace (``fr isolation up --print-path``);
subagent worktrees (``agent-*``) and non-fr repos keep Claude's default shape
(``<repo>/.claude/worktrees/<name>``, detached). The hooks are exercised via
subprocess with hook-protocol JSON on stdin and a stub ``fr`` on PATH that
records its argv (and the FR_ISOLATION_TARGET it saw) and fakes a worktree
path, so no real fr state is ever touched. git runs for real.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="hook scripts require jq")

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "plugins" / "super-fr" / "hooks"
CREATE = HOOKS_DIR / "fr-worktree-create.sh"
REMOVE = HOOKS_DIR / "fr-worktree-remove.sh"

STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FR_STUB_LOG"
printf '%s\n' "${FR_ISOLATION_TARGET:-}" >> "$FR_STUB_LOG.env"
b=""
for ((i=1;i<=$#;i++)); do [ "${!i}" = "--branch" ] && j=$((i+1)) && b="${!j}"; done
d="$HOME/.cache/fr/worktrees/stub/${b//\//__}"
mkdir -p "$d"
echo "stdout noise"
echo "isolation up: worktree=$d" >&2
echo "$d"
"""


@pytest.fixture
def stub_fr(tmp_path: Path) -> dict[str, str]:
    """Recording ``fr`` stub first on PATH; returns the env to run hooks with.

    The stub CREATES the path it is asked for and prints it as its last stdout
    line (after some noise), mirroring ``fr isolation up --print-path``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "fr"
    stub.write_text(STUB)
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "FR_STUB_LOG": str(tmp_path / "fr.log"),
        "HOME": str(tmp_path),
    }
    env.pop("FR_ISOLATION_TARGET", None)  # case (f) depends on it being unset
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An fr-enabled repo: one commit, docs/superpowers/plans/ committed."""
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "docs" / "superpowers" / "plans").mkdir(parents=True)
    (r / "docs" / "superpowers" / "plans" / ".keep").write_text("")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r.resolve()


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    """A git repo that is NOT fr-enabled (no plans dir, no devcontainer)."""
    r = tmp_path / "plain"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "x").write_text("x")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r.resolve()


def run_hook(script: Path, payload: dict, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def create_payload(name: str, cwd: Path | str) -> dict:
    return {
        "session_id": "sess-1",
        "cwd": str(cwd),
        "hook_event_name": "WorktreeCreate",
        "name": name,
    }


def remove_payload(cwd: Path | str, **fields: object) -> dict:
    return {
        "session_id": "sess-1",
        "cwd": str(cwd),
        "hook_event_name": "WorktreeRemove",
        **fields,
    }


def logged(env: dict[str, str]) -> list[str]:
    log = Path(env["FR_STUB_LOG"])
    if not log.exists():
        return []
    return log.read_text().splitlines()


def logged_env(env: dict[str, str]) -> list[str]:
    log = Path(env["FR_STUB_LOG"] + ".env")
    if not log.exists():
        return []
    return log.read_text().splitlines()


def last_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def worktrees(repo: Path) -> list[str]:
    out = _git(repo, "worktree", "list", "--porcelain")
    return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]


class TestWorktreeCreate:
    # (a) session worktree in an fr-enabled repo -> fr isolation up --print-path
    def test_session_worktree_becomes_fr_workspace(
        self, tmp_path: Path, repo: Path, stub_fr: dict[str, str]
    ) -> None:
        result = run_hook(CREATE, create_payload("feature-auth", repo), stub_fr)
        assert result.returncode == 0, result.stderr
        assert last_line(result.stdout) == str(
            tmp_path / ".cache" / "fr" / "worktrees" / "stub" / "wt__feature-auth"
        )
        assert logged(stub_fr) == [
            f"isolation up --repo {repo} --branch wt/feature-auth "
            "--session sess-1 --harness claude --print-path"
        ]

    # (b) a name that already contains a slash is used as the branch verbatim
    def test_name_with_slash_is_branch_verbatim(
        self, tmp_path: Path, repo: Path, stub_fr: dict[str, str]
    ) -> None:
        result = run_hook(CREATE, create_payload("feat/x", repo), stub_fr)
        assert result.returncode == 0, result.stderr
        assert last_line(result.stdout) == str(
            tmp_path / ".cache" / "fr" / "worktrees" / "stub" / "feat__x"
        )
        assert "--branch feat/x " in logged(stub_fr)[0]

    # (c) agent-* -> Claude default shape, real git worktree, idempotent
    def test_agent_worktree_mimics_claude_default(
        self, repo: Path, stub_fr: dict[str, str]
    ) -> None:
        result = run_hook(CREATE, create_payload("agent-abc123", repo), stub_fr)
        assert result.returncode == 0, result.stderr
        expected = repo / ".claude" / "worktrees" / "agent-abc123"
        assert last_line(result.stdout) == str(expected)
        assert expected.is_dir()
        assert str(expected) in worktrees(repo)
        assert logged(stub_fr) == []
        again = run_hook(CREATE, create_payload("agent-abc123", repo), stub_fr)
        assert again.returncode == 0, again.stderr
        assert last_line(again.stdout) == str(expected)
        assert logged(stub_fr) == []

    # (d) non-fr repo -> default shape even for a session worktree name
    def test_non_fr_repo_mimics_default(self, plain_repo: Path, stub_fr: dict[str, str]) -> None:
        result = run_hook(CREATE, create_payload("feature-auth", plain_repo), stub_fr)
        assert result.returncode == 0, result.stderr
        expected = plain_repo / ".claude" / "worktrees" / "feature-auth"
        assert last_line(result.stdout) == str(expected)
        assert expected.is_dir()
        assert logged(stub_fr) == []

    # (e) cwd not a git repo -> exit 1 (a registered hook cannot decline)
    def test_non_git_cwd_exits_1(self, tmp_path: Path, stub_fr: dict[str, str]) -> None:
        d = tmp_path / "nogit"
        d.mkdir()
        result = run_hook(CREATE, create_payload("feature-auth", d), stub_fr)
        assert result.returncode == 1
        assert "not a git repo" in result.stderr
        assert logged(stub_fr) == []

    # (f) fr-enabled via plans dir, no profile, FR_ISOLATION_TARGET unset ->
    #     the hook exports host-worktree mode for this call
    def test_no_profile_exports_host_worktree_mode(
        self, repo: Path, stub_fr: dict[str, str]
    ) -> None:
        assert "FR_ISOLATION_TARGET" not in stub_fr
        result = run_hook(CREATE, create_payload("feature-auth", repo), stub_fr)
        assert result.returncode == 0, result.stderr
        assert len(logged(stub_fr)) == 1
        assert logged_env(stub_fr) == ["worktree"]
        assert "host-worktree mode" in result.stderr

    def test_profile_present_leaves_target_alone(self, repo: Path, stub_fr: dict[str, str]) -> None:
        (repo / ".devcontainer" / "dev").mkdir(parents=True)
        result = run_hook(CREATE, create_payload("feature-auth", repo), stub_fr)
        assert result.returncode == 0, result.stderr
        assert logged_env(stub_fr) == [""]

    def test_preset_target_is_respected(self, repo: Path, stub_fr: dict[str, str]) -> None:
        env = {**stub_fr, "FR_ISOLATION_TARGET": "devcontainer"}
        result = run_hook(CREATE, create_payload("feature-auth", repo), env)
        assert result.returncode == 0, result.stderr
        assert logged_env(env) == ["devcontainer"]


class TestWorktreeRemove:
    # (g) agent-* path -> plain git worktree removal, fr never called
    def test_agent_worktree_removed_with_git(self, repo: Path, stub_fr: dict[str, str]) -> None:
        wt = repo / ".claude" / "worktrees" / "agent-abc123"
        wt.parent.mkdir(parents=True)
        _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")
        assert str(wt) in worktrees(repo)
        result = run_hook(REMOVE, remove_payload(repo, worktree_path=str(wt)), stub_fr)
        assert result.returncode == 0, result.stderr
        assert not wt.exists()
        assert str(wt) not in worktrees(repo)
        assert logged(stub_fr) == []

    # (h) any other path -> fr isolation down --worktree <path>
    def test_other_path_delegates_to_fr_down(
        self, tmp_path: Path, repo: Path, stub_fr: dict[str, str]
    ) -> None:
        p = tmp_path / "cache" / "wt"
        result = run_hook(REMOVE, remove_payload(repo, worktree_path=str(p)), stub_fr)
        assert result.returncode == 0, result.stderr
        assert logged(stub_fr) == [f"isolation down --worktree {p}"]

    # (i) missing worktree_path -> exit 0, nothing happens
    def test_missing_worktree_path_is_noop(self, repo: Path, stub_fr: dict[str, str]) -> None:
        result = run_hook(REMOVE, remove_payload(repo), stub_fr)
        assert result.returncode == 0, result.stderr
        assert logged(stub_fr) == []

    def test_fr_refusal_is_reported_but_exit_0(
        self, tmp_path: Path, repo: Path, stub_fr: dict[str, str]
    ) -> None:
        """A refused down (open PR) keeps the workspace: the hook says so on
        stderr and still exits 0 (spec §5.B.4)."""
        stub = Path(stub_fr["PATH"].split(":")[0]) / "fr"
        stub.write_text(STUB + "exit 2\n")
        p = tmp_path / "cache" / "wt"
        result = run_hook(REMOVE, remove_payload(repo, worktree_path=str(p)), stub_fr)
        assert result.returncode == 0
        assert "refused" in result.stderr and str(p) in result.stderr
        assert logged(stub_fr) == [f"isolation down --worktree {p}"]
