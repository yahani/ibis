"""Command-line interface (E11)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import anthropic
import typer
from dotenv import load_dotenv

from ibis_docs import __version__
from ibis_docs.config import Config, ConfigError, load_config
from ibis_docs.discover import Found, check_pdf, discover, load_seen, sha256_of
from ibis_docs.extract import ApiSetupError, make_client
from ibis_docs.memory import Memory
from ibis_docs.models import Status
from ibis_docs.pipeline import Document, Outcome, process
from ibis_docs.report import RETRY_SHARE, RunReport, cost_of, dollars
from ibis_docs.report import estimate as estimate_cost
from ibis_docs.run import keep

app = typer.Typer(
    help="Ibis reads supplier invoices, checks the numbers, and exports a clean sheet.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"ibis {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """Every invoice, read and checked."""


ConfigOption = Annotated[Path, typer.Option("--config", "-c", help="Path to the config file.")]
DEFAULT_CONFIG = Path("config.yaml")


def _load_or_exit(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("check-config")
def check_config(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Validate the config file and show what Ibis will do."""
    cfg = _load_or_exit(config)
    outputs = [
        name
        for name, on in (
            ("CSV", cfg.outputs.csv),
            ("Excel", cfg.outputs.excel),
            ("Google Sheet", cfg.outputs.google_sheet.enabled),
        )
        if on
    ]
    escalate = cfg.model.escalate_to or "off"
    typer.echo(f"✓ {config} is valid")
    typer.echo(f"  input:       {cfg.input_path}")
    typer.echo(f"  output:      {cfg.output_path} ({', '.join(outputs)})")
    typer.echo(f"  model:       {cfg.model.default} (escalate: {escalate})")
    typer.echo(f"  currencies:  {', '.join(cfg.validation.allowed_currencies)}")
    if cfg.validation.currency_symbols:
        remaps = ", ".join(f"{s} → {c}" for s, c in cfg.validation.currency_symbols.items())
        typer.echo(f"  symbols:     {remaps}")
    typer.echo(f"  move files:  {'yes' if cfg.move_files else 'no'}")
    typer.echo(f"  suppliers:   {len(cfg.suppliers)} hint(s)")


def today() -> date:
    """The run date (a function so tests can fix it)."""
    return date.today()


def now() -> datetime:
    return datetime.now()


@app.command()
def run(
    config: ConfigOption = DEFAULT_CONFIG,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Extract and check, but write and move nothing.")
    ] = False,
) -> None:
    """Process every new PDF in the input folder."""
    cfg = _load_or_exit(config)
    started = now()
    run_id = started.strftime("%Y%m%d-%H%M%S")
    try:
        found = discover(cfg.input_path, cfg.model.max_pages, load_seen(cfg.output_path))
    except FileNotFoundError as exc:
        typer.echo(f"✗ {exc} (create it, or change input_dir in {config})", err=True)
        raise typer.Exit(code=1) from exc

    if dry_run:
        typer.echo("Dry run: documents are read and checked, but nothing is written or moved.")
    width = max((len(f.name) for f in [*found.found, *found.skipped]), default=0)
    outcome = None
    if found.found:
        typer.echo(f"Reading {len(found.found)} document(s) from {cfg.input_path} ...")
        outcome = _process_or_exit(found.found, cfg)
        for doc in outcome.documents:
            _print_status_line(doc, width)
    if found.skipped:
        typer.echo(f"Skipped {len(found.skipped)}:")
        for s in found.skipped:
            typer.echo(f"  - {s.name:<{width}}  {s.reason}")
    if outcome is None:
        typer.echo(f"No new PDFs in {cfg.input_path}.")
        return

    report = RunReport.build(run_id, started, outcome, found.skipped, dry_run=dry_run)
    if not dry_run:
        kept = keep(outcome, cfg, run_id, started)
        report.moved, report.outputs = kept.moved, kept.outputs
        report.finished = now()
        report.save(cfg.output_path)
        for warning in kept.warnings:
            typer.echo(f"! {warning}", err=True)
    typer.echo("")
    for line in report.lines():
        typer.echo(line)
    if not dry_run:
        typer.echo(f"Sheets in {cfg.output_path}: {', '.join(report.outputs)}")


