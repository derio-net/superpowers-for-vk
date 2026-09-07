#!/usr/bin/env bash
# Canonical super-fr installer, normally invoked by scripts/bootstrap.sh.
# Handles Claude Code marketplace/plugin registration, OpenCode skill/command
# delivery, rules, MCP config, the fr CLI, stale cache cleanup, and the
# PostToolUse hook hint.
set -euo pipefail

# Clean up any .tmp sidecar files on failure so a rerun starts clean.
cleanup_tmps() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    rm -f "${SETTINGS:-}.tmp" "${MCP_CONFIG:-}.tmp" \
          "${KNOWN_MARKETPLACES:-}.tmp" "${INSTALLED_PLUGINS:-}.tmp" 2>/dev/null || true
    echo "install.sh failed (exit $rc). Rerun after fixing." >&2
  fi
  exit "$rc"
}
trap cleanup_tmps EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_DIR="$HOME/.claude"
RULES_DIR="$CLAUDE_DIR/rules"
SETTINGS="$CLAUDE_DIR/settings.json"
MCP_CONFIG="$CLAUDE_DIR/.mcp.json"
VK_MCP_BINARY="$HOME/bin/vibe-kanban-mcp"
# A Claude Code marketplace name is a 1:1 namespace over ONE source repo: its
# manifest (marketplaces/<name>/.claude-plugin/marketplace.json) is a single
# file listing every plugin of that marketplace, and the rsync that populates
# it is `--delete` — replace, never merge. So the name encodes org AND repo.
#
# It used to be the bare org name `derio-net`, which the sibling blog-craft
# repo also claimed; both installers rsync'd their own repo root into the same
# directory and evicted each other. The bare name is now RETIRED — no repo owns
# an org-level namespace — and both installers purge it on sight. See
# docs/superpowers/journals/debug/2026-07-23-marketplace-config-clobber.md.
MARKETPLACE_NAME="derio-net--super-fr"
MARKETPLACE_DIR="$CLAUDE_DIR/plugins/marketplaces/$MARKETPLACE_NAME"
CACHE_BASE="$CLAUDE_DIR/plugins/cache/$MARKETPLACE_NAME"
PLUGIN_NAMES=(super-fr super-fr-dispatch)
# The retired shared namespace. Purged wholesale: with no owner left, every
# `*@derio-net` registration is dangling by definition.
LEGACY_MARKETPLACE_NAME="derio-net"
LEGACY_MARKETPLACE_DIR="$CLAUDE_DIR/plugins/marketplaces/$LEGACY_MARKETPLACE_NAME"
LEGACY_CACHE_BASE="$CLAUDE_DIR/plugins/cache/$LEGACY_MARKETPLACE_NAME"
OPENCODE_SKILLS_DIR="$HOME/.config/opencode/skills"
OPENCODE_COMMANDS_DIR="$HOME/.config/opencode/commands"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGINS_DIR="$CLAUDE_DIR/plugins"
KNOWN_MARKETPLACES="$PLUGINS_DIR/known_marketplaces.json"
INSTALLED_PLUGINS="$PLUGINS_DIR/installed_plugins.json"
# Legacy user-level copies from pre-plugin installs (old vk-* names).
SKILL_NAMES=(vk-plan vk-dispatch vk-execute vk-progress)

if [[ "${1:-}" == "--install-bridge" ]]; then
  # Write the cron wrapper that exec's `python -m fr_vk.bridge`. Hidden by
  # design — there is no `vk bridge` public CLI verb.
  # Default to a user-writable path so operators don't need sudo. The
  # legacy default was /opt/vk-bridge/run.sh — fine for root-owned pod
  # deployments, but painful for shared-pod setups where the bridge runs
  # as the same user as the operator (no write access to /opt). Override
  # with VK_BRIDGE_WRAPPER_PATH=/opt/vk-bridge/run.sh (run via sudo) for
  # the system-path layout.
  wrapper_path="${VK_BRIDGE_WRAPPER_PATH:-$HOME/.local/bin/vk-bridge}"
  mkdir -p "$(dirname "$wrapper_path")"
  # Prefer the active uv tool's interpreter so the wrapper can't pick
  # up a stale system Python that doesn't have vk installed.
  vk_python="$(uv tool dir 2>/dev/null)/fr/bin/python"
  if [ ! -x "$vk_python" ]; then
    # Fallback chain: any `uv run` env, then plain `python3`.
    vk_python="$(uv run --no-project which python 2>/dev/null || command -v python3 || echo /usr/bin/python3)"
  fi
  # The wrapper is only correct if its interpreter can actually import the
  # adapter — verify before writing (review finding, 2026-06-06).
  if ! "$vk_python" -c "import fr_vk.bridge" >/dev/null 2>&1; then
    echo "  ERROR: $vk_python cannot import fr_vk.bridge — bridge wrapper not installed" >&2
    echo "  (re-run after: uv tool install --force --with $PLUGIN_ROOT/packages/fr-vk $PLUGIN_ROOT/packages/fr)" >&2
    exit 1
  fi
  cat > "$wrapper_path" <<EOF
