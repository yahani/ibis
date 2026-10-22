"""Command-line interface (E11)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from ibis_docs import __version__
from ibis_docs.config import Config, ConfigError, load_config
from ibis_docs.discover import check_pdf, discover, load_seen
from ibis_docs.extract import ApiSetupError, Extraction, extract, make_client

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
        _not_built("run", 3)

    try:
        found = discover(cfg.input_path, cfg.model.max_pages, load_seen(cfg.output_path))
    except FileNotFoundError as exc:
        typer.echo(f"✗ {exc} (create it, or change input_dir in {config})", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("Dry run: nothing is sent to Claude, written or moved.")
    typer.echo(f"{len(found.found)} PDF(s) to process in {cfg.input_path}:")
    width = max((len(f.name) for f in [*found.found, *found.skipped]), default=0)
    for f in found.found:
        note = f"  (same file as {f.duplicate_of})" if f.duplicate_of else ""
        typer.echo(f"  {f.name:<{width}}  {f.pages} page{'s' if f.pages != 1 else ''}{note}")
    if found.skipped:
        typer.echo(f"{len(found.skipped)} skipped:")
        for s in found.skipped:
            typer.echo(f"  {s.name:<{width}}  {s.reason}")
    typer.echo("Extraction and checks join the dry run in session 3.")


@app.command()
def show(
    file: Annotated[Path, typer.Argument(help="A PDF to extract and print, without saving.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Extract one PDF and print the fields, without saving anything."""
    cfg = _load_or_exit(config)
    if not file.is_file():
        typer.echo(f"✗ {file}: file not found", err=True)
        raise typer.Exit(code=1)
    _, reason = check_pdf(file, cfg.model.max_pages)
    if reason:
        typer.echo(f"✗ {file.name}: {reason}", err=True)
        raise typer.Exit(code=1)

    load_dotenv()
    try:
        result = extract(make_client(), file, cfg.model.default, cfg)
    except ApiSetupError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _print_extraction(result)
    if not result.ok:
        raise typer.Exit(code=1)


def _print_extraction(result: Extraction) -> None:
    typer.echo(
        f"{result.file}  ({result.model}, {result.input_tokens:,} input / "
        f"{result.output_tokens:,} output tokens)"
    )
    if result.error:
        typer.echo(f"✗ {result.error}")
        return
    inv = result.invoice
    assert inv is not None

    def val(value: object) -> str:
        return "—" if value is None else str(value)

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
    if result.issues:
        typer.echo("  could not read cleanly:")
        for issue in result.issues:
            typer.echo(f"    ! {issue}")
    typer.echo("  (The numbers aren't checked yet: validation comes in session 3.)")


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
