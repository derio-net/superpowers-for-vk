"""fr isolation — CLI over the isolation Target (worktree + devcontainer).

Plain-shell surface by design (agent-agnostic): any agent or a human drives
up/exec/status/down identically. IsolationError maps to exit 2.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, cast

import typer

from fr.isolation import sessions as _sessions
from fr.isolation.external import ExternalTarget
from fr.isolation.hostworktree import HostWorktreeTarget
from fr.isolation.local import (
    GcAction,
    LocalWorktreeDevcontainerTarget,
    _detached_gc_spawn,
    subprocess_runner,
)
from fr.isolation.types import (
    IsolationError,
    IsolationState,
    Target,
    clear_repo_sentinels,
    list_states,
    load_state,
)

isolation_app = typer.Typer(
    name="isolation",
    help="Isolated workspaces: git worktree + devcontainer.",
    no_args_is_help=True,
)

# Module-level runner seam so tests can monkeypatch every external call.
_runner = subprocess_runner
# Production entry point opts into the real detached gc spawn on up/down; the
# library default is a no-op. Module seam so tests never fork a real sweep.
_gc_spawner = _detached_gc_spawn

DEFAULT_BRANCH = "vk-iso/work"


def _target(repo: Path) -> Target:
    """Select the isolation backend. Precedence (spec §A Selection):

    1. a valid `external` marker at `repo`'s toplevel → `ExternalTarget`,
       regardless of any other configuration — a prepared container is a
       recognize-and-adopt, not a second isolation attempt;
    2. else `FR_ISOLATION_TARGET` (a HOST-level declaration, never a per-call
       flag — spec §B): unset/"devcontainer" → the local worktree+devcontainer
       target (unchanged default); "worktree" → HostWorktreeTarget (fr worktree,
       host env, no docker); anything else fails closed so a broken docker can't
       be silently routed around."""
    root = repo.resolve()
    external = ExternalTarget.detect(root, runner=_runner)
    if external is not None:
        return external
    mode = os.environ.get("FR_ISOLATION_TARGET")
    if mode in (None, "", "devcontainer"):
        return LocalWorktreeDevcontainerTarget(root, runner=_runner, gc_spawner=_gc_spawner)
    if mode == "worktree":
        return HostWorktreeTarget(root, runner=_runner, gc_spawner=_gc_spawner)
    raise IsolationError(f"unknown FR_ISOLATION_TARGET {mode!r} — valid: devcontainer | worktree")


def _worktree_ops(target: Target) -> LocalWorktreeDevcontainerTarget:
    """Type-narrow to the worktree+container family for the two subcommands
    whose operations (push-check, verify-merge) are NOT in the Target protocol.
    A `cast`, not an isinstance check, on purpose: those commands are driven in
    tests through duck-typed stubs monkeypatched over `_target`, so a nominal
    guard would reject the doubles. The non-devcontainer modes are refused
    up-front by the `_refuse_*` guards below before this cast runs."""
    return cast("LocalWorktreeDevcontainerTarget", target)


class _GcCapable(Protocol):
    """Every target reconciles — the shapes differ, the surface does not (#423).
    Separate from `_worktree_ops` precisely because `gc` is no longer a
    worktree-family-only operation: `ExternalTarget` implements it too."""

    def gc(self, dry_run: bool = False) -> list[GcAction]: ...


def _gc_ops(target: Target) -> _GcCapable:
    return cast("_GcCapable", target)


def _fail(err: IsolationError) -> None:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(2)


def _resolve_repo(repo: Path) -> Path:
    """Resolve the --repo path, degrading a deleted cwd to a clean error (#399).

    The default is ``Path(".")``; resolving it calls ``os.getcwd()``. When a
    prior ``down`` removed the worktree the operator's shell was sitting in, cwd
    no longer exists and ``resolve()`` raises ``FileNotFoundError`` — an
    unhandled traceback. Convert it to a normal IsolationError (exit 2) that
    tells the operator to cd elsewhere or pass ``--repo``.
    """
    try:
        return repo.resolve()
    except FileNotFoundError:
        _fail(
            IsolationError(
                "current directory no longer exists (a prior teardown likely "
                "removed it) — cd to an existing directory or pass --repo."
            )
        )
        raise AssertionError("unreachable")  # _fail always raises typer.Exit


def _target_or_exit(repo: Path) -> Target:
    """`_target` + uniform IsolationError → clean exit 2. `_target` fails closed
    on a bogus `FR_ISOLATION_TARGET`; every command that selects a target must
    map that to the same "error: … (exit 2)" UX rather than a traceback (up /
    restart / down already wrap their whole body; this is the shared wrap for
    exec / status / verify-merge / gc / down --all)."""
    try:
        return _target(repo)
    except IsolationError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(2) from err


def _resolve_single(root: Path, branch: str | None) -> IsolationState:
    """Resolve the workspace a command targets: explicit `--branch`, else the
    single active workspace, else a clean exit 2 (super-fr#299 part 3 — never a
    phantom hardcoded default). Shared by exec / restart / down / attach."""
    if branch is not None:
        state = load_state(root, branch)
    else:
        states = list_states(root)
        if len(states) > 1:
            _fail(
                IsolationError(
                    "multiple isolation workspaces — specify --branch: "
                    + ", ".join(s.branch for s in states)
                )
            )
        state = states[0] if states else None
    if state is None:
        _fail(
            IsolationError(
                f"no isolation workspace for branch {branch!r} — run `fr isolation up` first."
                if branch
                else "no isolation workspace — run `fr isolation up` first."
            )
        )
    return state  # type: ignore[return-value]


def _resolve_by_worktree(worktree: Path) -> IsolationState:
    """Resolve a workspace by its worktree path (`down --worktree`, spec
    2026-09-04 §5.B.4). `list_states` keys on the git common dir, which resolves
    from inside the worktree; a non-git or missing path degrades to no states
    (never a traceback) → clean exit 2."""
    wt = worktree.resolve()
    try:
        states = list_states(wt) if wt.is_dir() else []
    except IsolationError:
        states = []
    matches = [s for s in states if s.worktree.resolve() == wt]
    if not matches:
        _fail(IsolationError(f"no isolation workspace at {wt}"))
    return matches[0]


def _refuse_external(target: Target, op: str) -> None:
    """External mode adopts a preparer-managed containment — the worktree-ops
    subcommands (push-check / verify-merge / gc) do not apply. Refuse cleanly
    (exit 2) instead of AttributeError-ing through the `_worktree_ops` cast."""
    if isinstance(target, ExternalTarget):
        _fail(IsolationError(f"{op} not supported in external mode — externally managed."))


def _refuse_no_docker_status_extras(target: Target) -> None:
    """`status --stats` / `--push-check` need a docker-backed devcontainer
    (docker stats; in-container ssh-agent probe). Neither host-worktree nor
    external mode has one — refuse cleanly rather than FileNotFoundError on a
    docker-less host or AttributeError through the cast."""
    if isinstance(target, ExternalTarget):
        _fail(
            IsolationError(
                "--stats / --push-check not supported in external mode — externally managed."
            )
        )
    if isinstance(target, HostWorktreeTarget):
        _fail(
            IsolationError(
                "--stats / --push-check require devcontainer mode (docker) — "
                "not available in host-worktree mode."
            )
        )


@isolation_app.command()
def up(
    repo: Path = typer.Option(Path("."), help="Repo root (default: cwd)."),
    profile: str | None = typer.Option(
        None, help="Devcontainer profile (default: repo's configured default)."
    ),
    branch: str = typer.Option(DEFAULT_BRANCH, help="Branch for the worktree."),
    path: Path | None = typer.Option(
        None,
        help="Worktree path (default: ~/.cache/fr/worktrees/<main-checkout>/<branch>).",
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="Start-point for a NEW branch (e.g. origin/main, HEAD, <sha>). "
        "Given → no fetch, no default-branch resolution. New branches otherwise "
        "default to freshly-fetched origin/<default>.",
    ),
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help="Skip git fetch; base a new branch on the local origin/<default> "
        "tracking ref (or HEAD if absent).",
    ),
    session: str | None = typer.Option(
        None, "--session", help="Bind this agent session to the new workspace (one CLI call)."
    ),
    harness: str = typer.Option(
        "unknown", "--harness", help="claude | hermes | opencode | unknown (with --session)."
    ),
    print_path: bool = typer.Option(
        False,
        "--print-path",
        help="Print ONLY the worktree path on stdout (last line); everything else "
        "to stderr. For WorktreeCreate hooks.",
    ),
) -> None:
    """Create worktree + start the profile's devcontainer against it.

    With --print-path (spec 2026-09-04 §5.B.3) the LAST non-empty stdout line
    is the worktree path — the contract a WorktreeCreate hook relies on — and
    the human-facing lines move to stderr.
    """
    try:
        state = _target(repo).up(
            profile=profile, branch=branch, path=path, base=base, no_fetch=no_fetch
        )
        if session:
            state = _sessions.attach(state.repo_root, state.branch, session, harness=harness)
    except IsolationError as err:
        _fail(err)
        return
    typer.echo(
        f"isolation up: worktree={state.worktree} profile={state.profile} branch={state.branch}",
        err=print_path,
    )
    if os.environ.get("CLAUDECODE"):
        typer.echo(
            "tip: register the worktree as a Claude Code working directory so the "
            "shell cwd persists there (no more `cd <worktree> && …` for host git/gh):\n"
            f"    /add-dir {state.worktree}",
            err=print_path,
        )
    if print_path:
        typer.echo(str(state.worktree))


@isolation_app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def exec(  # noqa: A001 - typer command name
    ctx: typer.Context,
    repo: Path = typer.Option(Path("."), help="Repo root (default: cwd)."),
    branch: str | None = typer.Option(
        None, help="Isolation branch (default: the single active workspace)."
    ),
) -> None:
    """Run a command inside the isolation container (exit code passthrough)."""
    root = _resolve_repo(repo)
    # super-fr#299 part 3: with --branch omitted, resolve to the single active
    # workspace instead of a hardcoded vk-iso/work default — so `exec` after an
    # `up --branch feat/x` targets the workspace the operator actually has,
    # never a phantom default. Mirrors `status`/`down`'s no-branch handling.
    state = _resolve_single(root, branch)
    argv = list(ctx.args)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        _fail(IsolationError("nothing to run — usage: fr isolation exec -- CMD ..."))
        return
    raise typer.Exit(_target_or_exit(repo).exec(state, argv))


@isolation_app.command()
def restart(
    repo: Path = typer.Option(Path("."), help="Repo root (default: cwd)."),
    branch: str | None = typer.Option(
        None, help="Isolation branch (default: the single active workspace)."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Immediate SIGKILL (docker restart --time=0) when a graceful stop hangs.",
    ),
) -> None:
    """Bounce a resource-wedged devcontainer, keeping the worktree intact.

    Unlike `down` + `up`, `restart` cycles only the container process tree — the
    worktree, node_modules, local DB stack, and in-container installs survive.
    """
    root = _resolve_repo(repo)
    # Mirror exec's no-branch resolution: the single active workspace, or error.
    state = _resolve_single(root, branch)
    try:
        container = _target(root).restart(state, force=force)
    except IsolationError as err:
        _fail(err)
        return
    typer.echo(f"isolation restart: {state.branch} bounced ({container}).")


@isolation_app.command()
def status(
    repo: Path = typer.Option(Path("."), help="Repo root (default: cwd)."),
    branch: str | None = typer.Option(None, help="Limit to one isolation branch."),
    format: str = typer.Option("text", "--format", help="text | json"),
    stats: bool = typer.Option(
        False,
        "--stats",
        help="Include container resource use (docker stats --no-stream) — opt-in "
        "to keep the default status fast; helps spot a thrashing container (#341).",
    ),
    push_check: bool = typer.Option(
        False,
        "--push-check",
        help="Read-only preflight for #377: worktree remotes, in-container SSH-agent "
        "presence (informational — expected absent), and a backend-aware pointer at "
        "the correct host-side push workflow. Never prints key material or "
        "token/socket contents.",
    ),
    session: str | None = typer.Option(
        None, "--session", help="Only the workspace this session is bound to."
    ),
) -> None:
    """Show worktree, container, PR, and bound-session state for isolation workspaces."""
    root = _resolve_repo(repo)
    states = (
        [s for s in [load_state(root, branch)] if s is not None] if branch else list_states(root)
    )
    if branch and not states:
        _fail(IsolationError(f"no isolation workspace for branch {branch!r}."))
        return
    if session:
        states = [s for s in states if any(b.session_id == session for b in s.sessions)]
    target = _target_or_exit(root)
    if stats or push_check:
        _refuse_no_docker_status_extras(target)
    rows = [target.status(s) for s in states]
    for row, s in zip(rows, states):
        row["sessions"] = [b.model_dump() for b in s.sessions]
    if stats:
        for row, s in zip(rows, states):
            row["stats"] = target.stats(s)
    if push_check:
        for row, s in zip(rows, states):
            row["push_check"] = _worktree_ops(target).push_check(s)
    if format == "json":
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("no isolation workspaces.")
        return
    for r in rows:
        pr = r["pr"]
        pr_text = f"{pr['state']} {pr.get('url', '')}".strip() if pr else "none"
        line = (
            f"{r['branch']}: profile={r['profile']} container={r['container']} "
            f"worktree={r['worktree']} pr={pr_text}"
        )
        if stats:
            st = r.get("stats")
            line += (
                f" stats=cpu={st['cpu']} mem={st['mem']} ({st['mem_perc']})" if st else " stats=n/a"
            )
        line += f" sessions={','.join(b['session_id'] for b in r['sessions']) or 'none'}"
        typer.echo(line)
        if push_check:
            pc = r.get("push_check")
            if pc:
                remotes_text = ", ".join(pc["remotes"]) if pc["remotes"] else "(none)"
                typer.echo(f"  remotes: {remotes_text}")
                ssh = pc["ssh_agent_in_container"]
                ssh_text = "present" if ssh["present"] else "absent (expected — informational only)"
                typer.echo(f"  ssh-agent-in-container: {ssh_text}")
                typer.echo(f"  {pc['guidance']}")


@isolation_app.command()
def down(
    repo: Path = typer.Option(Path("."), help="Repo root (default: cwd)."),
    branch: str | None = typer.Option(
        None, help="Isolation branch to tear down (default: the single active workspace)."
    ),
    force: bool = typer.Option(False, "--force", help="Tear down even with an open PR."),
    all_: bool = typer.Option(
        False,
        "--all",
        help="Tear down EVERY workspace for the repo and clear the pipeline "
        "sentinel(s) — the escape from an orphaned-sentinel deadlock (#341).",
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help="The calling session; excluded from the still-attached warning.",
    ),
    worktree: Path | None = typer.Option(
        None,
        "--worktree",
        help="Resolve the workspace by its worktree path (WorktreeRemove hooks).",
    ),
) -> None:
    """Stop the container, remove the worktree, drop the state.

    With --worktree <path> (spec 2026-09-04 §5.B.4), resolve the workspace by
    its worktree path instead of --repo/--branch — the caller (a WorktreeRemove
    hook) only knows the path and may run from any cwd.

    With --branch omitted, resolve to the single active workspace — so bare
    `down` from inside a worktree tears down the workspace the operator actually
    has, never a phantom hardcoded default (#399). Mirrors exec/restart/status.

    With --all, ignore --branch: tear down all workspaces (keeping any with an
    OPEN PR unless --force) and clear this repo's pipeline sentinel(s).

    Bound sessions (spec 2026-09-04 §5.A): every session attached to the
    workspace is unbound; those other than `--session` are named in a warning
    (never a refusal — liveness is unknowable here).
    """
    if worktree is not None:
        state = _resolve_by_worktree(worktree)
        root = state.repo_root
    else:
        root = _resolve_repo(repo)
        if all_:
            _down_all(root, force=force)
            return
        state = _resolve_single(root, branch)
    others = [b.session_id for b in state.sessions if b.session_id != session]
    if others:
        typer.echo(
            f"warning: sessions still attached: {', '.join(others)} — they will be detached",
            err=True,
        )
    try:
        _target(root).down(state, force=force)
    except IsolationError as err:
        _fail(err)
        return
    # Only after a SUCCESSFUL teardown: a refused `down` (open PR) keeps the
    # workspace, so it keeps its bindings too.
    _sessions.detach_all(state)
    # #399: when this was the last workspace, clear the pipeline sentinel(s)
    # eagerly. The bash guard's own clear can't fire here — it exits early when
    # `down` runs from the worktree cwd (the prescribed workflow), so the guard
    # would keep reporting 'fr pipeline active'. Mirrors `down --all`'s eager
    # clear (clear_repo_sentinels), scoped to "zero workspaces remain".
    if not list_states(root):
        clear_repo_sentinels(root)
    typer.echo(f"isolation down: {state.branch} cleaned up.")


def _down_all(root: Path, force: bool) -> None:
    """Tear down every workspace + drop session sentinel(s) (#341 Task 2A).

    A workspace whose PR is still OPEN is KEPT (never silently destroyed) unless
    --force — that open-PR check is the only IsolationError `down` raises, so
    "kept" always means an open PR. Sentinels are cleared regardless — `down
    --all` is the deliberate "end this pipeline" lever, with the guard self-heal
    as the lazy backstop.
    """
    target = _target_or_exit(root)
    torn: list[str] = []
    kept: list[str] = []
    for state in list_states(root):
        try:
            target.down(state, force=force)
            torn.append(state.branch)
        except IsolationError:
            kept.append(state.branch)
            continue
        _sessions.detach_all(state)  # kept workspaces keep their bindings
    cleared = clear_repo_sentinels(root)
    summary = f"isolation down --all: {len(torn)} torn down"
    if kept:
        summary += f", {len(kept)} kept (open PR — rerun with --force): {', '.join(kept)}"
    summary += f", {cleared} sentinel(s) cleared."
    typer.echo(summary)


@isolation_app.command()
def attach(
    session: str = typer.Option(
        ..., "--session", help="Agent session id (from the harness hook payload)."
    ),
    repo: Path = typer.Option(Path("."), help="Repo root (default: cwd)."),
    branch: str | None = typer.Option(
        None, help="Isolation branch (default: the single active workspace)."
    ),
    harness: str = typer.Option("unknown", help="claude | hermes | opencode | unknown"),
) -> None:
    """Bind a session to a workspace (traceability; idempotent).

    A session holds at most one binding — attaching elsewhere moves it. State
    (IsolationState.sessions) is the source of truth; the per-session index
    under ~/.cache/fr/sessions/ is derived (spec 2026-09-04 §5.A, decision d1).
    """
    root = _resolve_repo(repo)
    state = _resolve_single(root, branch)
    try:
        _sessions.attach(root, state.branch, session, harness=harness)
    except IsolationError as err:
        _fail(err)
        return
    typer.echo(f"attached {session} -> {state.branch}")


@isolation_app.command()
def detach(
    session: str = typer.Option(..., "--session", help="Agent session id to unbind."),
    repo: Path | None = typer.Option(
        None, help="Limit to this repo (default: wherever the session is bound)."
    ),
    branch: str | None = typer.Option(None, help="Limit to this branch (with --repo)."),
) -> None:
    """Unbind a session (idempotent; exit 0 when nothing was bound)."""
    try:
        gone = _sessions.detach(session, repo_root=repo.resolve() if repo else None, branch=branch)
    except IsolationError as err:
        _fail(err)
        return
    typer.echo(
        f"detached {session} <- {', '.join(gone)}" if gone else f"detached {session} (no binding)"
    )


@isolation_app.command(name="verify-merge")
def verify_merge(
    repo: Path = typer.Option(Path("."), help="Repo root (default: cwd)."),
    branch: str | None = typer.Option(
        None, help="Branch whose merge to verify (default: the single active workspace)."
    ),
    default_branch: str = typer.Option(
        "main", "--default-branch", help="Base branch the PR merged into."
    ),
) -> None:
    """Verify a merged branch's changes actually reached the base branch.

    Squash/rebase/merge-safe: checks content presence on `origin/<default>`,
    not commit ancestry (the #320 close-out). Exit 1 if not verified — the fix
    may have orphaned (a commit pushed after the PR merged).
    """
    root = _resolve_repo(repo)
    if branch is None:
        states = list_states(root)
        if len(states) > 1:
            _fail(
                IsolationError(
                    "multiple isolation workspaces — specify --branch: "
                    + ", ".join(s.branch for s in states)
                )
            )
            return
        state = states[0] if states else None
    else:
        state = load_state(root, branch)
    if state is None:
        _fail(
            IsolationError(
                f"no isolation workspace for branch {branch!r}."
                if branch is not None
                else "no isolation workspace — run `fr isolation up` first."
            )
        )
        return
    target = _target_or_exit(root)
    _refuse_external(target, "verify-merge")
    res = _worktree_ops(target).verify_merge(state, default_branch=default_branch)
    if res["verified"]:
        typer.echo(
            f"verify-merge: {res['branch']} ✓ changes present on "
            f"origin/{default_branch}, PR MERGED."
        )
        return
    reasons = []
    if not res["fetched"]:
        reasons.append(f"could not fetch origin/{default_branch} (check may be stale)")
    if not res["changes_present"]:
        reasons.append(f"changes missing from origin/{default_branch}: {res['missing']}")
    if res["pr_state"] != "MERGED":
        reasons.append(f"PR state is {res['pr_state']} (expected MERGED)")
    typer.echo(
        f"verify-merge: {res['branch']} ✗ NOT verified — {'; '.join(reasons)}. "
        "Do NOT declare done; recover (cherry-pick onto the base branch / open a fresh PR).",
        err=True,
    )
    raise typer.Exit(1)


@isolation_app.command()
def gc(
    repo: Path = typer.Option(Path("."), help="Repo root (default: cwd)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Classify + report only; mutate nothing."
    ),
    format: str = typer.Option("text", "--format", help="text | json"),
) -> None:
    """Reconcile isolation workspaces host-wide (#354).

    Tears down workspaces whose PR merged, skips open PRs, warns on no-PR work,
    and reaps orphaned containers — so end-to-end runs no longer depend on a
    human running `down` in the originating session. Fired opportunistically on
    every up/down; also runnable standalone or on a schedule.

    Works in EVERY mode (#423), because unattended automation must not have to
    special-case one:

    - devcontainer — the full sweep (workspaces, orphaned containers, dangling
      `vsc-*` images);
    - host-worktree — the same sweep minus the docker-only steps;
    - external — a non-destructive `external` verdict; the preparer owns the
      cleanup of a checkout fr merely adopted.

    `--repo` names the repo whose state records to reconcile (the fr worktree
    cache and docker labels are host-wide regardless) — cron and agent runs
    cannot rely on cwd.
    """
    target = _target_or_exit(_resolve_repo(repo))
    actions = _gc_ops(target).gc(dry_run=dry_run)
    if format == "json":
        typer.echo(json.dumps([asdict(a) for a in actions], indent=2))
        return
    if not actions:
        typer.echo("gc: no isolation workspaces.")
        return
    for a in actions:
        line = f"gc: {a.branch or a.worktree}: {a.verdict} → {a.action}"
        if a.detail:
            line += f" ({a.detail})"
        typer.echo(line)