#!/bin/bash
exec "$vk_python" -m fr_vk.bridge "\$@"
EOF
  chmod +x "$wrapper_path"
  echo "Wrapper installed at $wrapper_path"
  echo ""
  echo "To schedule the bridge, add this line to your crontab:"
  echo "*/2 * * * * $wrapper_path"
  exit 0
fi

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "Uninstalling super-fr extras..."
  rm -f "$RULES_DIR/fr-plan-override.md" "$RULES_DIR/vk-plan-override.md"
  rm -f "$RULES_DIR/fr-worktree-override.md"
  echo "  Removed fr/vk plan-override and fr-worktree-override rules"
  if [ -f "$MCP_CONFIG" ] && command -v jq &>/dev/null; then
    if jq -e '.mcpServers.vibe_kanban' "$MCP_CONFIG" &>/dev/null; then
      jq 'del(.mcpServers.vibe_kanban)' "$MCP_CONFIG" > "${MCP_CONFIG}.tmp" && mv "${MCP_CONFIG}.tmp" "$MCP_CONFIG"
      echo "  Removed vibe_kanban from $MCP_CONFIG"
    fi
  fi
  for skill in "${SKILL_NAMES[@]}"; do
    if [ -d "$CLAUDE_DIR/skills/$skill" ] || [ -L "$CLAUDE_DIR/skills/$skill" ]; then
      rm -rf "$CLAUDE_DIR/skills/$skill"
      echo "  Removed stale $CLAUDE_DIR/skills/$skill"
    fi
  done
  if [ -d "$OPENCODE_SKILLS_DIR" ]; then
    for skill_dir in "$PLUGIN_ROOT"/plugins/super-fr/skills/*/; do
      skill="$(basename "$skill_dir")"
      if [ -d "$OPENCODE_SKILLS_DIR/$skill" ]; then
        rm -rf "$OPENCODE_SKILLS_DIR/$skill"
        echo "  Removed $OPENCODE_SKILLS_DIR/$skill"
      fi
    done
  fi
  if [ -d "$OPENCODE_COMMANDS_DIR" ]; then
    for skill_dir in "$PLUGIN_ROOT"/plugins/super-fr/skills/*/; do
      skill="$(basename "$skill_dir")"
      if [ -f "$OPENCODE_COMMANDS_DIR/$skill.md" ]; then
        rm -f "$OPENCODE_COMMANDS_DIR/$skill.md"
        echo "  Removed $OPENCODE_COMMANDS_DIR/$skill.md"
      fi
    done
  fi
  # Hermes: run the uninstall from THIS checkout, not whichever `fr` happens
  # to be installed globally. Upgrade removals may rename shipped inputs; a
  # stale binary then cannot parse the new tree and used to fail silently,
  # leaving active hooks behind while deleting only the skills.
  if [ -d "$HERMES_HOME" ]; then
    if ! command -v uv &>/dev/null; then
      echo "  ERROR: uv is required to remove Hermes hooks with this checkout's fr code" >&2
      exit 1
    fi
    if uv run --project "$PLUGIN_ROOT/packages/fr" fr hermes uninstall --source "$PLUGIN_ROOT" --home "$HERMES_HOME"; then
      echo "  Removed Hermes hooks/rules ($HERMES_HOME)"
    else
      echo "  ERROR: failed to remove Hermes hooks/rules; Hermes skills left intact" >&2
      exit 1
    fi
  fi
  if [ -d "$HERMES_HOME/skills/fr" ]; then
    rm -rf "$HERMES_HOME/skills/fr"
    echo "  Removed $HERMES_HOME/skills/fr"
  fi
  echo "Done. Note: Plugin and PostToolUse hook in settings.json were NOT removed (manual cleanup)."
  exit 0
fi

# Preflight: hard-require jq and uv. Both are used unconditionally downstream;
# continuing past a missing one yields a half-install that looks successful.
for cmd in jq uv rsync git; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: '$cmd' not found in PATH. Install it first." >&2
    exit 1
  fi
done

# Preflight: PLUGIN_ROOT must be a clean checkout of main, in sync with origin.
# This script clobbers $MARKETPLACE_DIR with PLUGIN_ROOT's contents, so anything
# uncommitted, unpushed, or off-main gets baked into the cache. Past incidents
# (cache stuck with a transient "Status: Not Started" revert that broke every
# subsequent `git pull --ff-only`) trace back to running this from a dirty tree.
#
# Escape hatch: integration tests (and only integration tests) set
# VK_INSTALL_SKIP_PREFLIGHT=1 to bypass these checks. CI runs this script from
# a detached HEAD on a PR ref, which would always fail the branch/sync gates.
echo ""
if [ "${VK_INSTALL_SKIP_PREFLIGHT:-}" = "1" ]; then
  echo "Preflight: SKIPPED (VK_INSTALL_SKIP_PREFLIGHT=1 — testing only)"
else
echo "Preflight: validating source repo at $PLUGIN_ROOT..."

if [ ! -d "$PLUGIN_ROOT/.git" ]; then
  echo "ERROR: $PLUGIN_ROOT is not a git checkout." >&2
  echo "  install.sh must be run from a git clone of derio-net/super-fr." >&2
  exit 1
fi

PREFLIGHT_FAILED=0
report_preflight_failure() {
  PREFLIGHT_FAILED=1
  echo "  - $1" >&2
  if [ -n "${2:-}" ]; then
    echo "    Fix: $2" >&2
  fi
}

