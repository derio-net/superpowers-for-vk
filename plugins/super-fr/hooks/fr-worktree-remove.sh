#!/bin/bash
# WorktreeRemove (spec 2026-09-04 §5.B.4). agent-* worktrees: plain git removal.
# Everything else: `fr isolation down --worktree`, which keeps a workspace with an
# open PR — the reason goes to stderr and `fr isolation status` still shows it.
set -eu
input=$(cat)
path=$(printf '%s' "$input" | jq -r '.worktree_path // empty')
[ -n "$path" ] || exit 0
case "$path" in
  */.claude/worktrees/agent-*)
    common=$(git -C "$path" rev-parse --git-common-dir 2>/dev/null) || { rm -rf "$path"; exit 0; }
    case "$common" in /*) ;; *) common="$path/$common" ;; esac
    git -C "$(dirname "$common")" worktree remove --force "$path" >&2 || rm -rf "$path"
    exit 0 ;;
esac
command -v fr >/dev/null 2>&1 || exit 0
fr isolation down --worktree "$path" >&2 ||
  echo "fr-worktree-remove: fr isolation down refused for $path; workspace kept (see fr isolation status)" >&2
exit 0
