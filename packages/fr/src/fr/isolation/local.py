"""LocalWorktreeDevcontainerTarget — worktree + devcontainer over a Runner seam.

Every external call (git, devcontainer, docker, gh) goes through `runner` so
the lifecycle is unit-testable without Docker. The devcontainer CLI labels
containers with `devcontainer.local_folder=<workspace>`, which is how status
and down re-find the container.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, ClassVar

from fr._hosts import detect_backend
from fr.isolation.types import (
    IsolationError,
    IsolationState,
    _git_common_dir,
    _warn_legacy,
    delete_state,
    harden_secret_file,
    list_states,
    repo_cache_name,
    resolve_profile,
    save_state,
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def subprocess_runner(
    argv: list[str], cwd: Path | None = None, check: bool = False, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    """capture=False inherits stdio — exec passthrough must stream the
    container's output live (long builds/test runs), not swallow it."""
    return subprocess.run(argv, cwd=cwd, check=check, capture_output=capture, text=True)


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


# --- opportunistic gc spawn seam (#354 Task B) ---
#
# `up`/`down` fire a detached `fr isolation gc` after their own work — the
# primary auto-trigger that bounds the steady-state leak to <=1 workspace with
# no daemon. Routed through an INJECTABLE seam (not the Runner: a fire-and-forget
# Popen is a different shape than subprocess.run) so the non-blocking /
# non-raising contract is testable. The library default is a NO-OP — production
# opts into the real spawn at the CLI boundary, so a Target built directly (every
# unit test, and gc's own sibling teardown Targets) never spawns.
GcSpawner = Callable[[Path], None]