CURRENT_BRANCH="$(git -C "$PLUGIN_ROOT" symbolic-ref --short HEAD 2>/dev/null || echo "DETACHED")"
if [ "$CURRENT_BRANCH" != "main" ]; then
  report_preflight_failure \
    "Current branch is '$CURRENT_BRANCH', expected 'main'." \
    "git -C $PLUGIN_ROOT checkout main"
fi

if [ -n "$(git -C "$PLUGIN_ROOT" status --porcelain)" ]; then
  report_preflight_failure \
    "Working tree has uncommitted or untracked files." \
    "git -C $PLUGIN_ROOT status   # then commit, stash --include-untracked, or clean"
fi

if ! git -C "$PLUGIN_ROOT" fetch --quiet origin main 2>/dev/null; then
  report_preflight_failure \
    "Could not fetch origin/main." \
    "check network/SSH access to origin"
else
  LOCAL_SHA="$(git -C "$PLUGIN_ROOT" rev-parse HEAD)"
  ORIGIN_SHA="$(git -C "$PLUGIN_ROOT" rev-parse origin/main)"
  if [ "$LOCAL_SHA" != "$ORIGIN_SHA" ]; then
    BEHIND="$(git -C "$PLUGIN_ROOT" rev-list --count HEAD..origin/main)"
    AHEAD="$(git -C "$PLUGIN_ROOT" rev-list --count origin/main..HEAD)"
    if [ "$BEHIND" -gt 0 ] && [ "$AHEAD" -eq 0 ]; then
      report_preflight_failure \
        "Local main is behind origin/main by $BEHIND commit(s)." \
        "git -C $PLUGIN_ROOT pull --ff-only"
    elif [ "$AHEAD" -gt 0 ] && [ "$BEHIND" -eq 0 ]; then
      report_preflight_failure \
        "Local main is ahead of origin/main by $AHEAD commit(s) (unpushed work)." \
        "git -C $PLUGIN_ROOT push origin main"
    else
      report_preflight_failure \
        "Local main has diverged from origin/main (ahead $AHEAD, behind $BEHIND)." \
        "reconcile (rebase/merge/reset) before installing"
    fi
  fi
fi

if [ "$PREFLIGHT_FAILED" -ne 0 ]; then
  echo "" >&2
  echo "Preflight failed. install.sh refuses to run from a dirty / out-of-sync source" >&2
  echo "because it clobbers \$MARKETPLACE_DIR with PLUGIN_ROOT's contents — anything" >&2
  echo "uncommitted ends up baked into the cache." >&2
  exit 1
fi
echo "  OK: on main, clean, in sync with origin/main"
fi  # end VK_INSTALL_SKIP_PREFLIGHT guard

# VK MCP binary is optional — warn but continue if missing.
if [ ! -x "$VK_MCP_BINARY" ]; then
  echo "WARNING: VK MCP binary not found at $VK_MCP_BINARY" >&2
  echo "  MCP server configuration will be skipped." >&2
  echo "  Install it later: see https://github.com/derio-net/vibe-kanban" >&2
  SKIP_MCP=true
else
  SKIP_MCP=false
fi

echo ""
echo "Installing super-fr..."