@app.command()
def show(
    file: Annotated[Path, typer.Argument(help="A PDF to extract and print, without saving.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Extract one PDF and print the fields and checks, without saving anything."""
    cfg = _load_or_exit(config)
    if not file.is_file():
        typer.echo(f"✗ {file}: file not found", err=True)
        raise typer.Exit(code=1)
    pages, reason = check_pdf(file, cfg.model.max_pages)
    if reason:
        typer.echo(f"✗ {file.name}: {reason}", err=True)
        raise typer.Exit(code=1)

    found = Found(file, sha256_of(file), pages or 0)
    doc = _process_or_exit([found], cfg).documents[0]
    _print_document(doc)
    if doc.invoice.status == Status.FAILED:
        raise typer.Exit(code=1)


def _process_or_exit(files: list[Found], cfg: Config) -> Outcome:
    load_dotenv()
    try:
        return process(
            files, cfg, make_client(), today=today(), memory=Memory.load(cfg.output_path)
        )
    except ApiSetupError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc


_MARK = {Status.OK: "✓", Status.REVIEW: "!", Status.FAILED: "✗"}


def _print_status_line(doc: Document, width: int) -> None:
    inv = doc.invoice
    retried = f"  ({inv.model})" if len(doc.extractions) > 1 else ""
    typer.echo(f"  {_MARK[inv.status]} {doc.name:<{width}}  {inv.status.value}{retried}")
    for reason in inv.reasons:
        typer.echo(f"      {reason.message}")


def _print_document(doc: Document) -> None:
    inv = doc.invoice
    calls = ", ".join(
        f"{ex.model} failed"
        if ex.error
        else f"{ex.model} {ex.input_tokens:,} in / {ex.output_tokens:,} out"
        for ex in doc.extractions
    )
    typer.echo(f"{doc.name}  ({calls})")

    def val(value: object) -> str:
        return "—" if value is None else str(value)

    if inv.status != Status.FAILED:
        supplier = val(inv.supplier_name)
        if inv.supplier_tax_id:
            supplier += f" (tax ID {inv.supplier_tax_id})"
        rows = [
            ("type", inv.doc_type.value),
            ("supplier", supplier),
            ("number", val(inv.invoice_number)),
            ("date", val(inv.invoice_date)),
            ("due", val(inv.due_date)),
            ("currency", val(inv.currency)),
            ("PO", val(inv.po_number)),
            ("subtotal", val(inv.subtotal)),
            ("tax", val(inv.tax)),
            ("discount", val(inv.discount)),
            ("shipping", val(inv.shipping)),
            ("total", val(inv.total)),
        ]
        for label, value in rows:
            typer.echo(f"  {label + ':':<10} {value}")

        typer.echo(f"  lines ({len(inv.line_items)}):")
        typer.echo(f"    {'qty':>8} {'unit price':>11} {'line total':>11}  description")
        for line in inv.line_items:
            sku = f" [{line.sku}]" if line.sku else ""
            typer.echo(
                f"    {val(line.quantity):>8} {val(line.unit_price):>11} "
                f"{val(line.line_total):>11}  {line.description}{sku}"
            )

    typer.echo(f"  {_MARK[inv.status]} {inv.status.value} (final read: {inv.model})")
    for reason in inv.reasons:
        typer.echo(f"    - {reason.message}")


@app.command()
def review(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Open the review page: check flagged documents against their PDFs, fix, approve."""
    _load_or_exit(config)
    from ibis_docs import review_app

    command = [
        sys.executable, "-m", "streamlit", "run", review_app.__file__,
        "--server.address", "localhost",  # this computer only
        "--browser.gatherUsageStats", "false",
        "--theme.primaryColor", "#23735F",
        "--theme.backgroundColor", "#F7F8F6",
        "--theme.textColor", "#1C2321",
    ]  # fmt: skip
    env = {**os.environ, review_app.CONFIG_ENV: str(config.resolve())}
    typer.echo("Opening the review page on http://localhost:8501 (Ctrl+C to stop) ...")
    raise typer.Exit(code=subprocess.call(command, env=env))


@app.command()
def cost(
    estimate: Annotated[
        Path, typer.Option("--estimate", help="Folder of PDFs to estimate the API cost for.")
    ],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Estimate the API cost of processing a folder (counts tokens; reads nothing)."""
    cfg = _load_or_exit(config)
    try:
        found = discover(estimate, cfg.model.max_pages)
    except FileNotFoundError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not found.found:
        typer.echo(f"No PDFs to estimate in {estimate}.")
        return

    load_dotenv()
    try:
        result = estimate_cost(found.found, cfg, make_client())
    except ApiSetupError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except anthropic.APIError as exc:
        typer.echo(f"✗ couldn't count tokens: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"{result.documents} document(s), {result.pages} page(s) in {estimate}")
    for model, (i, o) in result.tokens.items():
        typer.echo(f"  {model}: about {i:,} in / {o:,} out tokens, {dollars(cost_of(model, i, o))}")
    if cfg.model.escalate_to and result.cost != result.cost_without_retries:
        share = int(RETRY_SHARE * 100)
        typer.echo(
            f"Estimated cost: {dollars(result.cost_without_retries)} if every document checks "
            f"out first time; about {dollars(result.cost)} if {share}% need a retry on "
            f"{cfg.model.escalate_to}."
        )
    else:
        typer.echo(f"Estimated cost: {dollars(result.cost)}")
    typer.echo("(Input tokens are counted exactly; output tokens are estimated.)")
    if found.skipped:
        typer.echo(f"{len(found.skipped)} file(s) wouldn't be processed (not readable PDFs).")
