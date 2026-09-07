#!/bin/bash
# SessionEnd: drop this session's workspace binding (spec 2026-09-04 §5.B.2).
# `resume` keeps the id and the worktree, so it is not an end for our purposes.
# Transport only — the engine is `fr isolation detach`. Never blocks: exit 0
# on every path.

set -eu

input=$(cat)

command -v jq >/dev/null 2>&1 || exit 0
command -v fr >/dev/null 2>&1 || exit 0

[ "$(printf '%s' "$input" | jq -r '.reason // empty')" != "resume" ] || exit 0

session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')
[ -n "$session_id" ] || exit 0

fr isolation detach --session "$session_id" >/dev/null 2>&1 || true

exit 0