# 2. Register the marketplace so the plugin system knows where to find it.
#
# These writes are UNCONDITIONAL, not skip-if-present. `if ! jq -e '."<key>"'`
# reads as idempotence but means first-writer-wins: a wrong `source.repo` left
# by anyone else survives every reinstall, and a later
# `/plugin marketplace update` then re-fetches the wrong repo. Idempotence for
# a key we own means converging on our value, not deferring to whatever is
# already there.
echo ""
echo "Registering marketplace..."
if command -v jq &>/dev/null; then
  MARKETPLACE_SOURCE='{"source":"github","repo":"derio-net/super-fr"}'

  # Add to extraKnownMarketplaces in settings.json
  if [ -f "$SETTINGS" ]; then
    jq --arg name "$MARKETPLACE_NAME" --argjson src "$MARKETPLACE_SOURCE" \
      '.extraKnownMarketplaces[$name] = {"source":$src}' \
      "$SETTINGS" > "${SETTINGS}.tmp" && mv "${SETTINGS}.tmp" "$SETTINGS"
    echo "  Registered $MARKETPLACE_NAME in extraKnownMarketplaces"
  fi

  # Add to known_marketplaces.json
  if [ -f "$KNOWN_MARKETPLACES" ]; then
    jq --arg name "$MARKETPLACE_NAME" --argjson src "$MARKETPLACE_SOURCE" \
      --arg loc "$MARKETPLACE_DIR" \
      '.[$name] = {"source":$src,"installLocation":$loc}' \
      "$KNOWN_MARKETPLACES" > "${KNOWN_MARKETPLACES}.tmp" && mv "${KNOWN_MARKETPLACES}.tmp" "$KNOWN_MARKETPLACES"
    echo "  Registered $MARKETPLACE_NAME in known_marketplaces.json"
  fi

  # Enable both plugins in settings.json (v3: superpowers-for-vk is gone)
  if [ -f "$SETTINGS" ]; then
    for plugin_name in "${PLUGIN_NAMES[@]}"; do
      jq --arg id "$plugin_name@$MARKETPLACE_NAME" '.enabledPlugins[$id] = true' \
        "$SETTINGS" > "${SETTINGS}.tmp" && mv "${SETTINGS}.tmp" "$SETTINGS"
      echo "  Enabled $plugin_name@$MARKETPLACE_NAME in settings.json"
    done
  fi

  # Purge the retired bare-org marketplace. Two repos claimed `derio-net` and
  # rsync --delete'd each other out of it; the name is retired rather than
  # awarded to either, so no repo owns an org-level namespace. With no owner
  # left, EVERY `*@derio-net` registration is dangling by definition — including
  # blog-craft's, which its own installer re-registers under
  # `derio-net--blog-craft`. Removing the whole key is therefore safe by
  # construction, not us reaching into another repo's state.
  purged_ids=""
  for state_file in "$INSTALLED_PLUGINS" "$SETTINGS"; do
    [ -f "$state_file" ] || continue
    if [ "$state_file" = "$INSTALLED_PLUGINS" ]; then
      key_path='.plugins'
    else
      key_path='.enabledPlugins'
    fi
    while IFS= read -r plugin_id; do
      [ -n "$plugin_id" ] || continue
      case " $purged_ids " in *" $plugin_id "*) ;; *) purged_ids="$purged_ids $plugin_id" ;; esac
    done < <(jq -r "($key_path // {}) | keys[] | select(endswith(\"@$LEGACY_MARKETPLACE_NAME\"))" \
               "$state_file" 2>/dev/null || true)
    jq --arg suffix "@$LEGACY_MARKETPLACE_NAME" \
      "$key_path |= with_entries(select(.key | endswith(\$suffix) | not))" \
      "$state_file" > "${state_file}.tmp" && mv "${state_file}.tmp" "$state_file"
  done
  if [ -f "$KNOWN_MARKETPLACES" ]; then
    jq --arg name "$LEGACY_MARKETPLACE_NAME" 'del(.[$name])' \
      "$KNOWN_MARKETPLACES" > "${KNOWN_MARKETPLACES}.tmp" && mv "${KNOWN_MARKETPLACES}.tmp" "$KNOWN_MARKETPLACES"
  fi
  if [ -f "$SETTINGS" ]; then
    jq --arg name "$LEGACY_MARKETPLACE_NAME" 'del(.extraKnownMarketplaces[$name])' \
      "$SETTINGS" > "${SETTINGS}.tmp" && mv "${SETTINGS}.tmp" "$SETTINGS"
  fi
  if [ -n "$purged_ids" ] || [ -d "$LEGACY_MARKETPLACE_DIR" ] || [ -d "$LEGACY_CACHE_BASE" ]; then
    rm -rf "$LEGACY_MARKETPLACE_DIR" "$LEGACY_CACHE_BASE"
    echo "  Retired the '$LEGACY_MARKETPLACE_NAME' marketplace (registry, cache, directory)"
    for plugin_id in $purged_ids; do
      echo "    - dropped $plugin_id"
    done
    case " $purged_ids " in
      *"@$LEGACY_MARKETPLACE_NAME"*)
        if [ -n "$(echo "$purged_ids" | tr ' ' '\n' | grep -v "^super-fr@\|^super-fr-dispatch@\|^superpowers-for-vk@\|^$" || true)" ]; then
          echo "  NOTE: some of those belong to sibling repos. Re-run their installers" >&2
          echo "  to re-register them under their own 'derio-net--<repo>' marketplace." >&2
        fi
        ;;
    esac
  fi
else
  echo "  WARNING: jq not found — cannot register marketplace automatically" >&2
fi

# 3. Copy plugin into marketplace directory (decoupled from source repo).
# The cache is treated as ephemeral — it holds nothing worth preserving across
# runs, so we wipe any stale .git from older installs and clobber the rest.
echo ""
echo "Setting up marketplace directory..."
# The rsync below is `--delete`: it replaces the whole tree, it does not merge
# manifests. `derio-net--super-fr` names exactly one repo so nothing else should
# ever be here — but a name collision is silent and total, so check rather than
# assume. If a foreign manifest is squatting, we still reclaim (refusing would
# let a squat permanently break super-fr installs) and name whose plugins just
# went dark.
OCCUPANT_MANIFEST="$MARKETPLACE_DIR/.claude-plugin/marketplace.json"
if [ -f "$OCCUPANT_MANIFEST" ] && command -v jq &>/dev/null; then
  occupant_name="$(jq -r '.name // empty' "$OCCUPANT_MANIFEST" 2>/dev/null || true)"
  if [ -n "$occupant_name" ] && [ "$occupant_name" != "$MARKETPLACE_NAME" ]; then
    echo "  WARNING: foreign marketplace '$occupant_name' occupies $MARKETPLACE_DIR." >&2
    echo "  super-fr owns '$MARKETPLACE_NAME' and is reclaiming this directory;" >&2
    echo "  '$occupant_name' plugins installed from here will stop resolving." >&2
    echo "  Fix on that repo's side: a marketplace name is a 1:1 namespace over one" >&2
    echo "  source repo — use 'derio-net--<its own repo>', matching its own" >&2
    echo "  .claude-plugin/marketplace.json 'name'." >&2
  fi
