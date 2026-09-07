"""fr skills — overview of the v2 CLI surface and the skill files that document it.

Two sections:
  - **Commands** — every top-level command and sub-app, introspected from the
    typer app at runtime (so it stays in sync with the actual surface).
  - **Skills** — the `fr-*` SKILL.md files. v2 skills are not 1:1 with
    sub-apps (e.g. `fr-execute` orchestrates `fr pickup` + `fr plan edit` +
    `fr apply`), so the skill section is free-form prose pointing at the
    relevant commands rather than a single-app mapping.
"""

from __future__ import annotations

import typer

# Free-form skill summaries — what each `fr-*` skill is for and which CLI
# verbs it orchestrates. Update alongside the SKILL.md files.
SKILLS: list[tuple[str, str, str]] = [
    (
        "fr-plan",
        "Author / edit plans (skill).",
        "fr plan {create,edit,rework,rework-add,rework-list,self-review}",
    ),
    (
        "fr-dispatch",
        "Reconcile a plan's GitHub Issues (skill).",
        "fr status  →  fr apply [--yes] [--force]  ·  fr undispatch / fr archive to invert/finish",
    ),
    (
        "fr-execute",
        "Implement a phase end-to-end (skill).",
        "fr pickup --phase N  →  fr plan edit --tick / --complete-phase  →  fr apply --yes",
    ),
    (
        "fr-acceptance",
        "Acceptance matrix: backfill, status flips, honest coverage (skill).",
        "fr acceptance {check,report,status,add,init,backfill,digest}",
    ),
    (
        "fr-progress",
        "Plan / spec progress reporting (skill).",
        "fr status <plan-dir>  +  fr spec status [--all]  +  fr plan edit  +  fr archive [--all]"
        "  ·  fr repair [--yes] to normalize stale refs",
    ),
    (
        "fr-goal",
        "Autonomous goal-to-PR pipeline, driven by a workflow shape (skill).",
        "fr run {start,advance,resolve,adopt,status,check}  ·  fr workflow check"
        "  ·  fr plan {create,self-review,edit}",
    ),
    (
        "fr-isolation",
        "Isolated workspace: worktree + devcontainer, exec-bridge (skill).",
        "fr isolation {up,exec,status,attach,detach,down,gc}",
    ),
    (
        "fr-runner",
        "Operate/debug an autonomous runner (dispatch plugin skill).",
        "heartbeat + failure metrics  ·  fr status  ·  fr undispatch",
    ),
    (
        "fr-init",
        "Scaffold devcontainer profiles via interview (skill).",
        "fr init scaffold --profile NAME --purpose TEXT [--tool ...] [--secret ...]",
    ),
    (
        "fr-brainstorming",
        "Brainstorm inside fr-isolation; hard stop without a profile (skill).",
        "fr isolation up  →  superpowers:brainstorming  →  fr-plan handoff",
    ),
    (
        "fr-debugging",
        "Debug a bug inside fr-isolation; reuse-or-fresh workspace (skill).",
        "fr isolation {status,up,exec,down}  →  superpowers:systematic-debugging  →  fix-PR",
    ),
]


def _commands(app: typer.Typer) -> list[tuple[str, str]]:
    """Introspect a typer app: return (name, first-line-help) for every command/sub-app.

    Duck-typed on purpose: typer ≥0.26 vendors click (`typer._click`), so the
    compiled group is NOT an instance of the externally-installed click's
    Group — isinstance checks (and bare `import click`) break across typer
    versions. `.commands` exists on both lineages.
    """
    click_group = typer.main.get_command(app)
    commands = getattr(click_group, "commands", {})
    rows: list[tuple[str, str]] = []
    for name, cmd in commands.items():
        help_text = (cmd.help or "").strip().splitlines()[0] if cmd.help else ""
        rows.append((name, help_text))
    return rows


def skills() -> None:
    """Show the v2 CLI surface + skill summaries."""
    # Late import — avoids cycle: cli.py imports skills_cmd, and we need cli.app here.
    from fr.cli import app

    typer.echo("Commands:")
    rows = _commands(app)
    width = max((len(n) for n, _ in rows), default=0)
    for name, help_text in rows:
        typer.echo(f"  fr {name:<{width}}  {help_text}")
    typer.echo()
    typer.echo("Skills (full docs in plugins/*/skills/<name>/SKILL.md):")
    skill_width = max((len(n) for n, _, _ in SKILLS), default=0)
    for name, summary, verbs in SKILLS:
        typer.echo(f"  {name:<{skill_width}}  {summary}")
        typer.echo(f"  {' ' * skill_width}    →  {verbs}")
    typer.echo()
