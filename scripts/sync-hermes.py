#!/usr/bin/env python3
"""Sync super-fr skills (and rules) into Hermes-Agent-discoverable mirrors.

Hermes Agent (github.com/NousResearch/hermes-agent) discovers skills as plain
`SKILL.md` files under `~/.hermes/skills/<category>/<name>/` — it has no concept
of the Claude Code plugin/marketplace layout this repo ships skills through
(`plugins/super-fr/skills/<name>/SKILL.md`). super-fr's SKILL.md format (with
`name` + `description` frontmatter, which Hermes requires) loads unchanged, so
the skills mirror is a byte-for-byte copy under a single `fr` category
directory. Hermes has no `instructions` array like OpenCode; its only always-on
global surface is `~/.hermes/SOUL.md`, so super-fr's shipped rules are assembled
into a delimited managed block (see the rules section).

`plugins/super-fr/skills/` and `plugins/super-fr/rules/` stay the canonical
sources — never hand-edit `.hermes/skills/fr/<name>/SKILL.md` or
`.hermes/SOUL.d/super-fr-rules.md` directly; they are overwritten on sync.

Run via `uv run scripts/sync-hermes.py` — this module lives alongside
`scripts/sync-opencode.py` and follows the same idioms.

Usage:
    uv run scripts/sync-hermes.py          # write/update the mirrors
    uv run scripts/sync-hermes.py --check  # exit non-zero on drift, no writes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SKILLS_CANONICAL_DIR = REPO_ROOT / "plugins" / "super-fr" / "skills"
# Every super-fr skill lives under a single `fr` category in ~/.hermes/skills/,
# so uninstall can target `skills/fr/` wholesale and skills never collide with
# the user's own or agent-generated skills.
SKILLS_MIRROR_DIR = REPO_ROOT / ".hermes" / "skills" / "fr"

RULES_CANONICAL_DIR = REPO_ROOT / "plugins" / "super-fr" / "rules"
# ONLY the installer-shipped plugin rules reach a consumer's global SOUL.md —
# exactly the set install.sh copies to ~/.claude/rules/ for Claude Code. The
# repo-local acceptance-matrix rule (no plugin equivalent) is maintainer-only
# and must never ship into a consumer's SOUL.md.
SHIPPED_RULE_NAMES = (
    "fr-isolation-required",
    "fr-plan-override",
    "fr-worktree-override",
    "no-claude-p-batch",
)
SOUL_D_MIRROR = REPO_ROOT / ".hermes" / "SOUL.d" / "super-fr-rules.md"
SOUL_BLOCK_START = "<!-- super-fr:rules START -->"
SOUL_BLOCK_END = "<!-- super-fr:rules END -->"


# ---------------------------------------------------------------------------
# skills


def canonical_skills() -> dict[str, Path]:
    """Map of skill name -> canonical SKILL.md path."""
    return {p.parent.name: p for p in sorted(SKILLS_CANONICAL_DIR.glob("*/SKILL.md"))}


def mirror_skills() -> dict[str, Path]:
    """Map of skill name -> existing mirror SKILL.md path (if any)."""
    if not SKILLS_MIRROR_DIR.is_dir():
        return {}
    return {p.parent.name: p for p in sorted(SKILLS_MIRROR_DIR.glob("*/SKILL.md"))}


def find_skills_drift() -> list[str]:
    """Human-readable skill mirror drift descriptions; empty means in sync."""
    canonical = canonical_skills()
    mirror = mirror_skills()
    problems = []

    missing = sorted(set(canonical) - set(mirror))
    extra = sorted(set(mirror) - set(canonical))
    for name in missing:
        problems.append(f"{name}: missing from .hermes/skills/fr/")
    for name in extra:
        problems.append(f"{name}: present in .hermes/skills/fr/ with no canonical source")

    for name in sorted(set(canonical) & set(mirror)):
        if canonical[name].read_text() != mirror[name].read_text():
            problems.append(f"{name}: .hermes/skills/fr/ content differs from canonical")

    return problems


def sync_skills() -> None:
    """Write/overwrite the skills mirror to match canonical exactly."""
    canonical = canonical_skills()

    for name, path in mirror_skills().items():
        if name not in canonical:
            skill_dir = path.parent
            for child in skill_dir.iterdir():
                child.unlink()
            skill_dir.rmdir()

    for name, src in canonical.items():
        dest_dir = SKILLS_MIRROR_DIR / name
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "SKILL.md"
        dest.write_text(src.read_text())
        # Sibling breadcrumb pointing back at the canonical source — purely
        # informational, never parsed by Hermes (it only reads SKILL.md).
        source_note = dest_dir / ".source"
        source_note.write_text(
            f"Generated from {src.relative_to(REPO_ROOT)} by "
            f"scripts/sync-hermes.py. Do not edit SKILL.md here directly.\n"
        )


# ---------------------------------------------------------------------------
# rules -> managed SOUL.md block


def canonical_rules() -> dict[str, Path]:
    """Map of shipped rule name -> canonical rule markdown path.

    Restricted to SHIPPED_RULE_NAMES so a new maintainer-only rule dropped into
    plugins/super-fr/rules/ never silently leaks into a consumer's SOUL.md.
    """
    result: dict[str, Path] = {}
    for name in SHIPPED_RULE_NAMES:
        path = RULES_CANONICAL_DIR / f"{name}.md"
        if path.is_file():
            result[name] = path
    return result


def render_rules_block() -> str:
    """The delimited managed block super-fr owns inside ~/.hermes/SOUL.md."""
    parts = [SOUL_BLOCK_START, ""]
    for _name, path in canonical_rules().items():
        parts.append(path.read_text().rstrip())
        parts.append("")
    parts.append(SOUL_BLOCK_END)
    return "\n".join(parts) + "\n"


def find_rules_drift() -> list[str]:
    """Human-readable rules-block drift descriptions; empty means in sync."""
    expected = render_rules_block()
    if not SOUL_D_MIRROR.is_file():
        return [f"{SOUL_D_MIRROR.relative_to(REPO_ROOT)}: missing"]
    if SOUL_D_MIRROR.read_text() != expected:
        return [f"{SOUL_D_MIRROR.relative_to(REPO_ROOT)}: content differs from canonical rules"]
    return []


def sync_rules() -> None:
    """Write/overwrite the SOUL.d managed-block mirror to match canonical."""
    SOUL_D_MIRROR.parent.mkdir(parents=True, exist_ok=True)
    SOUL_D_MIRROR.write_text(render_rules_block())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any mirror is out of sync; make no writes.",
    )
    args = parser.parse_args()

    if args.check:
        drift = find_skills_drift() + find_rules_drift()
        if drift:
            print("scripts/sync-hermes.py --check: drift detected:", file=sys.stderr)
            for line in drift:
                print(f"  - {line}", file=sys.stderr)
            print("Run `scripts/sync-hermes.py` (no --check) to fix.", file=sys.stderr)
            return 1
        print("scripts/sync-hermes.py --check: .hermes/ mirrors are in sync.")
        return 0

    sync_skills()
    sync_rules()
    print(
        f"Synced {len(canonical_skills())} skill(s) into "
        f"{SKILLS_MIRROR_DIR.relative_to(REPO_ROOT)}/ and "
        f"{len(canonical_rules())} rule(s) into "
        f"{SOUL_D_MIRROR.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