fi
mkdir -p "$MARKETPLACE_DIR"
# Remove stale symlinks from older installs
if [ -L "$MARKETPLACE_DIR" ]; then
  rm "$MARKETPLACE_DIR"
  mkdir -p "$MARKETPLACE_DIR"
  echo "  Replaced stale symlink with standalone copy"
fi
# Drop any leftover .git so the cache cannot accumulate locally-modified state
# that would make a future operation refuse to update it.
if [ -e "$MARKETPLACE_DIR/.git" ]; then
  rm -rf "$MARKETPLACE_DIR/.git"
  echo "  Removed stale .git from cache (cache is ephemeral)"
fi
rsync -a --delete --exclude='.git' --exclude='__pycache__' --exclude='.venv' \
  "$PLUGIN_ROOT/" "$MARKETPLACE_DIR/"
echo "  Copied plugin into $MARKETPLACE_DIR"
# Shipped workflow manifests (plugins/super-fr/workflows/*.yaml, spec §4.A)
# ride this same wholesale rsync — no separate cp line, unlike rules/ or the
# OpenCode skill mirror below, both of which target a destination OUTSIDE
# this tree. fr.workflow.resolve.default_shipped_workflows_dir()'s fallback
# ($HOME/.claude/plugins/marketplaces/derio-net--super-fr/plugins/super-fr/
# workflows) is exactly $MARKETPLACE_DIR/plugins/super-fr/workflows —
# pinned by tests/integration/test_install_sh.py's TestInstallWorkflows so a
# future --exclude here can't silently unship a manifest.

# 4. Register each plugin in installed_plugins.json + sync per-plugin cache
echo ""
echo "Registering plugins..."
if command -v jq &>/dev/null && [ -f "$INSTALLED_PLUGINS" ]; then
  for plugin_name in "${PLUGIN_NAMES[@]}"; do
    plugin_src="$PLUGIN_ROOT/plugins/$plugin_name"
    CURRENT_VERSION=$(jq -r '.version' "$plugin_src/.claude-plugin/plugin.json" 2>/dev/null || echo "unknown")
    PLUGIN_CACHE="$CACHE_BASE/$plugin_name"
    CACHE_VERSION_DIR="$PLUGIN_CACHE/$CURRENT_VERSION"
    CACHE_CURRENT_LINK="$PLUGIN_CACHE/current"
    mkdir -p "$CACHE_VERSION_DIR"
    rsync -a --delete --exclude='__pycache__' \
      "$plugin_src/" "$CACHE_VERSION_DIR/"
    echo "  Synced $plugin_name v$CURRENT_VERSION to cache"

    # Point a stable `current` symlink at the freshly-synced version, AFTER the
    # sync completes (atomic-ish via -fn). installPath records this symlink, not
    # the version dir — so a running Claude Code session, which keeps installPath
    # literal and resolves it at exec time, picks up new hook/command code on the
    # next fire without a restart. Relative target keeps the link path-independent.
    ln -sfn "$CURRENT_VERSION" "$CACHE_CURRENT_LINK"
    echo "  Pointed $plugin_name/current -> $CURRENT_VERSION"

    INSTALL_ENTRY='[{"scope":"user","installPath":"'"$CACHE_CURRENT_LINK"'","version":"'"$CURRENT_VERSION"'","installedAt":"'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'","lastUpdated":"'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'"}]'
    jq --argjson entry "$INSTALL_ENTRY" --arg id "$plugin_name@$MARKETPLACE_NAME" \
      '.plugins[$id] = $entry' \
      "$INSTALLED_PLUGINS" > "${INSTALLED_PLUGINS}.tmp" && mv "${INSTALLED_PLUGINS}.tmp" "$INSTALLED_PLUGINS"
    echo "  Registered $plugin_name@$MARKETPLACE_NAME v$CURRENT_VERSION in installed_plugins.json"

    # Prune to current + the most-recent previous version dir (N-1 buffer): a
    # session that somehow cached a realpath keeps working until restart. Never
    # touch the `current` symlink — the `*/` glob matches it, so skip symlinks.
    PREV_KEEP=""
    while IFS= read -r prev_dir; do
      [ -n "$prev_dir" ] || continue
      PREV_KEEP="$(basename "$prev_dir")"
      break
    done < <(ls -dt "$PLUGIN_CACHE"/*/ 2>/dev/null | while IFS= read -r p; do
               q="${p%/}"
               [ -L "$q" ] && continue
               [ "$(basename "$q")" = "$CURRENT_VERSION" ] && continue
               echo "$q"
             done)

    for version_dir in "$PLUGIN_CACHE"/*/; do
      vd="${version_dir%/}"
      [ -L "$vd" ] && continue   # leave the `current` symlink alone
      version_name="$(basename "$vd")"
      if [ "$version_name" = "$CURRENT_VERSION" ]; then
        echo "  keeping cache: $plugin_name/$version_name (current)"
      elif [ "$version_name" = "$PREV_KEEP" ]; then
        echo "  keeping cache: $plugin_name/$version_name (previous)"
      else
        rm -rf "$vd"
        echo "  cleared stale cache: $plugin_name/$version_name"
      fi
    done
  done
  # (The retired superpowers-for-vk@derio-net entry needs no special case any
  # more — the bare-org purge above drops every `*@derio-net` id wholesale.)

  # Report — never delete — `X@derio-net--super-fr` registrations our own
  # manifest doesn't list. Ours is the only repo that can legitimately write
  # this namespace, so an unknown id here means a stale plugin name from an
  # older super-fr, or a genuine collision. Name it rather than guess: silently
  # deleting a registration is how the original bug hid for two months.
  OWNED_IDS=""
  for plugin_name in "${PLUGIN_NAMES[@]}"; do
    OWNED_IDS="$OWNED_IDS $plugin_name@$MARKETPLACE_NAME"
  done
  orphans=""
  for source_file in "$INSTALLED_PLUGINS" "$SETTINGS"; do
    [ -f "$source_file" ] || continue
    if [ "$source_file" = "$INSTALLED_PLUGINS" ]; then
      jq_path='.plugins // {}'
    else
      jq_path='.enabledPlugins // {}'
    fi
    while IFS= read -r plugin_id; do
      [ -n "$plugin_id" ] || continue
      case " $OWNED_IDS " in *" $plugin_id "*) continue ;; esac
      case " $orphans " in *" $plugin_id "*) continue ;; esac
      orphans="$orphans $plugin_id"
    done < <(jq -r "$jq_path | keys[] | select(endswith(\"@$MARKETPLACE_NAME\"))" \
               "$source_file" 2>/dev/null || true)
  done
  if [ -n "$orphans" ]; then
    echo "" >&2
    echo "  WARNING: orphaned plugin registration(s) in the $MARKETPLACE_NAME marketplace:" >&2
    for plugin_id in $orphans; do
      echo "    - $plugin_id  (not listed in super-fr's marketplace.json)" >&2
    done
    echo "  These stay enabled but can no longer resolve. Left in place rather than" >&2
    echo "  silently deleted. Remove them with:" >&2
    echo "    jq 'del(.plugins[\"<id>\"])' ~/.claude/plugins/installed_plugins.json" >&2
    echo "    jq 'del(.enabledPlugins[\"<id>\"])' ~/.claude/settings.json" >&2
  fi
