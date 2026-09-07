"""Plugin hook registration: hooks.json shape and shipped scripts."""

from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "plugins" / "super-fr" / "hooks"


class TestPluginHooks:
    def test_hooks_json_parses(self) -> None:
        data = json.loads((HOOKS_DIR / "hooks.json").read_text())
        events = data["hooks"]
        # Skill → pipeline sentinel; Bash → session bind (spec 2026-09-04 §5.B.1).
        assert {m["matcher"] for m in events["PostToolUse"]} == {"Skill", "Bash"}
        # SessionEnd → session unbind (§5.B.2); no matcher, fires for every reason.
        assert "SessionEnd" in events
        assert [h["command"] for m in events["SessionEnd"] for h in m["hooks"]] == [
            "${CLAUDE_PLUGIN_ROOT}/hooks/fr-session-unbind.sh"
        ]
        # WorktreeCreate / WorktreeRemove → native Claude worktree sessions land
        # in fr (§5.B.3 / §5.B.4); no matcher, every worktree name goes through.
        assert [h["command"] for m in events["WorktreeCreate"] for h in m["hooks"]] == [
            "${CLAUDE_PLUGIN_ROOT}/hooks/fr-worktree-create.sh"
        ]
        assert [h["command"] for m in events["WorktreeRemove"] for h in m["hooks"]] == [
            "${CLAUDE_PLUGIN_ROOT}/hooks/fr-worktree-remove.sh"
        ]
        assert {m["matcher"] for m in events["PreToolUse"]} == {
            "Bash",
            "Edit|Write|MultiEdit|NotebookEdit",
            # super-fr#420: refuses the poisoned phase-executor dispatch. Both
            # spellings of the subagent tool — `Agent` today, `Task` on older builds.
            "Agent|Task",
        }

    def test_registered_scripts_exist_and_are_executable(self) -> None:
        data = json.loads((HOOKS_DIR / "hooks.json").read_text())
        commands = [
            h["command"]
            for matchers in data["hooks"].values()
            for m in matchers
            for h in m["hooks"]
        ]
        assert commands, "no hook commands registered"
        for command in commands:
            assert command.startswith("${CLAUDE_PLUGIN_ROOT}/"), command
            rel = command.replace("${CLAUDE_PLUGIN_ROOT}/", "")
            script = REPO_ROOT / "plugins" / "super-fr" / rel
            assert script.is_file(), f"missing {script}"
            assert os.access(script, os.X_OK), f"not executable: {script}"
