# Worktree Skill Override (fr-enabled repos)

In a repo with fr plans (`docs/superpowers/plans/`) or devcontainer profiles
(`.devcontainer/<profile>/`), any skill or instruction that would invoke
`superpowers:using-git-worktrees` — "work in a worktree", "isolate this",
executing-plans' isolation step — invokes `fr-isolation` instead:

    fr isolation up --branch <feat/slug>

Never create `<repo>/.worktrees/`. fr owns the location
(`~/.cache/fr/worktrees/<main-checkout>/<branch-slug>`), the state, and the
session binding (`fr isolation status` shows which sessions hold a workspace).

`claude --worktree <name>` and the `EnterWorktree` tool need no override:
super-fr's `WorktreeCreate` hook already lands them in fr (subagent worktrees,
`agent-*`, keep Claude's default shape on purpose). Plain
`using-git-worktrees` remains for non-fr repos.
