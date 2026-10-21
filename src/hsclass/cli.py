from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from hsclass.config import get_settings
from hsclass.schedule.models import build_from_export, load_export
from hsclass.schedule.validate import validate_tree

app = typer.Typer(help="HS tariff classification", no_args_is_help=True)
schedule_app = typer.Typer(help="Parse and validate the tariff schedule", no_args_is_help=True)
app.add_typer(schedule_app, name="schedule")
console = Console()


def _tree():
    settings = get_settings()
    export = settings.raw_dir / "hts-export.json"
    if not export.exists():
        raise typer.BadParameter(f"missing {export}; download the release first")
    return settings, build_from_export(load_export(export), settings.require_revision())


@app.command("config")
def show_config() -> None:
    """Print the active configuration and the pinned schedule version."""
    s = get_settings()
    table = Table(show_header=False)
    table.add_row("hts_revision", s.hts_revision)
    table.add_row("llm_model", s.llm_model)
    table.add_row("embedding", f"{s.embedding_backend} / {s.embedding_model}")
    table.add_row("cost ceiling", f"${s.cost_ceiling_usd_per_request:.4f} per request")
    table.add_row("anthropic key", "set" if s.anthropic_api_key else "unset")
    console.print(table)


@schedule_app.command("build")
def schedule_build() -> None:
    """Build the nomenclature tree from the published export."""
    settings, tree = _tree()
    settings.processed_dir.mkdir(parents=True, exist_ok=True)
    out = settings.processed_dir / "tree.json"
    out.write_text(
        json.dumps({"revision": tree.revision, "nodes": [n.to_dict() for n in tree]}),
        encoding="utf-8",
    )
    console.print(tree.stats())
    console.print(f"[green]wrote[/green] {out}")


@schedule_app.command("validate")
def schedule_validate() -> None:
    """Validate the tree and write the report. Exits non-zero on any error."""
    settings, tree = _tree()
    manifest = settings.manifest() if settings.manifest_path.exists() else None
    report = validate_tree(tree, manifest)
    path = report.write(settings.reports_dir / "tree-validation.json")

    table = Table("check", "severity", "count")
    counts: dict[tuple[str, str], int] = {}
    for issue in report.issues:
        counts[(issue.check, issue.severity)] = counts.get((issue.check, issue.severity), 0) + 1
    for (check, severity), n in sorted(counts.items()):
        table.add_row(check, severity, str(n))
    console.print(table)
    console.print(f"report: {path}")

    if report.passed:
        console.print(f"[green]tree valid[/green] - {report.stats['nodes']} nodes")
    else:
        console.print(f"[red]{len(report.errors)} errors[/red]")
        raise typer.Exit(code=1)


@schedule_app.command("reconcile")
def schedule_reconcile(chapter: str = "61") -> None:
    """Parse a chapter from the PDF and check it row by row against the export.

    The PDF is the source; the export is the oracle that proves the parse.
    """
    import json

    from hsclass.schedule.parse_pdf import parse_pdf
    from hsclass.schedule.reconcile import export_rows_for_chapter, reconcile

    settings = get_settings()
    pdf = settings.raw_dir / f"chapter-{int(chapter)}.pdf"
    if not pdf.exists():
        raise typer.BadParameter(f"missing {pdf}; only downloaded chapters can be reconciled")

    parsed = parse_pdf(pdf)
    export = json.loads((settings.raw_dir / "hts-export.json").read_text(encoding="utf-8"))
    report = reconcile(parsed, export_rows_for_chapter(export, chapter.zfill(2)), chapter.zfill(2))
    path = report.write(settings.reports_dir / f"reconcile-ch{chapter.zfill(2)}.json")

    console.print(
        f"chapter {chapter}: {report.matched}/{report.export_rows} rows matched "
        f"({report.accuracy:.2%})"
    )
    if report.passed:
        console.print("[green]parse matches the published schedule exactly[/green]")
    else:
        table = Table("kind", "count")
        for kind, n in report.counts().items():
            table.add_row(kind, str(n))
        console.print(table)
    console.print(f"report: {path}")
    if not report.passed:
        raise typer.Exit(code=1)


@schedule_app.command("show")
def schedule_show(code: str) -> None:
    """Show one tariff line with the inherited chain that makes it embeddable."""
    _, tree = _tree()
    node = tree.by_code(code)
    if node is None:
        raise typer.BadParameter(f"{code} not found in this revision")
    console.print(f"[bold]{node.htsno}[/bold]  chapter {node.chapter}  indent {node.indent}")
    if node.subchapter:
        console.print(f"subchapter: {node.subchapter}")
    for part in tree.inherited_chain(node.key):
        console.print(f"  {part}")
    console.print(f"\nrates: general={node.general!r} special={node.special!r} col2={node.other!r}")


if __name__ == "__main__":
    app()
