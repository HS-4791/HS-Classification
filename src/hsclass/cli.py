from __future__ import annotations

import json
from pathlib import Path

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


def _rows_path(settings, source: str):
    """Where each source's flat rows live.

    The PDF is the source of truth: only it carries the Section, which appears
    nowhere in a code's digits and whose notes are binding. The export is kept
    as the oracle the parse is checked against.
    """
    if source == "pdf":
        return settings.processed_dir / "pdf-rows.json", (
            "run `python scripts/parse_full_schedule.py` to parse the PDF first"
        )
    return settings.raw_dir / "hts-export.json", "download the release first"


def _tree(source: str = "pdf"):
    settings = get_settings()
    path, hint = _rows_path(settings, source)
    if not path.exists():
        raise typer.BadParameter(f"missing {path}; {hint}")
    return settings, build_from_export(load_export(path), settings.require_revision())


@app.command("classify")
def classify(
    description: str,
    trace_to: str = typer.Option("", help="Where to append the trace; defaults to traces/"),
    budget_ms: float = typer.Option(
        -1.0, help="Per-request latency budget in ms; omit to use LATENCY_BUDGET_MS"
    ),
) -> None:
    """Classify one description and write a reconstructable trace.

    This is the only place the whole pipeline runs as a pipeline rather than
    as an evaluation, which is why tracing is wired here: a trace nobody ever
    reads is not observability, and the eval scripts deliberately leave it
    off so that 2,200 cases do not write 2,200 files.
    """
    from hsclass.obs.resources import Budget
    from hsclass.pipeline.adjudicated import AdjudicatedClassifier
    from hsclass.pipeline.gri import GRIEngine
    from hsclass.pipeline.traversal import HierarchicalClassifier, LevelIndex
    from hsclass.retrieval.embedder import get_embedder
    from hsclass.retrieval.index import VectorIndex
    from hsclass.retrieval.lexical import LexicalIndex
    from hsclass.schedule.exclusions import build_exclusion_graph
    from hsclass.schedule.notes import load_notes

    settings = get_settings()
    revision = settings.require_revision()
    index_dir = settings.processed_dir / "index-levels"
    if not (index_dir / "vectors.npy").exists():
        raise typer.BadParameter(f"missing {index_dir}; run scripts/run_ablations.py once")

    levelled = VectorIndex.load(index_dir)
    lexical = LexicalIndex.build(levelled.candidates)
    notes_path = settings.processed_dir / "notes.json"
    if not notes_path.exists():
        raise typer.BadParameter(
            f"missing {notes_path}; run `python scripts/extract_notes.py` first"
        )
    notes = load_notes(notes_path)
    proposer = HierarchicalClassifier(LevelIndex(levelled, lexical),
                                      get_embedder(settings.embedding_backend,
                                                   settings.embedding_model), k=5)
    sink = Path(trace_to) if trace_to else settings.traces_dir / f"{revision}.jsonl"
    # ``None`` is the only value meaning "no budget", and 0 is a real budget
    # of zero. ``budget_ms or settings.latency_budget_ms`` made a configured
    # budget impossible to override from the command line, and
    # LATENCY_BUDGET_MS=0 - the natural way to write "off" - failed every
    # request at stage 1 while ``hsclass config`` reported "unset".
    budget = Budget(
        usd=settings.cost_ceiling_usd_per_request,
        latency_ms=settings.latency_budget_ms if budget_ms < 0 else budget_ms,
    )
    classifier = AdjudicatedClassifier(
        proposer, GRIEngine(build_exclusion_graph(notes)),
        {c.code: c.text for c in levelled.candidates},
        schedule_version=revision, sink=sink, budget=budget,
    )

    decision, candidates = classifier.decide(description)
    if decision.code is not None:
        console.print(f"[green]{decision.code}[/green]  {decision.reason}")
    else:
        console.print("[yellow]escalated[/yellow] - the rules do not settle this")
        console.print(f"reason: {decision.reason}")
    console.print(f"rules evaluated: {' -> '.join(decision.rule_path)}")
    if decision.notes_cited:
        console.print(f"notes cited: {', '.join(decision.notes_cited)}")
    console.print(f"shortlist: {', '.join(decision.shortlist or candidates[:5])}")
    console.print(f"[dim]trace appended to {sink}[/dim]")


@app.command("config")
def show_config() -> None:
    """Print the active configuration and the pinned schedule version."""
    s = get_settings()
    table = Table(show_header=False)
    table.add_row("hts_revision", s.hts_revision)
    table.add_row("llm_model", s.llm_model)
    table.add_row("embedding", f"{s.embedding_backend} / {s.embedding_model}")
    table.add_row("cost ceiling", f"${s.cost_ceiling_usd_per_request:.4f} per request")
    table.add_row(
        "latency budget",
        f"{s.latency_budget_ms:.0f}ms per request"
        if s.latency_budget_ms is not None
        else "unset",
    )
    table.add_row("anthropic key", "set" if s.anthropic_api_key else "unset")
    console.print(table)


@schedule_app.command("build")
def schedule_build(source: str = "pdf") -> None:
    """Build the nomenclature tree. Source is "pdf" (default) or "export"."""
    settings, tree = _tree(source)
    settings.processed_dir.mkdir(parents=True, exist_ok=True)
    out = settings.processed_dir / "tree.json"
    out.write_text(
        json.dumps({"revision": tree.revision, "nodes": [n.to_dict() for n in tree]}),
        encoding="utf-8",
    )
    console.print(tree.stats())
    console.print(f"[green]wrote[/green] {out}")


@schedule_app.command("validate")
def schedule_validate(source: str = "pdf") -> None:
    """Validate the tree and write the report. Exits non-zero on any error."""
    settings, tree = _tree(source)
    # The manifest records the *export's* shape. A PDF-sourced tree legitimately
    # differs from it, and is checked instead by `schedule reconcile`, row by row.
    manifest = (
        settings.manifest()
        if source == "export" and settings.manifest_path.exists()
        else None
    )
    report = validate_tree(tree, manifest)
    path = report.write(settings.reports_dir / f"tree-validation-{source}.json")

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
        f"chapter {chapter}: {report.field_perfect}/{report.export_rows} rows correct "
        f"({report.accuracy:.2%}); {report.alignment:.2%} aligned"
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
def schedule_show(code: str, source: str = "pdf") -> None:
    """Show one tariff line with the inherited chain that makes it embeddable."""
    _, tree = _tree(source)
    node = tree.by_code(code)
    if node is None:
        raise typer.BadParameter(f"{code} not found in this revision")
    console.print(
        f"[bold]{node.htsno}[/bold]  Section {node.section}  "
        f"chapter {node.chapter}  indent {node.indent}"
    )
    if node.subchapter:
        console.print(f"subchapter: {node.subchapter}")
    for part in tree.inherited_chain(node.key):
        console.print(f"  {part}")
    console.print(f"\nrates: general={node.general!r} special={node.special!r} col2={node.other!r}")


if __name__ == "__main__":
    app()
