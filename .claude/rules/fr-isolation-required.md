# fr-isolation Required — repo mirror

In this fr-enabled repo, edits to tracked source/docs belong **inside an
fr-isolation workspace**, never the base clone. A super-fr PreToolUse hook
(`fr-isolation-required.sh`) enforces this on Edit / Write / MultiEdit /
NotebookEdit: it allows the edit only when a valid `.fr-isolation` marker sits
at the repo toplevel, checked by the marker's `mode`:

- `worktree` (devcontainer or host-worktree mode) → toplevel must be a real
  linked worktree.
- `external` (preparer-adopted container) → toplevel match **plus** container
  evidence (`/.dockerenv`, `/run/.containerenv`, or `$KUBERNETES_SERVICE_HOST`),
  so a marker forged on a bare host or copied to the base clone never validates.

`fr isolation up` selects the mode (`FR_ISOLATION_TARGET=worktree` for a
docker-less host; a preparer-written `external` marker is adopted as-is) and
writes the marker; devcontainer mode is the default.

**Scope.** fr-isolation answers one question — *is this fr work happening in an
fr-enabled repo's base clone instead of its workspace?* — and is silent on
everything else. Not in a git repo, or not fr-enabled → allowed. It is **not** a
filesystem sandbox or a credential boundary: `~/.ssh`, `~/.aws` and friends are
outside its scope (including when `$HOME` is a dotfiles git repo, which is not
fr-enabled), and a session with no active pipeline sentinel is ungated by the
bash guard entirely. Protecting those paths is the harness permission layer's
job (`permissions.deny` in `~/.claude/settings.json`), not fr's. Session
bindings (`fr isolation attach`, `up --session`) are traceability only — the
gate reads the marker, never a binding.

To work here:

- Enter isolation — `fr isolation up --branch <branch>` (or run fr-goal /
  fr-brainstorming / fr-debugging) and edit in the worktree.
- For base-clone paths that are operator-managed (data, caches, memory), list
  them in `.fr-isolation-allow` at the repo root (`*` spans `/`).
- For a deliberate one-off base edit, set `FR_BASE_OK=1`.

**Carve-out — `fr-phase-executor` must NOT be dispatched with
`isolation: "worktree"`.** The org `agent-worktree-default` convention says
every code-writing subagent gets its own worktree; this one agent is the
exception, because fr-goal §6 runs phase executors serially *inside the
fr-isolation worktree that already exists* — that worktree IS their isolation,
and the two mechanisms don't compose. With the flag the executor wakes in a
fresh worktree cut from `main` where the spec/plan are invisible and every
Bash/Edit call is denied, yet the dispatch succeeds, so the run looks healthy
while nothing happens (super-fr#420). `fr-phase-executor-guard.sh` refuses it.
fr-goal §3 is the opposite case and keeps the flag — those agents each start a
fresh pipeline in a different repo.

`.fr-isolation` is gitignored and must never be committed (a CI tripwire
enforces this). Full rationale and the operator-level install live in the
shipped rule `~/.claude/rules/fr-isolation-required.md` (super-fr). This mirror
is intentionally host-neutral so it is safe to auto-load in every clone,
including devcontainer pods.