else
  echo "  WARNING: cannot register plugins — jq or installed_plugins.json missing" >&2
fi

# 6. Clean stale user-level skill copies (from older installs)
for skill in "${SKILL_NAMES[@]}"; do
  if [ -d "$CLAUDE_DIR/skills/$skill" ] || [ -L "$CLAUDE_DIR/skills/$skill" ]; then
    rm -rf "$CLAUDE_DIR/skills/$skill"
    echo "  Removed stale $CLAUDE_DIR/skills/$skill (now delivered by plugin)"
  fi
done

# 7. Rules
echo ""
echo "Installing rules..."
mkdir -p "$RULES_DIR"
rm -f "$RULES_DIR/vk-plan-override.md" "$RULES_DIR/fr-plan-override.md"
cp "$PLUGIN_ROOT/plugins/super-fr/rules/fr-plan-override.md" "$RULES_DIR/fr-plan-override.md"
echo "  Installed $RULES_DIR/fr-plan-override.md (retired vk-plan-override.md)"
cp "$PLUGIN_ROOT/plugins/super-fr/rules/fr-isolation-required.md" "$RULES_DIR/fr-isolation-required.md"
echo "  Installed $RULES_DIR/fr-isolation-required.md (#328 isolation Edit/Write guard)"
cp "$PLUGIN_ROOT/plugins/super-fr/rules/no-claude-p-batch.md" "$RULES_DIR/no-claude-p-batch.md"
echo "  Installed $RULES_DIR/no-claude-p-batch.md (#328 batch-LLM convention)"
cp "$PLUGIN_ROOT/plugins/super-fr/rules/fr-worktree-override.md" "$RULES_DIR/fr-worktree-override.md"
echo "  Installed $RULES_DIR/fr-worktree-override.md (worktree-skill routing)"

# 7a. Allowlist the fr-phase-executor subagent in the org agent-worktree hook.
# fr-goal dispatches each plan phase to this narrow, serial, already-isolated
# subagent (2026-07-22 fr-goal-subagent-execution spec §B.1). Idempotent; a
# no-op when the org hook is absent (fr-goal then falls back to inline).
# The script already exits 0 when the hook is simply absent, so a non-zero exit
# here is a REAL failure (anchor drift / unwritable hook) — surface it as a
# warning instead of the misleading "not managed here", which used to mask it
# and leave every fr-goal run mysteriously degraded to inline execution.
if ! bash "$PLUGIN_ROOT/scripts/ensure-phase-executor-allowlist.sh" \
     "$CLAUDE_DIR/hooks/agent-worktree-required.sh"; then
  echo "  WARNING: could not allowlist fr-phase-executor in the agent-worktree hook" >&2
  echo "  (see the error above) — fr-goal will fall back to INLINE phase execution." >&2
fi

