"""Command-line interface (E11)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ibis_docs import __version__
from ibis_docs.config import Config, ConfigError, load_config

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
    _load_or_exit(config)
    _not_built("run", 2)


@app.command()
def show(
    file: Annotated[Path, typer.Argument(help="A PDF to extract and print, without saving.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Extract one PDF and print the fields and checks."""
    _load_or_exit(config)
    _not_built("show", 2)


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