def _detached_gc_spawn(repo_root: Path) -> None:
    """Fire-and-forget `fr isolation gc`: detached (own session), non-blocking,
    output to a rotating-ish log. Any spawn error is swallowed — a caller must
    never fail because a background reap could not start.

    Named with `--repo` and run there rather than inheriting the caller's cwd
    (#423): a `down` fired from inside the worktree it just removed would
    otherwise hand the child a deleted cwd — `Path.cwd()` then raises and the
    sweep dies before classifying anything. It also keeps the child's
    state-record discovery source pointed at the repo the caller was working in.
    """
    try:
        log_path = _home() / ".cache" / "fr" / "isolation-gc.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "a")  # noqa: SIM115 — handed to the child; not ours to close
        subprocess.Popen(
            [sys.executable, "-m", "fr", "isolation", "gc", "--repo", str(repo_root)],
            cwd=str(repo_root) if repo_root.is_dir() else None,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
    except Exception:
        pass


def _noop_gc_spawn(repo_root: Path) -> None:
    """The safe default — no background sweep (see GcSpawner)."""


@dataclass
class MergeVerification:
    """Whether a branch's changes are present on a base ref (#320 close-out)."""

    changed: list[str]  # files the branch changed since its merge-base
    missing: list[str]  # changed files NOT yet present on the base ref
    changes_present: bool


@dataclass
class GcWorkspace:
    """A workspace discovered by the host-wide gc sweep (#354 Task B).

    `state` is None for a true orphan (worktree gone, container found only by
    docker label) or an ambiguous label-less directory with no fr state.
    """

    worktree: Path
    container_id: str | None
    state: IsolationState | None


@dataclass
class GcAction:
    """One workspace's gc verdict + what gc did about it (#354 Task B)."""

    worktree: str
    branch: str | None
    # merged | merged-by-content | open | no-pr | orphan | no-state | dangling-image
    # | empty-repo-dir | stale-session (cache/index hygiene, spec 2026-09-04 §5)
    verdict: str
    # reaped | skipped | warned | reap-failed | would-reap | removed | would-remove
    action: str
    detail: str = ""


def _branch_added_lines(
    run: Runner, repo_root: Path, merge_base: str, branch: str, path: str
) -> list[str]:
    """The non-blank lines the branch ADDED to `path` since `merge_base`.

    Parsed from the branch's own diff (`+` lines, minus the `+++` header).
    Blank / whitespace-only additions are dropped — they carry no identity and
    would trivially "match" almost any base content, weakening containment.
    """
    diff = run(["git", "diff", merge_base, branch, "--", path], cwd=repo_root)
    added: list[str] = []
    for ln in diff.stdout.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            content = ln[1:]
            if content.strip():
                added.append(content)
    return added


def _branch_change_present_in_file(
    run: Runner, repo_root: Path, merge_base: str, branch: str, base_ref: str, path: str
) -> bool:
    """Is the branch's contribution to `path` reflected on `base_ref`?

    Per-line containment (#387): every non-blank line the branch added to `path`
    must appear verbatim in `base_ref:path`. This is robust to a CONCURRENT
    merge that later touches the same file elsewhere — such an edit shifts line
    numbers and diverges whole-file content, but leaves the branch's own added
    lines intact, so whole-file equality false-negatives while containment does
    not.

    Conservative in the missing direction: a file whose branch-side change added
    no identifiable line (a pure deletion / pure line-removal that did NOT land
    byte-identically — it only reaches here because whole-file content already
    differs) reports *not present*, i.e. a safe "STOP and check", never a false
    "verified".
    """
    added = _branch_added_lines(run, repo_root, merge_base, branch, path)
    if not added:
        return False
    show = run(["git", "show", f"{base_ref}:{path}"], cwd=repo_root)
    if show.returncode != 0:
        return False  # path absent on base_ref — the branch's additions cannot be present
    base_lines = set(show.stdout.splitlines())
    return all(line in base_lines for line in added)


def branch_changes_present(
    run: Runner, repo_root: Path, branch: str, base_ref: str
) -> MergeVerification:
    """Are the branch's changes present on `base_ref` (e.g. origin/main)?

    Compares CONTENT, not commit identity / ancestry — so it is correct across
    squash, merge-commit, and rebase merges alike (an ancestry/patch-id check
    would false-negative on squash). Two-stage:

    1. Fast path — a file whose `base_ref` content is byte-identical to the
       branch's is present, full stop (covers the clean squash/rebase/merge
       cases with no further work).
    2. For each file that DIFFERS on `base_ref`, fall back to per-line
       containment (`_branch_change_present_in_file`): the branch's own added
       lines must all be on `base_ref`. This is what distinguishes a genuinely
       un-merged change (the #320 orphan — a commit pushed to the branch after
       the merge shows up as a path whose added lines are absent → missing) from
       a CONCURRENT merge that later edits the same file elsewhere (#387 — the
       branch's added lines are still there, so it is NOT missing even though
       whole-file content diverged).

    Conservative: anything it cannot positively confirm reads as missing (a safe
    "STOP and check", never a false "verified").
    """
    mb = run(["git", "merge-base", base_ref, branch], cwd=repo_root)
    if mb.returncode != 0:
        raise IsolationError(f"no merge-base for {base_ref} and {branch} — unrelated histories?")
    merge_base = mb.stdout.strip()
    names = run(["git", "diff", "--name-only", merge_base, branch], cwd=repo_root)
    changed = [ln for ln in names.stdout.splitlines() if ln]
    if not changed:
        return MergeVerification(changed=[], missing=[], changes_present=True)
    diff = run(
        ["git", "diff", "--name-only", branch, base_ref, "--", *changed],
        cwd=repo_root,
    )
    differing = [ln for ln in diff.stdout.splitlines() if ln]
    missing = [
        path
        for path in differing
        if not _branch_change_present_in_file(run, repo_root, merge_base, branch, base_ref, path)
    ]
    return MergeVerification(changed=changed, missing=missing, changes_present=not missing)


def _realpath(p: Path) -> Path:
    """Symlink-resolved identity for gc's discovery keys. The three sources
    spell the same workspace differently (a docker label is whatever string was
    recorded at `up`; the cache scan builds from `$HOME`; a state record carries
    its own copy), and on macOS `/tmp` is a symlink — so raw path equality would
    classify one workspace twice. Never raises: a path that cannot be resolved
    keys as itself."""
    try:
        return p.resolve()
    except OSError:
        return p


def _main_worktree_root(repo_root: Path) -> Path:
    """The MAIN checkout's toplevel, even when launched from a linked worktree.

    The common dir is <main>/.git; its parent is the main toplevel. Keying
    isolation off the durable main checkout (not the possibly-ephemeral launch
    worktree, e.g. an Agent(isolation:"worktree")) means the persisted state and
    the spawned worktree survive that launch worktree being reaped. No-op for a
    main checkout. #292

    `--separate-git-dir` / non-".git"-named git dirs are out of scope: the
    guard falls back to repo_root (the bind-mount still resolves correctly via
    _git_common_dir; only this normalization is skipped).
    """
    common = _git_common_dir(repo_root)
    return common.parent if common.name == ".git" else repo_root


class LocalWorktreeDevcontainerTarget:
    def __init__(
        self,
        repo_root: Path,
        runner: Runner = subprocess_runner,
        gc_spawner: GcSpawner = _noop_gc_spawn,
    ):
        # resolve() — the mount target must match the realpath git bakes
        # into the worktree's gitdir pointer (symlinked /tmp on macOS etc.).
        # Then normalize to the main toplevel so a worktree-launched run keys
        # off the durable main checkout (#292).
        self.repo_root = _main_worktree_root(Path(repo_root).resolve())
        self.run = runner
        self._gc_spawner = gc_spawner

    # ---------- lifecycle ----------

    def _worktree_up_core(self, branch: str, path: Path | None) -> Path:
        """Shared worktree prelude for the linked-worktree modes: the git-repo
        guard + the default worktree-path computation + parent mkdir. Extracted
        so `HostWorktreeTarget.up` reuses it verbatim instead of duplicating ~15
        lines (isolation host modes). Callers append their own mode-specific
        steps (profile/devcontainer for the local target; none for host)."""
        if not (self.repo_root / ".git").exists():
            raise IsolationError(
                f"{self.repo_root} is not a git repo — fr isolation only runs inside one."
            )
        # Keyed on the MAIN checkout's basename (spec 2026-09-04 §5.C): an `up`
        # from inside a linked worktree files under the repo, never `agent-…`.
        worktree = path or (
            _home()
            / ".cache"
            / "fr"
            / "worktrees"
            / repo_cache_name(self.repo_root)
            / branch.replace("/", "__")
        )
        worktree.parent.mkdir(parents=True, exist_ok=True)
        return worktree

    def up(
        self,
        profile: str | None,
        branch: str,
        path: Path | None = None,
        base: str | None = None,
        no_fetch: bool = False,
    ) -> IsolationState:
        worktree = self._worktree_up_core(branch, path)
        name = resolve_profile(self.repo_root, profile)
        self._git_worktree_add(worktree, branch, base=base, no_fetch=no_fetch)

        config = worktree / ".devcontainer" / name / "devcontainer.json"
        # super-fr#299 part 2: the worktree is cut from the committed tree. If
        # the profile exists only as an UNCOMMITTED file in the base repo, the
        # worktree won't have it — explain the fix instead of letting
        # `devcontainer up` fail with a cryptic "config not found". Gate on the
        # base copy being GENUINELY uncommitted (porcelain non-empty): a profile
        # that is committed but merely absent on an older target branch is a
        # different situation, so we must not misreport it as "not committed".
        if not config.exists():
            rel = f".devcontainer/{name}/devcontainer.json"
            base_status = self.run(
                ["git", "-C", str(self.repo_root), "status", "--porcelain", "--", rel]
            )
            if base_status.stdout.strip():
                raise IsolationError(
                    f"profile {name!r} is written in the base repo but not committed, so the "
                    f"worktree can't see it — run `fr init scaffold --profile {name}` (which now "
                    "commits) or commit .devcontainer/ yourself, then retry `fr isolation up`."
                )
        self._ensure_mounted_env_file(config)
        # Resolve the shared common dir, not <repo_root>/.git: correct even if
        # repo_root is a worktree (a gitfile), independent of normalization (#292).
        git_dir = _git_common_dir(self.repo_root)
        result = self.run(
            [
                "devcontainer",
                "up",
                f"--workspace-folder={worktree}",
                f"--config={config}",
                f"--mount=type=bind,source={git_dir},target={git_dir}",
            ],
            cwd=worktree,
        )
        if result.returncode != 0:
            raise IsolationError(f"devcontainer up failed: {result.stderr or result.stdout}")

        state = IsolationState(
            repo_root=self.repo_root,
            branch=branch,
            worktree=worktree,
            profile=name,
            created_at=datetime.now(UTC).isoformat(),
        )
        save_state(state)
        self._write_isolation_marker(worktree, branch)
        self._spawn_gc()
        return state

    def exec(self, state: IsolationState, argv: list[str]) -> int:
        config = state.worktree / ".devcontainer" / state.profile / "devcontainer.json"
        result = self.run(
            [
                "devcontainer",
                "exec",
                f"--workspace-folder={state.worktree}",
                f"--config={config}",
                *argv,
            ],
            cwd=state.worktree,
            capture=False,
        )
        return result.returncode

    def restart(self, state: IsolationState, force: bool = False) -> str:
        """Bounce the devcontainer without dropping the worktree (#341 Task 3).

        `docker restart` cycles only the process tree — the container filesystem
        and the bind-mounted worktree survive, so node_modules / local DB stacks
        / in-container installs are kept (unlike down+up). `force` uses
        `--time=0` (immediate SIGKILL then start) for a container too wedged to
        stop gracefully. Returns the restarted container id.
        """
        container = self._container_id(state)
        if not container:
            raise IsolationError(
                f"no container for {state.branch} — nothing to restart "
                "(run `fr isolation up` first)."
            )
        argv = ["docker", "restart", *(["--time=0"] if force else []), container]
        result = self.run(argv)
        if result.returncode != 0:
            raise IsolationError(
                f"docker restart failed: {result.stderr or result.stdout}. If the container "
                "is too wedged to stop gracefully, retry with --force."
            )
        return container

    def stats(self, state: IsolationState) -> dict[str, str] | None:
        """Host-side `docker stats --no-stream` for a RUNNING container (#341
        Task 3B), so an agent can detect a thrashing container instead of
        inferring it from hung execs. Returns None (never raises) for a missing,
        non-running, or unreadable container — the caller renders `n/a`."""
        # One `docker ps` for both id and state (id state, space-joined).
        parts = self._docker_ps_line(state).split()
        if len(parts) < 2 or parts[1] != "running":
            return None
        container = parts[0]
        # Pipe-delimited: MemUsage ("1.2GiB / 4GiB") itself contains spaces.
        result = self.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}",
                container,
            ]
        )
        line = (result.stdout or "").strip()
        if result.returncode != 0 or line.count("|") != 2:
            return None
        cpu, mem, mem_perc = line.split("|")
        return {"cpu": cpu, "mem": mem, "mem_perc": mem_perc}

    def status(self, state: IsolationState) -> dict[str, Any]:
        return {
            "repo": str(state.repo_root),
            "branch": state.branch,
            "profile": state.profile,
            "worktree": str(state.worktree),
            "worktree_exists": state.worktree.is_dir(),
            "container": self._container_state(state) or "not running",
            "pr": self._pr(state),
        }

    # Backend CLI name + PR/MR label for push_check's guidance line —
    # shares the gh/glab/tea vocabulary `fr._hosts.TAG_FOR_BACKEND` already
    # uses elsewhere, kept local (not imported) since that map's values are
    # the short vk-card tags ("gh"/"gl"/"gt"), not the CLI binary names.
    _CLI_FOR_BACKEND: ClassVar[dict[str, str]] = {
        "github": "gh",
        "gitlab": "glab",
        "gitea": "tea",
    }
    _PR_LABEL: ClassVar[dict[str, str]] = {
        "github": "PR",
        "gitlab": "MR",
        "gitea": "PR",
    }

    def push_check(self, state: IsolationState) -> dict[str, Any]:
        """Read-only preflight diagnostic for #377: worktree remotes,
        whether an SSH agent socket is visible IN-CONTAINER (informational
        only — expected to be ABSENT, that is not a failure), and a
        backend-aware pointer at the correct host-side push workflow. Never
        prints key material, tokens, or ssh-agent socket contents/paths —
        presence/absence only (see `_ssh_agent_probe`)."""
        remote = self.run(["git", "-C", str(state.worktree), "remote", "-v"])
        remotes = [ln for ln in (remote.stdout or "").splitlines() if ln.strip()]
        backend = detect_backend(self.repo_root)
        cli = self._CLI_FOR_BACKEND.get(backend, "gh")
        pr_label = self._PR_LABEL.get(backend, "PR")
        guidance = (
            f"push and {cli} {pr_label} creation must run on the HOST from the worktree "
            f"(cd {state.worktree} && git push ...) — the container has no SSH agent "
            "or git-host credentials by design; see fr-isolation SKILL.md's "
            "Exec-bridge discipline."
        )
        return {
            "branch": state.branch,
            "remotes": remotes,
            "backend": backend,
            "ssh_agent_in_container": self._ssh_agent_probe(state),
            "guidance": guidance,
        }

    def _ssh_agent_probe(self, state: IsolationState) -> dict[str, Any]:
        """In-container SSH_AUTH_SOCK presence check ONLY — reports whether
        the env var is set and, if set, whether the socket path exists, never
        the path itself or any key material (no `ssh-add -l`, no reading the
        socket)."""
        config = state.worktree / ".devcontainer" / state.profile / "devcontainer.json"
        probe_script = (
            'if [ -n "$SSH_AUTH_SOCK" ]; then '
            'if [ -S "$SSH_AUTH_SOCK" ]; then echo set:socket-exists; '
            "else echo set:socket-missing; fi; "
            "else echo unset; fi"
        )
        result = self.run(
            [
                "devcontainer",
                "exec",
                f"--workspace-folder={state.worktree}",
                f"--config={config}",
                "sh",
                "-c",
                probe_script,
            ],
            cwd=state.worktree,
        )
        detail = (result.stdout or "").strip() or "unknown"
        return {"present": detail.startswith("set:"), "detail": detail}

    def verify_merge(
        self,
        state: IsolationState,
        default_branch: str = "main",
        remote: str = "origin",
    ) -> dict[str, Any]:
        """Confirm the branch's changes reached `<remote>/<default_branch>`.

        Squash/rebase/merge-safe (content-based, not ancestry). `verified`
        requires ALL THREE positive confirmations — content present AND the PR
        is `MERGED` AND the `<remote>/<default_branch>` ref is fresh (fetch
        succeeded). The content check alone can be fooled by genuinely
        convergent content (the same fix landing twice), so the MERGED PR is the
        load-bearing tiebreak; an unknown PR state or a failed fetch is
        conservatively NOT verified, never a silent pass. The close-out (#320)
        STOPs (and the caller inspects which signal is missing) when not
        verified.
        """
        base_ref = f"{remote}/{default_branch}"
        fetch = self.run(["git", "fetch", remote, default_branch], cwd=state.worktree)
        fetched = fetch.returncode == 0
        res = branch_changes_present(self.run, state.worktree, state.branch, base_ref)
        pr = self._pr(state)
        pr_state = pr.get("state") if pr else None
        verified = res.changes_present and pr_state == "MERGED" and fetched
        return {
            "branch": state.branch,
            "verified": verified,
            "changes_present": res.changes_present,
            "missing": res.missing,
            "pr_state": pr_state,
            "fetched": fetched,
        }

    def down(self, state: IsolationState, force: bool = False) -> None:
        """Tear down the workspace, verifying each destructive step's
        POST-CONDITION before deleting the bookkeeping (#354 Task A).

        Historically `down` ran `docker stop`/`rm` and `git worktree remove`
        with their return codes ignored, then `delete_state()` unconditionally —
        so a transient docker hiccup left a running container that `fr isolation
        status` could no longer see (state gone). Now: re-query the container /
        worktree AFTER the teardown call and, if it survived, raise
        `IsolationError` while LEAVING the state file + `.fr-isolation` marker in
        place, so the workspace stays visible and a retry (or `--force` for the
        open-PR case) can finish the job.

        Re-query — not the return code — is authoritative: `docker rm` on an
        already-gone container returns non-zero while the post-condition (gone)
        holds, and a `rm` can return 0 yet leave a wedged container. `--force`
        bypasses the open-PR guard ONLY; it never skips this verification (that
        would re-introduce the invisible-leak bug).
        """
        self._down_worktree_tail(state, force)
        self._spawn_gc()

    def _down_worktree_tail(self, state: IsolationState, force: bool) -> None:
        """PR guard → environment teardown → verified worktree removal → marker +
        state retirement. Shared with `HostWorktreeTarget` (#... isolation host
        modes): the ONLY per-mode difference is `_teardown_container`, which the
        host-worktree mode overrides to a no-op (no docker), so the guard, the
        post-condition verification, and the marker/state cleanup stay identical
        across modes."""
        pr = self._pr(state)
        if pr and pr.get("state") == "OPEN" and not force:
            raise IsolationError(
                f"PR for {state.branch} is still open ({pr.get('url', '?')}) — "
                "the operator may push to it. Re-run with --force to tear down anyway."
            )
        self._teardown_container(state)
        wt = self.run(
            ["git", "worktree", "remove", "--force", str(state.worktree)],
            cwd=self.repo_root,
        )
        if wt.returncode != 0 and state.worktree.exists():
            raise IsolationError(
                f"git worktree remove failed for {state.worktree}: "
                f"{wt.stderr or wt.stdout} — state left intact; retry `fr isolation down`."
            )
        # Marker removal is LAST: a raise above leaves the marker inside the
        # still-present worktree, so the workspace stays a valid isolation
        # workspace. When the worktree is gone the marker went with it — the
        # unlink is then an idempotent no-op.
        self._remove_isolation_marker(state.worktree)
        delete_state(state.repo_root, state.branch)

    def _teardown_container(self, state: IsolationState) -> None:
        """Stop + rm the devcontainer and reclaim its image, verifying the
        post-condition. Overridden to a no-op by docker-less modes."""
        # A FAILED `docker ps` (daemon unreachable) must NOT be read as "no
        # container" — that path would `delete_state()` while a container may
        # still be running once the daemon recovers, the exact #354 leak. So the
        # probe raises on query failure, only a successful-but-empty result means
        # "genuinely absent".
        probe = self._docker_ps(state)
        if probe.returncode != 0:
            raise IsolationError(
                f"docker ps failed while tearing down {state.branch} "
                f"({(probe.stderr or probe.stdout or '').strip()}) — cannot verify teardown; "
                "state left intact, retry `fr isolation down` once docker recovers."
            )
        line = (probe.stdout or "").strip()
        container = line.split()[0] if line else None
        # Capture the image id BEFORE `docker rm` (the container must still exist
        # to inspect it); reclaim it AFTER the container is confirmed gone.
        image = self._image_for(container) if container else None
        if container:
            self.run(["docker", "stop", container])
            self.run(["docker", "rm", container])
            verify = self._docker_ps(state)
            if verify.returncode != 0 or (verify.stdout or "").strip():
                raise IsolationError(
                    f"container for {state.branch} still present (or unverifiable) after "
                    "docker stop/rm — workspace left intact (still visible to `fr isolation "
                    "status`); retry `fr isolation down` once docker recovers."
                )
            self._reclaim_image(image)

    def _spawn_gc(self) -> None:
        """Fire the opportunistic background sweep — best-effort, never raises
        into up()/down()."""
        try:
            self._gc_spawner(self.repo_root)
        except Exception:
            pass

    # ---------- gc: host-wide reconciliation (#354 Task B) ----------

    def gc(self, dry_run: bool = False) -> list[GcAction]:
        """Reconcile every isolation workspace on the host: tear down the ones
        whose PR merged, leave open-PR and no-PR work alone, and reap orphaned
        containers. Host-wide (an `up` in repo A reaps completed work in B…F),
        so end-to-end workflows no longer depend on a human remembering to run
        `down` in the originating session.

        Classification is authoritative BEFORE any action — a blind `down()`
        would reap in-progress no-PR work (its guard only blocks OPEN PRs). Each
        workspace is handled in isolation: one failed teardown is recorded and
        the sweep continues.

        A host-wide flock serializes concurrent sweeps (two near-simultaneous
        up/down each fork a gc): the second gets `EWOULDBLOCK` and short-circuits
        to an empty report. The sweep is idempotent, so a skipped overlap is
        harmless — the next up/down re-runs it.
        """
        try:
            lock = self._acquire_gc_lock()
        except BlockingIOError:
            return []
        try:
            actions = [self._gc_one(rec, dry_run) for rec in self._discover_workspaces()]
            actions.extend(self._sweep_dangling_images(dry_run))
            actions.extend(self._sweep_empty_repo_dirs(dry_run))
            actions.extend(self._sweep_stale_sessions(dry_run))
            return actions
        finally:
            lock.close()

    def _acquire_gc_lock(self) -> IO[str]:
        """flock(LOCK_EX | LOCK_NB) on the host-wide gc lock; BlockingIOError if
        another sweep holds it. Idiom mirrored from fr_vk.bridge_cli (reimplemented
        locally — `fr` must not depend on `fr_vk`, which is being dropped)."""
        lock_path = _home() / ".cache" / "fr" / "isolation-gc.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            lock_path = Path("/tmp/fr-isolation-gc.lock")  # noqa: S108 — unprivileged fallback
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise BlockingIOError("gc sweep already in progress") from e
            raise
        return fh

    def _gc_one(self, rec: GcWorkspace, dry_run: bool) -> GcAction:
        wt = str(rec.worktree)
        if not rec.worktree.is_dir():
            # Orphan: worktree gone. If a container lingers (found by label),
            # reap it directly — no PR check needed (worktree gone ⇒ done).
            if not rec.container_id:
                return self._gc_stale_state(rec, dry_run)
            if dry_run:
                return GcAction(wt, None, "orphan", "would-reap")
            try:
                self._label_reap(rec.container_id)
                if rec.state is not None:  # dangling state file, if any
                    delete_state(rec.state.repo_root, rec.state.branch)
                return GcAction(wt, None, "orphan", "reaped", rec.container_id)
            except Exception as e:  # reap is best-effort; never abort the sweep
                return GcAction(wt, None, "orphan", "reap-failed", str(e))
        state = rec.state
        if state is None:
            return GcAction(wt, None, "no-state", "warned", "worktree present, no fr state")
        pr = self._pr_from(state.worktree, state.branch)
        pr_state = pr.get("state") if pr else None
        if pr_state == "MERGED":
            if dry_run:
                return GcAction(wt, state.branch, "merged", "would-reap")
            try:
                # Tear down through a Target rooted at the workspace's OWN repo
                # (down() keys git/gh off its repo_root) — substrate-neutral: gc
                # orchestrates Targets, it doesn't reach past them. The sibling
                # gets the NO-OP spawner so a reap never re-triggers a sweep.
                type(self)(state.repo_root, runner=self.run, gc_spawner=_noop_gc_spawn).down(
                    state, force=False
                )
                return GcAction(wt, state.branch, "merged", "reaped")
            except Exception as e:
                # Broad by design (matches the orphan branch): one workspace's
                # teardown — IsolationError, a missing binary, an OSError from
                # delete_state — must NEVER abort the host-wide sweep.
                return GcAction(wt, state.branch, "merged", "reap-failed", str(e))
        if pr_state == "OPEN":
            return GcAction(wt, state.branch, "open", "skipped")
        # No MERGED/OPEN PR. Before warning forever, check whether the branch's
        # CHANGES already landed on origin/<default> — a PR-less merge (work
        # squash-merged, rebased, or re-authored under another PR) is invisible
        # to gc's PR-only view, so the workspace would warn forever. Reap only
        # when provably safe (see _merged_by_content).
        if self._merged_by_content(state):
            if dry_run:
                return GcAction(wt, state.branch, "merged-by-content", "would-reap")
            try:
                type(self)(state.repo_root, runner=self.run, gc_spawner=_noop_gc_spawn).down(
                    state, force=False
                )
                return GcAction(wt, state.branch, "merged-by-content", "reaped")
            except Exception as e:  # per-workspace; never abort the host-wide sweep
                return GcAction(wt, state.branch, "merged-by-content", "reap-failed", str(e))
        return GcAction(
            wt, state.branch, "no-pr", "warned", "no PR — `fr isolation down` when done"
        )

    def _gc_stale_state(self, rec: GcWorkspace, dry_run: bool) -> GcAction:
        """Worktree gone, no container: retire the dangling fr state RECORD.

        A worktree removed out of band (`rm -rf`, a runner reaping its own
        workspace, a `git worktree remove` outside fr) leaves the state JSON at
        `<git-common-dir>/fr/isolation/<branch>.json` behind. Nothing discovered
        it before this (#423) — discovery was directory/container-driven — so
        `fr isolation status` reported a workspace that no longer exists, forever.

        Only bookkeeping is retired: the branch, its commits, and git's own
        worktree registration are left exactly as they are. Reaping is gated on
        `_stale_state_reapable()` so #354's invariant holds — a FAILED docker
        query must never be read as "no container".
        """
        wt = str(rec.worktree)
        state = rec.state
        if state is None:
            return GcAction(wt, None, "orphan", "skipped", "no container")
        if not self._stale_state_reapable():
            return GcAction(
                wt, state.branch, "orphan", "skipped", "docker unavailable — reap deferred"
            )
        if dry_run:
            return GcAction(wt, state.branch, "orphan", "would-reap", "stale state record")
        try:
            delete_state(state.repo_root, state.branch)
            return GcAction(wt, state.branch, "orphan", "reaped", "stale state record")
        except Exception as e:  # per-workspace; never abort the host-wide sweep
            return GcAction(wt, state.branch, "orphan", "reap-failed", str(e))

    def _stale_state_reapable(self) -> bool:
        """May a state record whose worktree is gone be retired right now?

        Only when the container view is TRUSTWORTHY: a healthy `docker ps`
        (returncode 0) means `_labelled_containers()` genuinely saw every
        container, so a record with none really is stale. A broken or absent
        daemon defers the reap to a later sweep rather than dropping the state
        file that is the only pointer to a container that may still be running
        (#354). Docker-less modes override this to an unconditional True — they
        have no containers by construction.
        """
        try:
            return self.run(["docker", "ps", "-q"]).returncode == 0
        except Exception:
            return False

    def _merged_by_content(self, state: IsolationState) -> bool:
        """True when a PR-less workspace's changes are ALL already present on
        origin/<default> — a merge gc's PR-only classifier can't see — and
        reaping is provably safe.

        CONTENT-based, not ancestry-based, so it recognizes SQUASH merges — the
        dominant org pattern. A squash lands the branch's net content as a new
        commit, leaving the branch NOT an ancestor of the default branch; an
        `is-ancestor` check would false-negative exactly there. Comparing final
        file content (`branch_changes_present` — squash/rebase/merge-commit safe)
        still sees the work as landed.

        ALL must hold (conservative on every unknown ⇒ False ⇒ warn, never reap):
        - `origin/<default>` resolves and exists;
        - the branch actually CHANGED files since its merge-base — a pristine or
          idle branch that merely fell behind main changed nothing, so it is not
          a merge and is never reaped (this protects the fresh workspace `up`
          creates at, or just behind, the tip);
        - every changed file's final content is already on `origin/<default>`;
        - the worktree is CLEAN (no uncommitted work to lose).

        A stale local `origin/<default>` ref only DEFERS a reap to a later sweep,
        never causes a wrong one. Documented residual (clean-worktree-guarded):
        genuinely convergent content — the identical change landing independently
        on main — reads as merged.
        """
        try:
            wt = state.worktree
            sibling = type(self)(state.repo_root, runner=self.run, gc_spawner=_noop_gc_spawn)
            base = f"origin/{sibling._resolve_default_branch()}"
            if self.run(["git", "rev-parse", "--verify", "--quiet", base], cwd=wt).returncode != 0:
                return False
            status = self.run(["git", "status", "--porcelain"], cwd=wt)
            if status.returncode != 0 or (status.stdout or "").strip():
                return False
            result = branch_changes_present(self.run, wt, state.branch, base)
            return bool(result.changed) and result.changes_present
        except Exception:
            return False

    def _discover_workspaces(self) -> list[GcWorkspace]:
        """Union THREE ownership-proving sources, then resolve each to its fr
        state. Every source is evidence that fr owns the workspace; nothing here
        enumerates `git worktree list`, so a plain `git worktree add` made by
        other automation is never seen and never reaped (#423 non-goal).

        1. docker-label containers (`devcontainer.local_folder`) — devcontainer
           mode only; docker-less modes override it to an empty list;
        2. directories under the fr worktree cache `~/.cache/fr/worktrees`;
        3. the invoking repo's own fr state records (#423) — the ONLY source
           that finds a workspace created at a custom `--path` (the shape
           runners use) or a record whose worktree has since vanished.

        Sources 1–2 are host-wide; source 3 is necessarily per-repo (state is
        per-repo, there is no host registry), which is why the cache scan stays.
        """
        by_path: dict[Path, GcWorkspace] = {}
        for cid, path in self._labelled_containers():
            by_path[path] = GcWorkspace(worktree=path, container_id=cid, state=None)
        for path in self._worktree_dirs():
            by_path.setdefault(path, GcWorkspace(worktree=path, container_id=None, state=None))
        recorded: dict[Path, IsolationState] = {}
        seen = {_realpath(p) for p in by_path}
        for st in self._repo_states():
            recorded[st.worktree] = st
            key = _realpath(st.worktree)
            if key in seen:
                continue  # already discovered by label/cache — don't double-classify
            seen.add(key)
            by_path[st.worktree] = GcWorkspace(worktree=st.worktree, container_id=None, state=None)
        for path, rec in by_path.items():
            if path.is_dir():
                try:
                    rec.state = self._resolve_state(path)
                except Exception:
                    # A single corrupt/unreadable state JSON must not abort the
                    # host-wide sweep — treat as no-state (the workspace is then
                    # warned, never blindly reaped).
                    rec.state = None
            else:
                # Worktree gone. Carry the state record through so the sweep can
                # retire the dangling bookkeeping (`_gc_stale_state`) instead of
                # leaving `fr isolation status` reporting a phantom workspace.
                rec.state = recorded.get(path)
        return list(by_path.values())

    def _repo_states(self) -> list[IsolationState]:
        """The invoking repo's fr state records — discovery source 3.

        Best-effort: a missing/corrupt state dir yields nothing rather than
        aborting the host-wide sweep (the other two sources still report)."""
        try:
            return list_states(self.repo_root)
        except Exception:
            return []

    def _labelled_containers(self) -> list[tuple[str, Path]]:
        result = self.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=devcontainer.local_folder",
                "--format",
                '{{.ID}}\t{{.Label "devcontainer.local_folder"}}',
            ]
        )
        out: list[tuple[str, Path]] = []
        for line in (result.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[0] and parts[1]:
                out.append((parts[0], Path(parts[1])))
        return out

    def _worktree_dirs(self) -> list[Path]:
        root = _home() / ".cache" / "fr" / "worktrees"
        if not root.is_dir():
            return []
        return [
            child
            for repo in root.iterdir()
            if repo.is_dir()
            for child in repo.iterdir()
            if child.is_dir()
        ]

    def _resolve_state(self, worktree: Path) -> IsolationState | None:
        common = _git_common_dir(worktree)
        repo_root = common.parent if common.name == ".git" else common
        for st in list_states(repo_root):
            if st.worktree == worktree or st.worktree.resolve() == worktree.resolve():
                return st
        return None

    def _label_reap(self, container_id: str) -> None:
        """Reap a container found only by docker label (worktree already gone):
        stop + rm + best-effort image rmi."""
        image = self._image_for(container_id)
        self.run(["docker", "stop", container_id])
        self.run(["docker", "rm", container_id])
        self._reclaim_image(image)

    def _sweep_dangling_images(self, dry_run: bool) -> list[GcAction]:
        """rmi `vsc-*` devcontainer images no live container references — the
        ~1 GB layers that accumulate as workspaces come and go (#354). Each rmi
        is best-effort: a still-referenced image failing is recorded, never
        raised."""
        referenced = self._referenced_images()
        out: list[GcAction] = []
        for image_id, repo, tag in self._vsc_images():
            ref = f"{repo}:{tag}"
            if image_id in referenced or repo in referenced or ref in referenced:
                continue
            if dry_run:
                out.append(GcAction(repo, None, "dangling-image", "would-reap", image_id))
                continue
            # Reap by the TAGGED ref, not the image id: a dangling image carrying
            # more than one tag makes `docker rmi <id>` fail ("referenced in
            # multiple repositories"). Removing each `repo:tag` untags cleanly and
            # frees the layers on the last tag — no --force, no multi-tag conflict.
            r = self.run(["docker", "rmi", ref])
            out.append(
                GcAction(
                    repo,
                    None,
                    "dangling-image",
                    "reaped" if r.returncode == 0 else "reap-failed",
                    image_id,
                )
            )
        return out

    def _vsc_images(self) -> list[tuple[str, str, str]]:
        result = self.run(["docker", "images", "--format", "{{.ID}}\t{{.Repository}}\t{{.Tag}}"])
        out: list[tuple[str, str, str]] = []
        for line in (result.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[1].startswith("vsc-"):
                out.append((parts[0], parts[1], parts[2]))
        return out

    def _referenced_images(self) -> set[str]:
        result = self.run(["docker", "ps", "-a", "--format", "{{.Image}}"])
        return {ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()}

    # ---------- gc: cache + session-index hygiene (spec 2026-09-04 §5.A/§5.C) ----------
    #
    # Substrate-neutral (no docker, no git), so every worktree mode inherits
    # them verbatim; ExternalTarget has its own read-only gc and is untouched.

    def _empty_repo_dirs(self) -> list[Path]:
        """Children of `~/.cache/fr/worktrees` with NO subdirectories — the
        `agent-…` / branch-slug folders an `up` from inside a linked worktree
        used to create before the cache was keyed on the main checkout, plus any
        repo folder whose last workspace was torn down. Stray files (`.DS_Store`)
        do not make a folder live: a workspace is always a subdirectory."""
        root = _home() / ".cache" / "fr" / "worktrees"
        if not root.is_dir():
            return []
        return [
            d
            for d in sorted(root.iterdir())
            if d.is_dir() and not any(c.is_dir() for c in d.iterdir())
        ]

    def _sweep_empty_repo_dirs(self, dry_run: bool) -> list[GcAction]:
        out: list[GcAction] = []
        for d in self._empty_repo_dirs():
            if dry_run:
                out.append(GcAction(str(d), None, "empty-repo-dir", "would-remove"))
                continue
            try:
                shutil.rmtree(d)
                out.append(GcAction(str(d), None, "empty-repo-dir", "removed"))
            except OSError as e:  # per-dir; never abort the host-wide sweep
                out.append(GcAction(str(d), None, "empty-repo-dir", "reap-failed", str(e)))
        return out

    def _sweep_stale_sessions(self, dry_run: bool) -> list[GcAction]:
        """Unlink per-session index files whose worktree is gone or whose
        workspace state no longer lists the session (state wins — the index is
        derived). Classification lives in `sessions.stale_session_indexes`, which
        is pure; only the unlink happens here. `detail` carries the index path."""
        from .sessions import stale_session_indexes

        out: list[GcAction] = []
        for p, data in stale_session_indexes():
            wt, br = data.get("worktree", "?"), data.get("branch")
            if dry_run:
                out.append(GcAction(wt, br, "stale-session", "would-remove", str(p)))
                continue
            p.unlink(missing_ok=True)
            out.append(GcAction(wt, br, "stale-session", "removed", str(p)))
        return out

    # ---------- helpers ----------

    def _ensure_validator_wrapper_in_ref(self, ref: str) -> None:
        """Plan repos need the wrapper in the ref used for worktree add.

        `fr isolation up` creates/reuses a linked worktree from git state, not
        the base checkout's uncommitted files. A filesystem-only wrapper check
        would let an untracked wrapper pass, then the isolated worktree would
        still lack the validator entry point.
        """
        plans = self.run(
            ["git", "ls-tree", ref, "--", "docs/superpowers/plans"],
            cwd=self.repo_root,
        )
        if plans.returncode != 0 or not plans.stdout.strip():
            return
        result = self.run(
            ["git", "ls-tree", ref, "--", "scripts/validate-plans.sh"],
            cwd=self.repo_root,
        )
        line = result.stdout.strip()
        if result.returncode != 0 or not line:
            raise IsolationError(
                "plan repo has scripts/validate-plans.sh in the working tree but not in "
                f"{ref}; run `bash ~/.claude/plugins/marketplaces/derio-net--super-fr/scripts/"
                "install-validator-wrapper.sh` if needed, commit it to the isolation start "
                "ref, then retry `fr isolation up`."
            )
        mode = line.split(maxsplit=1)[0]
        if mode != "100755":
            raise IsolationError(
                f"plan repo has scripts/validate-plans.sh in {ref} but it is not executable; "
                "run `chmod +x scripts/validate-plans.sh`, commit it to the isolation "
                "start ref, then retry `fr isolation up`."
            )

    def _ensure_validator_wrapper_in_worktree(self, worktree: Path) -> None:
        plans = worktree / "docs" / "superpowers" / "plans"
        if not plans.is_dir():
            return
        wrapper = worktree / "scripts" / "validate-plans.sh"
        if not wrapper.is_file() or not (wrapper.stat().st_mode & 0o111):
            raise IsolationError(
                f"existing isolation worktree {worktree} is missing executable "
                "scripts/validate-plans.sh; run `bash ~/.claude/plugins/marketplaces/"
                "derio-net/scripts/install-validator-wrapper.sh`, commit it on the worktree "
                "branch, then retry `fr isolation up`."
            )

    def _git_worktree_add(
        self, worktree: Path, branch: str, base: str | None = None, no_fetch: bool = False
    ) -> None:
        if worktree.exists():
            self._ensure_validator_wrapper_in_worktree(worktree)
            return  # already provisioned — up() is idempotent on the worktree
        branches = self.run(["git", "branch", "--list", branch], cwd=self.repo_root)
        if branches.stdout.strip():
            # Reuse: check the existing branch out as-is. Never fetch or rebase —
            # continuation/reuse must inherit the branch's own tip (#322 corner 1).
            self._ensure_validator_wrapper_in_ref(branch)
            argv = ["git", "worktree", "add", str(worktree), branch]
        else:
            # Genuine cold-start: a brand-new branch. Default to freshly-fetched
            # origin/<default> instead of the base repo's current HEAD (#322).
            start_point, log_line = self._cold_start_base(branch, base, no_fetch)
            print(log_line, file=sys.stderr if log_line.startswith("WARNING") else sys.stdout)
            self._ensure_validator_wrapper_in_ref(start_point or "HEAD")
            argv = ["git", "worktree", "add", str(worktree), "-b", branch]
            if start_point is not None:
                argv.append(start_point)
        result = self.run(argv, cwd=self.repo_root)
        if result.returncode != 0:
            raise IsolationError(f"git worktree add failed: {result.stderr}")

    # ----- .fr-isolation marker lifecycle (#328 Task 3) -----

    def _write_isolation_marker(self, worktree: Path, branch: str, mode: str = "worktree") -> None:
        """Write the `.fr-isolation` identity marker and git-exclude it.

        The marker is what the `fr-isolation-required` PreToolUse hook reads to
        decide whether an edit is inside a real isolation workspace (#328 Task
        3). Identity = the resolved worktree toplevel + branch + mode; the hook
        blocks when the recorded toplevel does not match the file's actual
        toplevel (a stale / copied marker) or when the toplevel is not a linked
        worktree. `up` also appends it to the shared `info/exclude`, so the
        marker can never be staged into a PR — backed by a committed
        `.gitignore` entry and a CI tripwire.
        """
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / ".fr-isolation").write_text(
            json.dumps(
                {
                    "toplevel": str(worktree.resolve()),
                    "branch": branch,
                    "mode": mode,
                    "created_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
            + "\n"
        )
        exclude = _git_common_dir(self.repo_root) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text().splitlines() if exclude.is_file() else []
        if ".fr-isolation" not in existing:
            with exclude.open("a") as fh:
                fh.write(".fr-isolation\n")

    def _remove_isolation_marker(self, worktree: Path) -> None:
        """Retire the marker on `down` (idempotent — absent is fine)."""
        (worktree / ".fr-isolation").unlink(missing_ok=True)

    # ----- cold-start base resolution (#322) -----

    def _cold_start_base(
        self, branch: str, base: str | None, no_fetch: bool
    ) -> tuple[str | None, str]:
        """Resolve the start-point for a NEW branch per the spec matrix.

        Returns (start_point, log_line). start_point is the ref to append after
        `-b <branch>`, or None meaning "append nothing" — git then defaults to
        the current HEAD (byte-identical to the legacy behaviour). The log_line
        is printed by the caller; a `WARNING`-prefixed line goes to stderr.
        """
        # Operator named an explicit start-point — use it verbatim, no fetch, no
        # default-branch resolution. `--base HEAD` is the documented opt-in to the
        # old "fork from current checkout" behaviour (stacking / current-branch).
        if base is not None:
            if base == "HEAD":
                return None, f"isolation: basing new branch {branch} on HEAD (--base)"
            return base, f"isolation: basing new branch {branch} on {base} (--base)"

        if no_fetch:
            default = self._resolve_default_branch()
            ref = f"origin/{default}"
            if self._ref_exists(ref):
                return ref, f"isolation: basing new branch {branch} on {ref} (local, --no-fetch)"
            return None, (
                f"WARNING: --no-fetch but no local {ref} tracking ref — "
                f"basing {branch} on local HEAD"
            )

        if not self._has_origin_remote():
            return None, f"WARNING: no origin remote — basing {branch} on local HEAD"

        if not self._fetch_origin():
            return None, f"WARNING: git fetch origin failed — basing {branch} on local HEAD"

        default = self._resolve_default_branch()
        ref = f"origin/{default}"
        if self._ref_exists(ref):
            return ref, f"isolation: basing new branch {branch} on {ref} (fetched)"
        return None, f"WARNING: {ref} not found after fetch — basing {branch} on local HEAD"

    def _has_origin_remote(self) -> bool:
        result = self.run(["git", "remote"], cwd=self.repo_root)
        return "origin" in (result.stdout or "").split()

    def _fetch_origin(self) -> bool:
        """git fetch origin; return success. Never raises — a fetch problem
        degrades to the local-HEAD fallback, it does not abort the run."""
        result = self.run(["git", "fetch", "origin"], cwd=self.repo_root)
        if result.returncode != 0:
            return False
        # Refresh origin/HEAD so _resolve_default_branch's symbolic-ref hits.
        # Best-effort: a failure here just falls through to the gh/main chain.
        self.run(["git", "remote", "set-head", "origin", "--auto"], cwd=self.repo_root)
        return True

    def _resolve_default_branch(self) -> str:
        """symbolic-ref refs/remotes/origin/HEAD → backend-specific CLI → main.

        The backend-specific step branches on `fr._hosts.detect_backend`
        (see docs/superpowers/specs/
        2026-07-09-multi-backend-git-host-adapters-design.md §8) — `gh`
        for GitHub (today's only behavior, unchanged), `glab repo view`
        for GitLab, `tea repos` for Gitea. Deliberately a SEPARATE branch
        here rather than routed through `hostclient.client_for` — this is
        a two-field lookup on the isolation lifecycle's own `Runner`
        seam (independent from `GhClient` by design), not worth the cost
        of constructing a full adapter for.
        """
        sym = self.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=self.repo_root
        )
        name = (sym.stdout or "").strip()
        if sym.returncode == 0 and name:
            # --short yields "origin/main"; strip to the bare branch name.
            return name.removeprefix("origin/")

        backend = detect_backend(self.repo_root)
        if backend == "gitlab":
            result = self.run(
                ["glab", "repo", "view", "-F", "json", "--jq", ".default_branch"],
                cwd=self.repo_root,
            )
        elif backend == "gitea":
            result = self.run(
                ["tea", "repos", "--fields", "default_branch", "--output", "json"],
                cwd=self.repo_root,
            )
        else:
            result = self.run(
                [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "defaultBranchRef",
                    "--jq",
                    ".defaultBranchRef.name",
                ],
                cwd=self.repo_root,
            )
        out = (result.stdout or "").strip()
        if result.returncode == 0 and out:
            if backend == "gitea":
                # tea's `--output json` on a single-repo view returns a
                # JSON object (unlike glab's --jq, which already extracts
                # the bare string) — parse it here. Field name confirmed
                # against Gitea's live swagger spec (Repository.default_branch);
                # the CLI's own JSON shape is reconfirmed against a live
                # tea in Phase 9's manual verification.
                try:
                    parsed = json.loads(out)
                except json.JSONDecodeError:
                    return "main"
                branch = parsed.get("default_branch") if isinstance(parsed, dict) else None
                return branch if isinstance(branch, str) and branch else "main"
            return out  # gh/glab both yield a bare branch name — no prefix to strip
        return "main"

    def _ref_exists(self, ref: str) -> bool:
        result = self.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=self.repo_root
        )
        return result.returncode == 0

    def _ensure_mounted_env_file(self, config: Path) -> None:
        """Ensure the env-file the profile's devcontainer.json mounts exists.

        Mount-following (#272): the committed config is the source of truth —
        an unmigrated repo still mounts the legacy vk path, so creating the
        fr file would not help docker. Warn on the legacy spelling; no
        --env-file in runArgs → nothing to ensure.
        """
        try:
            run_args = json.loads(config.read_text()).get("runArgs", [])
        except (OSError, json.JSONDecodeError):
            return
        for flag, value in zip(run_args, run_args[1:]):
            if flag != "--env-file":
                continue
            env_file = Path(value.replace("${localEnv:HOME}", str(_home())))
            if "/.config/vk/secrets/" in str(env_file):
                _warn_legacy("secrets env-file mount", env_file)
            if not env_file.is_file():
                env_file.parent.mkdir(parents=True, exist_ok=True)
                env_file.write_text(f"# fr isolation secrets — {self.repo_root.name}\n")
            harden_secret_file(env_file)  # 0600 file / 0700 dirs — self-heals loose perms

    def _docker_ps(self, state: IsolationState) -> subprocess.CompletedProcess[str]:
        return self.run(
            [
                "docker",
                "ps",
                "--all",
                f"--filter=label=devcontainer.local_folder={state.worktree}",
                "--format={{.ID}} {{.State}}",
            ]
        )

    def _docker_ps_line(self, state: IsolationState) -> str:
        """Tolerant read for status/stats — empty on absent OR query failure.
        `down()` uses the raw `_docker_ps` so it can tell those two apart: a
        FAILED query must never be read as 'container gone' (that would re-open
        the #354 leak under a down daemon), only a successful-but-empty one."""
        return (self._docker_ps(state).stdout or "").strip()

    def _container_id(self, state: IsolationState) -> str | None:
        line = self._docker_ps_line(state)
        return line.split()[0] if line else None

    def _image_for(self, container: str) -> str | None:
        """The image id backing a container (for post-teardown reclamation)."""
        result = self.run(["docker", "inspect", "--format", "{{.Image}}", container])
        img = (result.stdout or "").strip()
        return img if result.returncode == 0 and img else None

    def _reclaim_image(self, image: str | None) -> None:
        """Best-effort `docker rmi` — #354. A devcontainer's `vsc-*` image is
        ~1 GB and leaks today (down never removed it). Reclamation is OFF the
        verification path: a shared / in-use image failing `rmi` is logged, never
        fatal — image cleanup must not block a teardown whose container is
        already gone."""
        if not image:
            return
        result = self.run(["docker", "rmi", image])
        if result.returncode != 0:
            print(
                f"warning: could not remove image {image} (shared or in use?): "
                f"{(result.stderr or result.stdout or '').strip()}",
                file=sys.stderr,
            )

    def _container_state(self, state: IsolationState) -> str | None:
        parts = self._docker_ps_line(state).split()
        return parts[1] if len(parts) > 1 else None

    def _pr(self, state: IsolationState) -> dict[str, Any] | None:
        return self._pr_from(self.repo_root, state.branch)

    def _pr_from(self, cwd: Path, branch: str) -> dict[str, Any] | None:
        """PR/MR-for-branch lookup from an arbitrary repo cwd — gc reconciles
        workspaces across repos, so it can't assume `self.repo_root`. Uses
        host auth (gh/glab) or the CLI's own login (tea).

        Backend-branched the same way as `_resolve_default_branch` (see
        docs/superpowers/specs/
        2026-07-09-multi-backend-git-host-adapters-design.md §8):
        - GitHub: `gh pr view <branch> --json state,url` (today's only
          behavior, unchanged).
        - GitLab: `glab mr view <branch> --output json` — a single-shot
          query like gh's (`glab mr view` accepts a bare branch name
          directly, per its own `--help`, unlike the URL case in
          `real_glabclient.py`'s `pr_status_by_url`).
        - Gitea: no single-shot branch→PR query exists (verified during
          research — `tea pulls` only takes a numeric index). Falls back
          to listing ALL PRs (`--state all`, so a merged/closed PR for a
          torn-down branch is still found) and matching `head.label`
          client-side — a real, bounded degradation vs. gh/glab's
          single-shot query, not a bug.

        Every path returns the SAME shape callers already depend on:
        `{"state": "OPEN"|"MERGED"|"CLOSED", "url": str}` — each
        backend's native state vocabulary is coerced here so callers never
        see gh/glab/tea-specific casing or a separate merged boolean.
        """
        backend = detect_backend(cwd)
        if backend == "gitlab":
            return self._pr_from_gitlab(cwd, branch)
        if backend == "gitea":
            return self._pr_from_gitea(cwd, branch)
        result = self.run(
            ["gh", "pr", "view", branch, "--json", "state,url"],
            cwd=cwd,
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _pr_from_gitlab(self, cwd: Path, branch: str) -> dict[str, Any] | None:
        result = self.run(["glab", "mr", "view", branch, "--output", "json"], cwd=cwd)
        if result.returncode != 0 or not (result.stdout or "").strip():
            return None
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        raw_state = raw.get("state", "opened")
        if raw_state == "merged":
            state = "MERGED"
        elif raw_state in ("closed", "locked"):
            state = "CLOSED"
        else:
            state = "OPEN"
        return {"state": state, "url": raw.get("web_url", "")}

    def _pr_from_gitea(self, cwd: Path, branch: str) -> dict[str, Any] | None:
        result = self.run(
            [
                "tea",
                "pulls",
                "list",
                "--state",
                "all",
                "--fields",
                "state,merged,url,head",
                "--output",
                "json",
            ],
            cwd=cwd,
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            return None
        try:
            entries = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            head = entry.get("head") or {}
            if not isinstance(head, dict) or head.get("label") != branch:
                continue
            merged = bool(entry.get("merged", False))
            state = "MERGED" if merged else ("CLOSED" if entry.get("state") == "closed" else "OPEN")
            return {"state": state, "url": entry.get("url", "")}
        return None