# 7b. OpenCode skill + command delivery — opt-in only (OpenCode has no
# plugin/marketplace concept; it discovers plain SKILL.md files and
# commands/<name>.md files from its own global dirs).
# Gate on an explicit opt-in or evidence the operator already uses OpenCode,
# so installs on machines without it stay untouched.
if [ "${OPENCODE_SKILLS_INSTALL:-}" = "1" ] || [ -d "$HOME/.config/opencode" ]; then
  echo ""
  echo "Installing skills for OpenCode ($OPENCODE_SKILLS_DIR)..."
  mkdir -p "$OPENCODE_SKILLS_DIR"
  for skill_dir in "$PLUGIN_ROOT"/plugins/super-fr/skills/*/; do
    skill="$(basename "$skill_dir")"
    mkdir -p "$OPENCODE_SKILLS_DIR/$skill"
    cp "$skill_dir/SKILL.md" "$OPENCODE_SKILLS_DIR/$skill/SKILL.md"
    echo "  Installed $OPENCODE_SKILLS_DIR/$skill/SKILL.md"
  done
  echo ""
  echo "Installing OpenCode slash commands ($OPENCODE_COMMANDS_DIR)..."
  mkdir -p "$OPENCODE_COMMANDS_DIR"
  for skill_dir in "$PLUGIN_ROOT"/plugins/super-fr/skills/*/; do
    skill="$(basename "$skill_dir")"
    # Copies from the repo's own already-synced, CI-guarded .opencode/commands/
    # mirror (scripts/sync-opencode.py) rather than regenerating — install.sh
    # stays bash+jq only, no Python/yaml dependency added here.
    cp "$PLUGIN_ROOT/.opencode/commands/$skill.md" "$OPENCODE_COMMANDS_DIR/$skill.md"
    echo "  Installed $OPENCODE_COMMANDS_DIR/$skill.md"
  done
else
  echo ""
  echo "Skipping OpenCode skill/command delivery (no ~/.config/opencode found; set"
  echo "OPENCODE_SKILLS_INSTALL=1 to force)."
fi

# 8. VK MCP server at user level
if [ "$SKIP_MCP" = true ]; then
  echo ""
  echo "Skipping MCP configuration (binary not found)."
else
  echo ""
  echo "Configuring MCP..."
  VK_MCP_ENTRY='{"command":"'"$VK_MCP_BINARY"'","args":["--mode","global"],"env":{"VIBE_BACKEND_URL":"http://localhost:8081"}}'
  if [ -f "$MCP_CONFIG" ] && command -v jq &>/dev/null; then
    jq --argjson entry "$VK_MCP_ENTRY" '.mcpServers.vibe_kanban = $entry' "$MCP_CONFIG" > "${MCP_CONFIG}.tmp" && mv "${MCP_CONFIG}.tmp" "$MCP_CONFIG"
    echo "  Updated vibe_kanban in $MCP_CONFIG"
  elif command -v jq &>/dev/null; then
    echo '{"mcpServers":{}}' | jq --argjson entry "$VK_MCP_ENTRY" '.mcpServers.vibe_kanban = $entry' > "$MCP_CONFIG"
    echo "  Created $MCP_CONFIG with vibe_kanban"
  else
    echo "  WARNING: jq not found — cannot configure MCP server automatically" >&2
    echo "  Add vibe_kanban manually to $MCP_CONFIG" >&2
  fi
fi

# 9. PostToolUse hook hint
if [ ! -f "$SETTINGS" ]; then
  echo "  WARNING: $SETTINGS not found — skipping hook check"
else
  if grep -q "validate-plans" "$SETTINGS"; then
    echo "  PostToolUse hook already present — skipping"
  else
    echo ""
    echo "  NOTE: Manual settings.json edit required. Add this PostToolUse hook:"
    cat << 'HOOK'
    {
      "matcher": "Edit|Write",
      "hooks": [
        {
          "type": "command",
          "command": "bash -c 'FILE=$(cat | jq -r \".tool_input.file_path // .tool_response.filePath // empty\"); case \"$FILE\" in */docs/superpowers/plans/*.md) REPO_ROOT=$(git -C \"$(dirname \"$FILE\")\" rev-parse --show-toplevel 2>/dev/null); [ -x \"$REPO_ROOT/scripts/validate-plans.sh\" ] && \"$REPO_ROOT/scripts/validate-plans.sh\" \"$FILE\" 2>&1 || true;; esac'",
          "statusMessage": "Validating plan..."
        }
      ]
    }
HOOK
  fi
fi

