"""CI tripwire: no worktree directory may ever be tracked, and the routing rule ships.

Worktree traceability (spec 2026-09-04 §5.D) routes `superpowers:using-git-worktrees`
to `fr-isolation` in fr-enabled repos, so `<repo>/.worktrees/` must never appear;
Claude's native `<repo>/.claude/worktrees/` is a runtime artifact. Both are
gitignored, but a hand-`git add` or a copied checkout could still stage them —
a tracked worktree leaks a whole second checkout into a PR. This guard fails
loud. It also pins that the rule carrying the routing claim exists, so the
acceptance row `superpowers-worktree-skill-routed-to-fr` is backed by a test.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RULE = REPO_ROOT / "plugins" / "super-fr" / "rules" / "fr-worktree-override.md"
FORBIDDEN = (".worktrees", ".claude/worktrees")


def tracked_under(names: Iterable[str], component: str) -> list[str]:
    """Tracked paths that have `component` as a path segment (or segment run).

    `component` may itself contain `/` (`.claude/worktrees`), so match on the
    slash-delimited form rather than on individual parts.
    """
    needle = f"/{component}/"
    return [n for n in names if needle in f"/{n}/"]


def test_tracked_under_detects_violation() -> None:
    names = [".worktrees/blog/README.md", "a.py", "sub/.claude/worktrees/agent-1/x"]
    assert tracked_under(names, ".worktrees") == [".worktrees/blog/README.md"]
    assert tracked_under(names, ".claude/worktrees") == ["sub/.claude/worktrees/agent-1/x"]


def test_tracked_under_clean() -> None:
    names = ["a.py", "docs/worktrees.md", ".claude/rules/x.md", "worktrees/.claude/y"]
    assert tracked_under(names, ".worktrees") == []
    assert tracked_under(names, ".claude/worktrees") == []


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_worktree_dirs_never_tracked() -> None:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
    )
    names = out.stdout.splitlines()
    for component in FORBIDDEN:
        offenders = tracked_under(names, component)
        assert offenders == [], f"{component} is tracked: {offenders}"


def test_gitignore_lists_both_worktree_dirs() -> None:
    lines = {line.strip() for line in (REPO_ROOT / ".gitignore").read_text().splitlines()}
    for component in FORBIDDEN:
        assert f"{component}/" in lines, f".gitignore must contain '{component}/'"


def test_worktree_override_rule_carries_the_routing_claim() -> None:
    """The acceptance row is `rule + tripwire`: the rule text IS the routing."""
    assert RULE.is_file(), f"missing shipped rule {RULE.relative_to(REPO_ROOT)}"
    text = RULE.read_text(encoding="utf-8")
    assert "superpowers:using-git-worktrees" in text
    assert "fr isolation up --branch" in text
    assert "Never create" in text and ".worktrees/" in text
