# Worktree traceability and conformity

Spec: `docs/superpowers/specs/2026-09-04-worktree-traceability-design.md`

## Why

On the operator's machine eleven live Claude sessions sat on `main` in base
clones while their real work happened in `~/.cache/fr/worktrees/…`, and
nothing on disk could say which worktree belonged to which session. fr's own
state (`<git-common-dir>/fr/isolation/<slug>.json`) knows every workspace but
no session; the pipeline sentinel knows the session but no branch. On top of
that, three creators (fr, Claude native worktrees, the superpowers
`using-git-worktrees` skill) put worktrees in three places, and fr itself
mis-files a workspace when `up` runs from inside another worktree because it
keys the cache folder on the cwd's basename.

The operator's priority: traceability first, conformity second.

## Shape of the change

**Engine (fr, harness-neutral).** `IsolationState` grows a `sessions` list of
`SessionBinding{session_id, harness, attached_at}`. A new
`fr.isolation.sessions` module owns `attach` / `detach` / `detach_all` /
`stale_session_indexes`, writing a per-session **index** file
`~/.cache/fr/sessions/<session_id>.json` (override `FR_SESSIONS_DIR`) so a
status line can answer "which workspace is this session in" by reading one
small file. State is the source of truth; the index is derived. New CLI verbs
`fr isolation attach|detach`; `status` shows sessions; `up --session`
binds in the same call; `down` unbinds everyone; `up --print-path` and
`down --worktree <path>` exist for the WorktreeCreate/Remove transport. The
pipeline sentinel is untouched — its presence arms the Bash guard, which is
why the binding lives in a sibling file (decision d1 refinement).

**Repo key.** `_worktree_up_core` keys the cache folder on the main
checkout's basename (via the git common dir), so nested runs file correctly.
`gc` gains `empty-repo-dir` and `stale-session` verdicts.

**Claude transports (plugin hooks).** `fr-session-bind.sh` (PostToolUse
Bash) parses `fr isolation up|exec|down …` — with the guard's leading-`cd`
fold — and calls attach/detach; `fr-session-unbind.sh` (SessionEnd)
detaches; `fr-worktree-create.sh` (WorktreeCreate) turns `claude --worktree`
/ `EnterWorktree` into `fr isolation up --session … --print-path` while
`agent-*` names get Claude's default shape reproduced with plain git;
`fr-worktree-remove.sh` (WorktreeRemove) is the counterpart.

**Status line.** `plugins/super-fr/scripts/fr-statusline-segment.sh` reads
the status-line JSON and prints two lines: the `iso:` segment and the
worktree gauge with relative paths. Shell + jq + git only, no fr CLI.

**Conformity.** `rules/fr-worktree-override.md` routes
`superpowers:using-git-worktrees` to `fr-isolation` in fr-enabled repos, the
way `fr-plan-override.md` routes `writing-plans`. A tripwire keeps
`.worktrees/` and `.claude/worktrees/` out of the tracked tree.

## Phase map

1. Engine: bindings, index, verbs, status/up/down integration.
2. Repo key + gc hygiene.
3. Claude bind/unbind hooks.
4. WorktreeCreate/Remove hooks + `--print-path` + `down --worktree`.
5. Status-line segment script + golden tests.
6. Rule, install wiring, tripwire, docs.
7. `[manual]` operator: install, patch `~/.claude/statusline.sh`, one-time
   gc sweep, verify `claude --worktree`.

Phases 2–5 all depend only on phase 1 and may be executed in any order after
it; phase 6 fans in; phase 7 is operator-side.

## Conventions

- TDD per task: RED test step, GREEN implement step, optional refactor.
- Hook tests run the bash scripts via subprocess with hook-protocol JSON on
  stdin, a stub `fr` on `PATH` that records its argv, and `FR_SESSIONS_DIR`
  / `HOME` pointed at `tmp_path` (mirror `tests/unit/test_hooks_sentinel.py`).
- Run the fr package suite with `uv run pytest -q --no-cov tests/unit/<file>`
  per task and the whole suite before each phase's final commit.
- Never call the fr CLI from the status-line segment; it must stay under
  60 ms.
