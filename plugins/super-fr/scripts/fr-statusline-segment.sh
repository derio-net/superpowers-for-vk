#!/bin/bash
# fr-statusline-segment.sh — status-line segment (spec 2026-09-04 §5.E, d4).
#
# stdin:  Claude Code status-line JSON ({session_id, workspace.current_dir, cwd}).
# stdout: exactly two lines, either may be empty —
#   1  iso segment:    "iso: <bound worktree>"   session bound elsewhere than cwd
#                      "iso: ? (feat/x, feat/y)" unbound, repo has fr workspaces
#                      ""                        bound to cwd / no workspaces / no repo
#   2  worktree gauge: "worktrees (N): b:rel, b2:rel[, +K more]" | ""
#      N counts every worktree of the repo; the list omits the cwd's own and the
#      bound one. rel is relative to the MAIN checkout when the cwd is inside
#      that checkout and the worktree lives under it; else ~-relative; else
#      absolute. Detached HEAD shows the 7-char sha instead of a branch.
#
# Env: FR_SESSIONS_DIR (default ~/.cache/fr/sessions) — per-session index;
#      STATUSLINE_WT_WIDTH (default 110) — width budget of the gauge list.
#
# Shell + jq + git only. NEVER the fr CLI (4 s). Spec budget 60 ms; measured
# ~100 ms warm on a Mac with a real git first on PATH, ~155 ms behind Apple's
# /usr/bin/git shim (each git call costs ~40 ms there) — put a real git first
# on PATH in the caller. Process count: 1 jq (stdin) + 2 git + 1-2 jq (files).
# Every failure path degrades to empty lines and exit 0 — a status line must
# never break the harness.
set -u

# One jq pass over stdin yields both fields (one line each; empty when absent).
{ IFS= read -r session_id; IFS= read -r cwd; } < <(
  jq -r '(.session_id // ""), (.workspace.current_dir // .cwd // "")' 2>/dev/null || true
)
sessions_dir="${FR_SESSIONS_DIR:-$HOME/.cache/fr/sessions}"
iso=""
gauge=""

# One git call for both the cwd's toplevel and the shared common dir.
toplevel=""
common=""
[ -n "${cwd:-}" ] && [ -d "$cwd" ] &&
  { IFS= read -r toplevel; IFS= read -r common; } < <(
    git -C "$cwd" --no-optional-locks rev-parse --show-toplevel --git-common-dir 2>/dev/null || true
  )
if [ -z "${toplevel:-}" ] || [ -z "${common:-}" ]; then
  printf '%s\n%s\n' "$iso" "$gauge"
  exit 0
fi

case "$common" in /*) ;; *) common="$toplevel/$common" ;; esac
common=$(cd "$common" 2>/dev/null && pwd -P)
main=$(dirname "$common") # main checkout: the common dir is <main>/.git

# --- iso segment: the session's bound worktree, from ONE index file read ---
bound=""
bound_phys=""
if [ -n "$session_id" ] && [ -f "$sessions_dir/$session_id.json" ]; then
  bound=$(jq -r '.worktree // empty' "$sessions_dir/$session_id.json" 2>/dev/null || true)
fi
if [ -n "$bound" ]; then
  bound_phys=$(cd "$bound" 2>/dev/null && pwd -P || printf '%s' "$bound")
  [ "$bound_phys" != "$toplevel" ] && iso="iso: $bound"
elif [ -d "$common/fr/isolation" ]; then
  # One jq over every state file; an empty dir leaves the glob literal -> no output.
  branches=$(jq -rn '[inputs.branch // empty] | join(", ")' "$common"/fr/isolation/*.json 2>/dev/null || true)
  [ -n "$branches" ] && iso="iso: ? ($branches)"
fi

# --- worktree gauge: every worktree except the cwd's own and the bound one ---
rel() { # $1 = absolute worktree path
  case "$toplevel/" in
  "$main"/*)
    case "$1" in
    "$main"/?*)
      printf '%s' "${1#"$main"/}"
      return
      ;;
    esac
    ;;
  esac
  case "$1" in
  "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;;
  *) printf '%s' "$1" ;;
  esac
}

entries=()
cur_path=""
cur_branch=""
cur_head=""
_flush() {
  [ -z "$cur_path" ] && return
  local b="$cur_branch"
  [ -z "$b" ] && b="${cur_head:0:7}"
  entries+=("${cur_path}|${b}")
  cur_path=""
  cur_branch=""
  cur_head=""
}
while IFS= read -r line; do
  case "$line" in
  "worktree "*)
    _flush
    cur_path="${line#worktree }"
    ;;
  "HEAD "*) cur_head="${line#HEAD }" ;;
  "branch refs/heads/"*) cur_branch="${line#branch refs/heads/}" ;;
  bare) cur_branch="bare" ;;
  esac
done < <(git -C "$toplevel" --no-optional-locks worktree list --porcelain 2>/dev/null)
_flush

total=${#entries[@]}
if [ "$total" -gt 1 ]; then
  budget=${STATUSLINE_WT_WIDTH:-110}
  plain=0
  shown=0
  list=""
  others=()
  for e in "${entries[@]}"; do
    p="${e%%|*}"
    [ "$p" = "$toplevel" ] && continue
    [ -n "$bound_phys" ] && [ "$p" = "$bound_phys" ] && continue
    others+=("$e")
  done
  n=${#others[@]}
  for e in "${others[@]}"; do
    token="${e##*|}:$(rel "${e%%|*}")"
    sep=0
    [ "$shown" -gt 0 ] && sep=2
    if [ "$shown" -gt 0 ] && [ $((plain + sep + ${#token})) -gt "$budget" ]; then
      list="$list, +$((n - shown)) more"
      break
    fi
    [ "$shown" -gt 0 ] && list="$list, "
    list="$list$token"
    plain=$((plain + sep + ${#token}))
    shown=$((shown + 1))
  done
  [ "$n" -gt 0 ] && gauge="worktrees ($total): $list"
fi

printf '%s\n%s\n' "$iso" "$gauge"
