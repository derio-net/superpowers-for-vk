"""CI tripwire: .hermes/SOUL.d/super-fr-rules.md must mirror the shipped rules.

Hermes has no OpenCode-style `instructions` array; its only always-on global
surface is `~/.hermes/SOUL.md`. `scripts/sync-hermes.py` assembles the four
*shipped* plugin rules (fr-isolation-required, fr-plan-override,
fr-worktree-override, no-claude-p-batch) into a delimited managed block written to
`.hermes/SOUL.d/super-fr-rules.md`; `fr hermes install` later applies that block
to the user's `~/.hermes/SOUL.md`. The repo-local `acceptance-matrix` rule is a
super-fr-repo-maintainer rule (no plugin equivalent) and is deliberately NOT
shipped to consumers — it must never appear in this consumer block.

Like the skills tripwire, this imports the generator's own drift function so the
CI gate and `--check` CLI can never disagree.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "sync_hermes", REPO_ROOT / "scripts" / "sync-hermes.py"
)
assert _spec is not None and _spec.loader is not None
sync_hermes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_hermes)

SHIPPED_RULES = {
    "fr-isolation-required",
    "fr-plan-override",
    "fr-worktree-override",
    "no-claude-p-batch",
}


def test_canonical_rules_are_exactly_the_shipped_rules() -> None:
    names = set(sync_hermes.canonical_rules())
    assert names == SHIPPED_RULES, (
        f"expected exactly {sorted(SHIPPED_RULES)}, got {sorted(names)} — "
        "acceptance-matrix (repo-local-only) must NOT ship to consumers."
    )


def test_rules_block_has_no_drift() -> None:
    drift = sync_hermes.find_rules_drift()
    assert not drift, "\n".join(drift) + (
        "\n\nRun `scripts/sync-hermes.py` (no --check) to fix, then commit .hermes/SOUL.d/."
    )


def test_block_is_delimited_by_managed_markers() -> None:
    block = (REPO_ROOT / ".hermes" / "SOUL.d" / "super-fr-rules.md").read_text()
    assert block.startswith(sync_hermes.SOUL_BLOCK_START), (
        f"block must start with {sync_hermes.SOUL_BLOCK_START!r}"
    )
    assert block.rstrip().endswith(sync_hermes.SOUL_BLOCK_END), (
        f"block must end with {sync_hermes.SOUL_BLOCK_END!r}"
    )