# 10. fr CLI
if command -v uv &>/dev/null; then
  echo ""
  echo "Installing fr CLI globally (workspace member fr + the VK adapter)..."
  # `uv tool install --force` removes the tool env in place; on macOS that
  # rmdir intermittently fails with "Directory not empty" (ENOTEMPTY), and a
  # freshly built env can fail a one-shot `fr --version` before it quiesces.
  # Both self-heal on a retry (the operator hit fail→fail→succeed). Retry
  # rather than turn a momentary hiccup into a hard install abort; on a stuck
  # tool dir, an explicit uninstall clears the ENOTEMPTY before the next try.
  # See docs/superpowers/debugging/2026-07-05-install-uv-tool-flaky.md.
  fr_install_retry_sleep="${FR_INSTALL_RETRY_SLEEP:-2}"
  fr_installed=""
  for attempt in 1 2 3; do
    # Pipeline lives in the `if` condition so a `uv` failure (propagated by
    # `pipefail` through `sed`) is caught here instead of tripping `set -e`.
    if uv tool install --force \
      --with "$PLUGIN_ROOT/packages/fr-vk" \
      "$PLUGIN_ROOT/packages/fr" 2>&1 | sed 's/^/  /'; then
      fr_installed=1
      break
    fi
    if [ "$attempt" -lt 3 ]; then
      echo "  uv tool install attempt $attempt failed; clearing tool env and retrying..." >&2
      uv tool uninstall fr >/dev/null 2>&1 || true
      rm -rf "$(uv tool dir 2>/dev/null)/fr" 2>/dev/null || true
      sleep "$fr_install_retry_sleep"
    fi
  done
  if [ -z "$fr_installed" ]; then
    echo "  ERROR: uv tool install failed after 3 attempts" >&2
    exit 1
  fi
  # Smoke check — a tool env without a working entry point must fail loud,
  # but give a just-installed env a couple of beats to quiesce first.
  fr_bin="$(uv tool dir 2>/dev/null)/fr/bin/fr"
  if [ -x "$fr_bin" ]; then
    fr_runs=""
    for _ in 1 2 3; do
      if "$fr_bin" --version >/dev/null 2>&1; then
        fr_runs=1
        break
      fi
      sleep "$fr_install_retry_sleep"
    done
    if [ -z "$fr_runs" ]; then
      echo "  ERROR: fr CLI installed but does not run" >&2
      exit 1
    fi
  else
    echo "  WARNING: fr entry point not found at $fr_bin (uv stub or unusual layout?)" >&2
  fi
else
  echo ""
  echo "  WARNING: uv not found — install fr CLI manually:"
  echo "    uv tool install $PLUGIN_ROOT/packages/fr"
fi

# 10b. Hermes Agent delivery — opt-in only (Hermes discovers skills from
# ~/.hermes/skills/ and loads its own config.yaml + SOUL.md). Gated like
# OpenCode. The invasive, reversible mutations (config.yaml hooks merge,
# shell-hooks allowlist, SOUL.md managed block, hook-tree copy) are delegated to
# the tested `fr hermes install` subcommand, so install.sh stays bash+jq only.
# Runs AFTER the fr CLI install above so `fr` is on PATH.
if [ "${HERMES_SKILLS_INSTALL:-}" = "1" ] || [ -d "$HERMES_HOME" ]; then
  echo ""
  echo "Installing skills for Hermes Agent ($HERMES_HOME/skills/fr)..."
  mkdir -p "$HERMES_HOME/skills/fr"
  for skill_dir in "$PLUGIN_ROOT"/.hermes/skills/fr/*/; do
    skill="$(basename "$skill_dir")"
    mkdir -p "$HERMES_HOME/skills/fr/$skill"
    cp "$skill_dir/SKILL.md" "$HERMES_HOME/skills/fr/$skill/SKILL.md"
    echo "  Installed $HERMES_HOME/skills/fr/$skill/SKILL.md"
  done
  if command -v fr &>/dev/null; then
    echo "Wiring Hermes hooks + rules (fr hermes install)..."
    if fr hermes install --source "$PLUGIN_ROOT" --home "$HERMES_HOME"; then
      echo "  Wired Hermes hooks + SOUL.md rules block"
    else
      echo "  WARNING: fr hermes install failed — hooks/rules not wired" >&2
    fi
  else
    echo "  WARNING: fr not on PATH — skipping fr hermes install (hooks/rules not wired)" >&2
  fi
else
  echo ""
  echo "Skipping Hermes Agent delivery (no ~/.hermes found; set"
  echo "HERMES_SKILLS_INSTALL=1 to force)."
fi

# 11. devcontainer CLI (fr-isolation dependency)
# `fr isolation up` shells out to `devcontainer` unconditionally; without it,
# the failure mode is a bare "command not found" deep inside isolation code,
# not a clear preflight message. Best-effort only (not a hard preflight
# requirement above): plenty of installs never touch fr isolation, and
# forcing an npm-global install on every operator would be too heavy-handed.
if command -v devcontainer &>/dev/null; then
  echo ""
  echo "  OK: devcontainer CLI already installed ($(devcontainer --version 2>/dev/null || echo present))"
elif command -v npm &>/dev/null; then
  echo ""
  echo "Installing devcontainer CLI (npm -g @devcontainers/cli, needed by fr isolation up)..."
  if npm install -g @devcontainers/cli >/dev/null 2>&1; then
    echo "  Installed devcontainer CLI"
  else
    echo "  WARNING: npm install -g @devcontainers/cli failed — install manually if you plan to use fr isolation" >&2
  fi
else
  echo ""
  echo "  WARNING: devcontainer CLI not found and npm not available — 'fr isolation up' will fail until"
  echo "  you install it manually: npm install -g @devcontainers/cli"
fi

echo ""
echo "Installation complete. Restart Claude Code to pick up plugin changes."
echo ""
echo "Verify with:"
echo "  jq '.mcpServers.vibe_kanban' ~/.claude/.mcp.json"
echo "  cat ~/.claude/rules/fr-plan-override.md"
echo "  fr --version"
