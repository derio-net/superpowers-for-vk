# fr-isolation Required — Edit/Write Enforcement (Org-Wide)

## Rule

In an **fr-enabled** repo, edits to tracked source/docs must happen **inside an
fr-isolation workspace**, never the base clone. This is enforced by the
PreToolUse hook `fr-isolation-required.sh` (shipped by super-fr, registered via
the plugin's `hooks.json`), which gates **Edit / Write / MultiEdit /
NotebookEdit** — you can't commit what you can't write.

A repo is **fr-enabled** when it has a `.devcontainer/<profile>/` profile or a
`docs/superpowers/plans/` directory.

## Why

fr-isolation used to be prose ("fr-brainstorming / fr-goal enter isolation
first, so the base repo is never touched"). Prose gets bypassed under load: a
session enters isolation for the brainstorm, then *wanders* — follow-up edits in
another repo, later turns, host-side chores — all landing on the **base clone**
with no gate forcing re-entry. This hook is the tool-layer backstop, mirroring
`agent-worktree-default.md` (Agent tool — but see the `fr-phase-executor`
carve-out below) and `fr-isolation-guard.sh` (Bash tool). It is
session-independent: it fires on any edit to an fr-enabled repo, even when no
pipeline skill ran this session.

## How it decides (fail-closed)

1. Not an edit tool, or `FR_BASE_OK=1` set → allow.
2. Target file not in a git repo, or repo not fr-enabled → allow.
3. **Valid `.fr-isolation` marker at the repo toplevel → allow.** Valid =
   present, recorded `toplevel` == the file's actual toplevel, **and** the
   marker's `mode` passes its own check:
   - `worktree` (devcontainer **or** host-worktree mode — an fr linked
     worktree either way) → the toplevel must be a real **linked worktree**
     (`git rev-parse --git-common-dir` ≠ `--git-dir`). Defeats a stale marker
     copied into the primary working tree.
   - `external` (preparer-adopted container) → the toplevel match **plus
     container evidence** — any of `/.dockerenv`, `/run/.containerenv`, or
     `$KUBERNETES_SERVICE_HOST`. A marker forged on a bare host never
     validates; a marker copied to the Mac base clone fails the toplevel match
     (the container checkout path can't equal the base-clone path).
   - any other `mode` → fail closed.
4. Path matches a glob in `.fr-isolation-allow` (below) → allow.
5. Otherwise → **deny** with a message naming the three ways forward.

`fr isolation up` writes the marker (and adds it to `info/exclude`); `down`
removes it. The marker is **never** committed — it is gitignored and a CI
tripwire fails if it is ever tracked.

## Scope — what fr-isolation does NOT protect

Read step 2 above literally: **not in a git repo, or not fr-enabled → allow.**
fr-isolation answers exactly one question — *"is this fr work happening in an
fr-enabled repo's base clone instead of its workspace?"* — and it is deliberately
silent on everything else. It is **not** a filesystem sandbox, a credential
boundary, or a secrets firewall. Concretely, all of these are allowed and are
meant to be:

- reading or writing `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config`, or any other
  path outside a git repo — including when `$HOME` is itself a **dotfiles git
  repo**, since that repo is not fr-enabled;
- any command at all in a session with no active pipeline sentinel — the bash
  guard exits immediately when there is none;
- any work in a repo that never opted into fr;
- **anything riding on an already-permitted `fr …` command.** The bash guard's
  allowances are start-anchored but not end-anchored, so
  `fr isolation status && <anything>` is allowed as a unit. Closing that would
  mean rejecting `fr isolation exec -- 'a && b'`, a legitimate and common shape,
  so it stays open deliberately. (A leading `cd` is *not* a way to extend this:
  the `cd` is only folded into the match when it lands inside the pipeline's own
  repo or on another repo's toplevel — never on an arbitrary directory.)

The `fr-isolation-guard.sh` cross-repo rules (super-fr#421) narrow *where a live
pipeline may hop*; they are discipline, and must not be read as containment.

**If you want those paths actually protected, that is the harness permission
layer's job, not fr's** — `permissions.deny` in `~/.claude/settings.json` for
Claude Code (e.g. `Read(./.ssh/**)`, `Bash(cat ~/.ssh/*)`), or the equivalent in
your harness. fr ships no such rules and should not: it has no idea what is
sensitive on your machine.

**Session bindings are traceability, not enforcement.** `fr isolation attach`
and `up --session` record which harness session holds a workspace (in the
workspace state plus `~/.cache/fr/sessions/<session-id>.json`) so that `status`
and the status line can answer "where is this session's work?". The edit gate
above never consults a binding: it reads the `.fr-isolation` marker only, so an
unbound session inside a valid workspace edits freely and a bound session in the
base clone is still denied. A missing or stale binding changes nothing here.

## Three isolation modes

The marker's `mode` records who owns the environment; the edit-gate only cares
that the workspace is a genuine isolation, per the checks above.

- **devcontainer** (default, operator machines): fr linked worktree +
  devcontainer + secrets env-file. Marker `mode: worktree`.
- **host-worktree** (`FR_ISOLATION_TARGET=worktree`, docker-less pods/CI): fr
  linked worktree, the host process env as-is, no profile, no fr-provisioned
  secrets. Marker `mode: worktree` — same enforcement surface as devcontainer.
- **external** (`mode: external`): the **preparer** (k8s operator, image build,
  attach script) writes the marker itself at its checkout toplevel — the
  hand-off artifact whose meaning is "this container is contained and prepared
  for fr." `fr isolation up --branch` adopts it (no second isolation). Container
  evidence in the hook is corroboration only, never a trigger: an unprepared
  container is never silently treated as isolated.

## Escapes

- **Enter isolation** — `fr isolation up --branch <branch>` (or run fr-goal /
  fr-brainstorming / fr-debugging), then edit in the worktree. The right answer
  almost always.
- **`.fr-isolation-allow`** — a globlist at the repo root for operator-managed
  paths that legitimately live in the base clone (data, caches, memory).
  Matched against the file's repo-relative path; `*` spans `/`. Example:
  ```
  # operator-managed, base-clone edits allowed
  projects/**
  data/**
  context/**
  memory/**
  *.local.md
  ```
- **`FR_BASE_OK=1`** — a one-shot env escape for a deliberate base-clone edit
  (e.g. a quick host-side chore you accept is outside isolation).

## Carve-out: `fr-phase-executor` must NOT get its own worktree

The org convention `agent-worktree-default.md` says every code-writing subagent
is dispatched with `isolation: "worktree"`. **`fr-phase-executor` is the one
exception, and it is a hard one** (super-fr#420):

- fr-goal §6 dispatches phase executors **serially into the fr-isolation
  worktree that already exists** — that worktree *is* their isolation. A second
  one is not extra safety, it is a different repo state.
- Given the flag, the executor wakes in a fresh worktree cut from `main`: the
  spec and plan live on the feature branch and are invisible, so `fr pickup` is
  unsatisfiable; Bash is denied by `fr-isolation-guard.sh`; and Edit/Write is
  denied by *this* rule's gate, because a fresh checkout has no marker and the
  gate is fail-closed. **The dispatch still succeeds**, so the run looks healthy
  while every phase does nothing.
- The reason is *not* "this agent is read-only" — it writes code. It is that
  **the two isolation mechanisms are mutually exclusive, not composable.**

fr-goal §3 is the opposite case and keeps the flag: those agents each start a
**fresh** pipeline in a **different** repo, so they need their own workspace.

Enforcement, not prose: `plugins/super-fr/hooks/fr-phase-executor-guard.sh`
(PreToolUse, `Agent|Task`) refuses the combination outright. Harnesses without
an `isolation` argument on their dispatch tool — Hermes' `delegate_task`,
OpenCode — cannot express the poisoned shape and need no hook.

## Rollout (two-file pattern)

Mirrors `agent-worktree-default.md`:

- This operator-level file installs to `~/.claude/rules/fr-isolation-required.md`
  (via super-fr's `install.sh`) and the hook auto-registers through the plugin.
- A repo-level mirror `.claude/rules/fr-isolation-required.md` lives in each
  fr-enabled repo for discoverability (it auto-loads in every clone, including
  pods — keep host-specific paths out of it).
- Each fr-enabled repo should gitignore `.fr-isolation` and carry the
  never-tracked tripwire.
