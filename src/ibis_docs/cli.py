"""Command-line interface (E11)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from ibis_docs import __version__
from ibis_docs.config import Config, ConfigError, load_config
from ibis_docs.discover import Found, check_pdf, discover, load_seen, sha256_of
from ibis_docs.extract import ApiSetupError, make_client
from ibis_docs.memory import Memory
from ibis_docs.models import Status
from ibis_docs.pipeline import Document, Outcome, process

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


def _not_built(command: str, session: int) -> None:
    typer.echo(f"`ibis {command}` isn't built yet (planned for session {session}).", err=True)
    raise typer.Exit(code=2)


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


@app.command()
def run(
    config: ConfigOption = DEFAULT_CONFIG,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Extract and check, but write and move nothing.")
    ] = False,
) -> None:
    """Process every new PDF in the input folder."""
    cfg = _load_or_exit(config)
    if not dry_run:
        _not_built("run", 4)

    try:
        found = discover(cfg.input_path, cfg.model.max_pages, load_seen(cfg.output_path))
    except FileNotFoundError as exc:
        typer.echo(f"✗ {exc} (create it, or change input_dir in {config})", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("Dry run: documents are read and checked, but nothing is written or moved.")
    width = max((len(f.name) for f in [*found.found, *found.skipped]), default=0)
    if found.found:
        typer.echo(f"Reading {len(found.found)} document(s) from {cfg.input_path} ...")
        outcome = _process_or_exit(found.found, cfg)
        for doc in outcome.documents:
            _print_status_line(doc, width)
    if found.skipped:
        typer.echo(f"Skipped {len(found.skipped)}:")
        for s in found.skipped:
            typer.echo(f"  - {s.name:<{width}}  {s.reason}")
    if not found.found:
        typer.echo(f"No new PDFs in {cfg.input_path}.")
        return
    typer.echo("")
    typer.echo(_summary(outcome))
    typer.echo(_tokens(outcome))


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


def _summary(outcome: Outcome) -> str:
    total = len(outcome.documents)
    ok, review = outcome.count(Status.OK), outcome.count(Status.REVIEW)
    failed = outcome.count(Status.FAILED)
    text = f"Ibis read {total} document{'s' if total != 1 else ''}. {ok} checked out."
    if review:
        text += f" {review} need{'s' if review == 1 else ''} a look."
    if failed:
        text += f" {failed} couldn't be read."
    return text


def _tokens(outcome: Outcome) -> str:
    parts = [f"{model} {i:,} in / {o:,} out" for model, (i, o) in outcome.tokens().items()]
    return "Tokens: " + ("; ".join(parts) if parts else "none")


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
def cost(
    estimate: Annotated[
        Path, typer.Option("--estimate", help="Folder of PDFs to estimate the API cost for.")
    ],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Estimate the API cost of processing a folder."""
    _load_or_exit(config)
    _not_built("cost --estimate", 4)
