# Worktree traceability and conformity — design

Status: draft (fr-brainstorming, 2026-09-04)
Branch: `feat/worktree-traceability`
Operator decisions recorded in §4 (d1–d4).

## 1. Goal

Make it possible to answer, from disk and in under 50 ms, **"which worktree is
this agent session working in?"** — and then bring the three worktree
creators on an operator machine onto fr's location and state so the answer is
the same regardless of who created the worktree.

Traceability first (the operator's stated priority), conformity second.

Concretely, after this ships:

- every fr workspace records the sessions attached to it, and every session
  has one small file naming its workspace;
- the Claude Code status line shows the session's isolation path next to the
  checked-out branch and cwd, and lists the repo's other worktrees;
- `claude --worktree`, `EnterWorktree`, and desktop parallel sessions create
  **fr** workspaces (fr location, fr state, session bound) instead of
  `.claude/worktrees/`;
- `fr isolation up` files the workspace under the **main checkout's** name
  even when invoked from inside another worktree;
- the superpowers `using-git-worktrees` skill is routed to `fr-isolation` in
  fr-enabled repos, the same way `writing-plans` is routed to `fr-plan`.

### Non-goals

- Migrating existing hand-made worktrees (frank's `.worktrees/*`) into fr.
  They stay where they are; the status line's worktree gauge still lists them.
- Routing **subagent** worktrees (`Agent … isolation: "worktree"`, names
  `agent-<id>`) through fr (decision d2: sessions only).
- A host-wide registry of workspaces. State stays per-repo in the git common
  dir; the per-session file is an index into it, not a second source of truth.
- Liveness detection of sessions. fr records attach/detach events; it does
  not poll harness processes.
- Hermes / OpenCode transports. The engine verbs are harness-neutral; wiring
  their hooks is a follow-up (the Hermes compat spec already has the shape).

## 2. Background — what exists today (verified 2026-09-04 on the operator Mac)

Four creators, three locations, no session link for fr:

| Creator | Location | State on disk | Session link |
|---|---|---|---|
| `fr isolation up` | `~/.cache/fr/worktrees/<cwd-basename>/<branch-slug>` | `<common-dir>/fr/isolation/<slug>.json` (`repo_root, branch, worktree, profile, created_at`) | none |
| Claude native (`--worktree`, `EnterWorktree`, desktop) | `<repo>/.claude/worktrees/<name>` | Claude-internal (session ↔ worktree binding; `worktree.*` in status-line JSON) | only visible to that session |
| Agent tool `isolation: "worktree"` | `<repo>/.claude/worktrees/agent-<id>` | Claude-internal, swept by `cleanupPeriodDays` | subagent only |
| superpowers `using-git-worktrees` | `<repo>/.worktrees/<name>` | none | none |

Two fr-side traceability defects:

1. **Basename-keyed cache folder.** `LocalTarget._worktree_up_core` computes
   `~/.cache/fr/worktrees/<self.repo_root.name>/…`. Invoked from inside a
   native agent worktree or a nested fr worktree, `repo_root.name` is
   `agent-a9338827a0b031c06` or the branch slug. Five orphaned, now-empty
   folders at the cache root are the residue (`agent-a9338827a0b031c06`,
   `agent-ac8817b7fc004a0f0`, `feat__bridge-reset-hard-sync`, and two
   `test_*` dirs). `_worktree_dirs()` also happily scans them.
2. **The session sentinel knows no branch.** `fr-pipeline-sentinel.sh` writes
   `~/.cache/fr/sentinels/<session_id>.json = {repo_root, skill, started_at}`.
   Its *presence* is what arms `fr-isolation-guard.sh`. Nothing on the machine
   maps a session to a workspace, which is why the status line shows `main`
   for eleven live sessions whose real work is in `~/.cache/fr/worktrees`.

What Claude Code gives us (docs verified: `hooks`, `worktrees`, `statusline`):

- every hook receives `session_id`, `cwd`, `transcript_path`; a `SessionEnd`
  event exists (matchers `clear|resume|logout|prompt_input_exit|other`);
- `WorktreeCreate` **replaces** native creation entirely: input adds `name`
  (operator-given or generated, `agent-<id>` for subagents); the hook prints
  the worktree path as the last stdout line; a non-zero exit aborts creation.
  `WorktreeRemove` gets `worktree_path`. Hook-based worktrees: no
  `.worktreeinclude` processing, transcript stays at the launch dir,
  `worktree.branch` absent from status-line JSON, `worktree.path` present;
- the status line receives `session_id`, `cwd`, `workspace.git_worktree`,
  `workspace.added_dirs`, `worktree.{name,path}`;
- there is **no** `CLAUDE_SESSION_ID` env var for the Bash tool. Only hooks
  know the session id. (`CLAUDE_CODE_BRIDGE_SESSION_ID` is the Remote Control
  id, a different identifier.)

Cost budget for the status line, measured: `git worktree list` 50 ms;
`fr isolation status --format json` 4.2 s (docker + gh). The status line must
never call the fr CLI.

## 3. Principle — engine vs transport

Per the org rule (`no-claude-p-batch.md`, "separate the engine from the LLM
transport"): fr grows harness-neutral **verbs** that record bindings; Claude
Code **hooks** are one transport that calls them. Hermes shell-hooks or
OpenCode can call the same verbs. The status line reads files, never the CLI.

## 4. Operator decisions (asked once, 2026-09-04)

- **d1 Binding location:** fr state **and** a per-session file (chosen:
  "fr state + sentinel"). Refinement in §5.A: the per-session binding lives in
  a sibling file, not inside the sentinel, because the sentinel's *presence*
  arms the Bash guard — enriching it would arm the guard for every session
  that merely ran `fr isolation up`.
- **d2 Native worktrees:** fr owns `WorktreeCreate` for **sessions only**;
  `agent-*` names keep Claude's default shape.
- **d3 Repo key:** main-checkout basename, resolved through the git common
  dir. Existing workspaces keep working (state stores absolute paths); orphan
  folders get a one-time gc sweep.
- **d4 Status line:** line 2 = `branch | full cwd (| iso: full isolation
  path)`; line 3 = the repo's other worktrees with relative paths.

## 5. Design

### A. Session binding — engine (`packages/fr`)

**State model.** `IsolationState` gains

```python
class SessionBinding(BaseModel):
    session_id: str
    harness: str            # "claude" | "hermes" | "opencode" | "unknown"
    attached_at: str        # ISO-8601 UTC

class IsolationState(BaseModel):
    ...
    sessions: list[SessionBinding] = []   # default keeps old files loadable
```

`save_state` already rewrites the whole file; `load_state`/`list_states`
tolerate the missing key. A session is bound to **at most one** workspace at
a time (mirrors `exec`'s "single active workspace" rule); attaching elsewhere
moves the binding.

**Per-session index.** `~/.cache/fr/sessions/<session_id>.json`
(override `FR_SESSIONS_DIR`):

```json
{
  "session_id": "7da57b3e-…",
  "harness": "claude",
  "repo_root": "/Users/x/Docs/projects/DERIO_NET/super-fr",
  "branch": "feat/worktree-traceability",
  "worktree": "/Users/x/.cache/fr/worktrees/super-fr/feat__worktree-traceability",
  "profile": "dev",
  "attached_at": "2026-09-04T10:12:00Z"
}
```

This is an **index**, derived from state; if the two disagree, state wins and
`fr isolation status` says so. The sentinel dir and its guard semantics are
untouched.

**Verbs** (`fr isolation …`):

- `attach --session <id> [--repo <path>] [--branch <b>] [--harness <h>]` —
  resolves the workspace like `exec` does (explicit `--branch`, else the
  single active workspace, else error), appends/refreshes the binding in
  state, writes the session file. Idempotent.
- `detach --session <id> [--branch <b> | --all]` — removes the binding from
  state and deletes the session file. Missing binding is a no-op, exit 0.
- `status` gains a `sessions=` column in text and a `sessions: [...]` array
  in `--format json`; `--session <id>` filters to that session's workspace.
- `down` prints the attached sessions other than the caller (`--session` is
  optional on `down`; when given, that session is detached first) as a
  warning; it does **not** refuse — liveness is unknowable here. `down`
  always detaches all sessions of the workspace it removes.
- `up` accepts `--session <id> [--harness <h>]` and attaches in the same
  call (used by the WorktreeCreate transport, saves a second CLI start).
- `gc` prunes session files whose `worktree` no longer exists or whose
  workspace state no longer lists them.

### B. Claude Code transports (`plugins/super-fr/hooks/`)

All hooks are `set -eu`, `jq`-only, exit 0 on anything unexpected (never
block the harness), and route through the installed `fr` binary.

1. **`fr-session-bind.sh` — PostToolUse(Bash).** Parses `tool_input.command`
   with the same `cd <dir> && ` folding the guard uses. Matches, start-anchored:
   - `fr isolation up …` / `fr isolation exec …` → `fr isolation attach
     --session $session_id --repo <cwd or folded cd> [--branch <parsed
     --branch value>] --harness claude`
   - `fr isolation down …` → `fr isolation detach --session $session_id
     [--branch <parsed>]` (down already detached everyone; this only cleans
     the index if the down targeted another repo's workspace).
   Runs only when the tool succeeded (`tool_response` carries no error). Not
   fired for subagents (`agent_id` present → exit 0), so a phase executor's
   `exec` calls do not rebind the orchestrator's session.
2. **`fr-session-unbind.sh` — SessionEnd.** `fr isolation detach --session
   $session_id --all`. Matcher: all reasons except `resume` (a resumed session
   keeps its id and its worktree).
3. **`fr-worktree-create.sh` — WorktreeCreate.** Input `name`:
   - `agent-*` → **mimic Claude's default**: `git -C <repo> worktree add
     --detach <repo>/.claude/worktrees/<name> <base>` where `<base>` is
     `origin/HEAD` if it resolves, else `HEAD`; copy `.worktreeinclude`
     matches with `git ls-files --others --ignored --exclude-standard` +
     the include patterns (best effort). Print the path.
   - otherwise → `fr isolation up --repo <cwd> --branch <branch> --session
     $session_id --harness claude --print-path`, where `<branch>` is
     `wt/<name>` unless `name` already contains a `/`. `up` must be
     **idempotent for an existing branch+worktree** (Claude re-runs the hook
     when a name is reused). If the repo has no devcontainer profile and no
     `FR_ISOLATION_TARGET`, the hook exports `FR_ISOLATION_TARGET=worktree`
     for this call — a `--worktree` launch must not hard-stop on the fr-init
     interview. `--print-path` makes the final stdout line the worktree path
     and pushes everything else to stderr.
   - non-git cwd or non-fr-enabled repo → mimic default for any name (the
     hook cannot decline once registered).
4. **`fr-worktree-remove.sh` — WorktreeRemove.** `worktree_path` under
   `<repo>/.claude/worktrees/agent-*` → `git worktree remove --force`.
   Otherwise → `fr isolation down --worktree <path>` (new resolver: state
   lookup by worktree path via the existing `_resolve_state`). If `down`
   refuses (open PR), the worktree is left in place and the reason goes to
   stderr; Claude logs hook failures only in debug mode, which is acceptable
   because the operator chose "remove" at exit and `fr isolation status`
   still shows the workspace.

`hooks.json` registers 1–4. `fr-pipeline-sentinel.sh` is unchanged.

### C. Repo key — main-checkout basename (`packages/fr`)

`_worktree_up_core`: `name = _git_common_dir(self.repo_root).parent.name`
when the common dir's basename is `.git`, else `self.repo_root.name` (bare
or unusual layouts). Same helper feeds `_worktree_dirs()`.

`gc` gains an `empty-repo-dir` verdict: a child of `~/.cache/fr/worktrees`
that is a directory with no subdirectories is removed (`--dry-run` reports
`would-remove`). One sweep clears the five orphans listed in §2.

### D. Superpowers routing — `rules/fr-worktree-override.md`

New plugin rule, installed by `scripts/install.sh` next to
`fr-plan-override.md`:

> In an fr-enabled repo, when any skill references
> `superpowers:using-git-worktrees`, invoke `fr-isolation` instead
> (`fr isolation up --branch <branch>`); never create `.worktrees/`.
> `EnterWorktree` needs no override: the `WorktreeCreate` hook already lands
> it in fr.

### E. Status line — plugin-shipped segment + operator script

super-fr ships `plugins/super-fr/scripts/fr-statusline-segment.sh`: reads the
status-line JSON on stdin, looks up `~/.cache/fr/sessions/<session_id>.json`,
and prints two lines (either may be empty):

- **iso segment** — `iso: <worktree>` when the session is bound and the
  bound worktree is not the cwd's own toplevel (a session *inside* its
  workspace already shows it as cwd). Unbound session in a repo that has
  workspaces → `iso: ? (feat/x, feat/y)` dimmed, from
  `<common-dir>/fr/isolation/*.json`.
- **worktree gauge** — `worktrees (N): feat/x:<rel>, docs/blog:<rel>` for
  every worktree of the repo except the cwd's and the bound one; `<rel>` is
  relative to the main checkout when the worktree is inside it
  (`.worktrees/blog-rewrite`, `.claude/worktrees/agent-…`), else
  `~`-relative. Width-bounded exactly as today (`STATUSLINE_WT_WIDTH`).

The operator's `~/.claude/statusline.sh` calls the segment script and lays
out line 2 as `branch | ~/cwd | iso: …` and line 3 as the gauge. Native
fr-created worktree sessions (§B.3) need nothing extra: their cwd is the
workspace, and the session file confirms it. Detached HEAD (native agent
worktrees) shows the short SHA instead of a blank branch.

Only shell + `jq` + `git`: budget ≤ 60 ms, no Python, no docker, no gh.

### F. Docs

- `plugins/super-fr/skills/fr-isolation/SKILL.md`: attach/detach, the
  session file, the WorktreeCreate behaviour, `--print-path`.
- `plugins/super-fr/rules/fr-isolation-required.md`: note that session
  bindings are traceability, not enforcement (the gate is unchanged).
- README "Isolation" section: the location table from §2, after.

## 6. Risks & mitigations

- **Stale bindings** (crash, `SessionEnd` not fired): status line shows a
  workspace the session no longer uses. Mitigation: `attached_at` shown in
  `status`; `gc` prunes bindings whose worktree is gone; a fresh `attach`
  overrides.
- **Mimic drift** for `agent-*` worktrees: Claude may change its default
  layout or `baseRef` handling. Mitigation: the mimic is ~10 lines, tested
  against `worktree.baseRef` unset/`head`; documented as best-effort.
- **Hook-based worktree semantics**: no `.worktreeinclude`, transcript at
  launch dir, `worktree.branch` absent. Mitigation: fr's `up` already copies
  the secrets env-file; the session file supplies the branch.
- **`--worktree` in a devcontainer-mode repo without a profile** silently
  degrades to host-worktree mode. Mitigation: the hook writes one stderr line
  saying so; `status` shows `profile=-`.
- **PostToolUse parsing** of `fr isolation …` commands is heuristic.
  Mitigation: start-anchored patterns shared with the guard; `up --session`
  makes the WorktreeCreate path parser-free; a mis-parse yields a no-op
  attach, never a wrong binding (attach without `--branch` errors on
  ambiguity and the hook exits 0).

## 7. Test plan

Unit (`packages/fr/tests`): SessionBinding round-trip and old-file load;
attach/detach idempotency and single-binding move; `up --session`;
`down --worktree` resolution; repo-key helper for main checkout, linked
worktree, nested fr worktree, bare; gc `empty-repo-dir` and session-file
prune. Hook tests (`tests/hooks`, bash + jq fixtures): bind on `up`/`exec`
with and without `cd … &&`, ignore subagent input, unbind on SessionEnd,
WorktreeCreate `agent-*` vs named, WorktreeRemove both branches. Segment
script: golden-output tests fed mock status-line JSON for the three session
shapes (base clone bound, base clone unbound with workspaces, inside a
native worktree). Tripwire: `.fr-isolation` never tracked (existing).

Operator-driven, post-merge: `claude --worktree demo` in super-fr lands under
`~/.cache/fr/worktrees/super-fr/wt__demo` with the session bound; the status
line in this very session shows `iso: …/feat__worktree-traceability`.

## Implementation Plans

| Plan | Repo | File | Depends on |
|---|---|---|---|
| 2026-09-04-worktree-traceability | `derio-net/super-fr` | `2026-09-04-worktree-traceability` | — |

## 8. Acceptance rows (born here; presented at spec review)

| id | capability | acceptance | level |
|---|---|---|---|
| `session-workspace-binding` | isolation-traceability | An operator can ask fr which workspace an agent session is attached to, and which sessions hold a workspace, without inspecting the harness. | unit + hook |
| `statusline-shows-bound-workspace` | isolation-traceability | The Claude Code status line shows the session's isolation path alongside the checked-out branch and cwd, and lists the repo's other worktrees, in under 60 ms. | golden |
| `native-worktree-sessions-land-in-fr` | isolation-conformity | `claude --worktree <name>` and `EnterWorktree` in an fr-enabled repo create an fr workspace at the fr location with the session bound; subagent worktrees keep Claude's default shape. | hook |
| `nested-up-files-under-main-checkout` | isolation-conformity | `fr isolation up` run from inside any worktree of a repo files the workspace under the main checkout's name. | unit |
| `gc-reaps-empty-repo-dirs` | isolation-hygiene | `fr isolation gc` removes empty repo folders and stale session files from the fr cache. | unit |
| `superpowers-worktree-skill-routed-to-fr` | isolation-conformity | In an fr-enabled repo, a request that would invoke `using-git-worktrees` is routed to `fr-isolation`; no `.worktrees/` directory is created. | rule + tripwire |
