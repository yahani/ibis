"""Keep a run's results: save them, rebuild the exports, move the PDFs, update what Ibis remembers.

Order matters: results and exports are written before any PDF moves, so a failure part-way
never leaves files moved with no sheet to show for it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ibis_docs import export, files, results
from ibis_docs.config import Config
from ibis_docs.discover import record_seen
from ibis_docs.memory import Memory, invoice_key
from ibis_docs.models import Status
from ibis_docs.pipeline import Outcome
from ibis_docs.results import Call, Result

GOOGLE_CREDENTIALS_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"


@dataclass
class Kept:
    outputs: list[str] = field(default_factory=list)  # files (or the sheet URL) written
    moved: dict[str, int] = field(default_factory=dict)  # folder -> count
    warnings: list[str] = field(default_factory=list)


def plan_moves(outcome: Outcome, input_dir: Path) -> dict[str, Path]:
    """Where each PDF will go: a free name in processed/ or needs-review/."""
    planned: dict[str, Path] = {}
    taken: set[Path] = set()
    for doc in outcome.documents:
        folder = input_dir / files.folder_for(doc.invoice.status)
        target = files.free_name(folder, doc.name, taken)
        planned[doc.name] = target
        taken.add(target)
    return planned


def write_exports(config: Config) -> Kept:
    """Rebuild every export from all saved results (after a run, or a review-page edit)."""
    kept = Kept()
    output_dir = config.output_path
    sheets = export.build_sheets(results.load_all(output_dir))
    if config.outputs.csv:
        export.write_csv(output_dir / export.INVOICES_CSV, sheets[0])
        export.write_csv(output_dir / export.LINE_ITEMS_CSV, sheets[1])
        kept.outputs += [export.INVOICES_CSV, export.LINE_ITEMS_CSV]
    if config.outputs.excel:
        export.write_excel(output_dir / export.WORKBOOK, sheets)
        kept.outputs.append(export.WORKBOOK)
    if config.outputs.google_sheet.enabled:
        credentials = os.environ.get(GOOGLE_CREDENTIALS_ENV)
        if not credentials:
            kept.warnings.append(
                f"Google Sheet not updated: set {GOOGLE_CREDENTIALS_ENV} in .env to the service "
                "account's JSON key file"
            )
        else:
            try:
                url = export.write_google_sheet(
                    config.outputs.google_sheet.spreadsheet_id, sheets, Path(credentials)
                )
                kept.outputs.append(url)
            except Exception as exc:  # the CSV and Excel files are already written
                kept.warnings.append(f"Google Sheet not updated: {type(exc).__name__}: {exc}")
    return kept


def keep(outcome: Outcome, config: Config, run_id: str, now: datetime) -> Kept:
    kept = Kept()
    output_dir, input_dir = config.output_path, config.input_path
    targets = plan_moves(outcome, input_dir) if config.move_files else {}

    # 1. This run's results, next to every earlier run's.
    run_results = [
        Result(
            invoice=doc.invoice,
            processed_at=now,
            run_id=run_id,
            calls=[
                Call(ex.model, ex.input_tokens, ex.output_tokens, ex.error)
                for ex in doc.extractions
            ],
            pdf_path=str(targets.get(doc.name, doc.found.path)),
        )
        for doc in outcome.documents
    ]
    results.save(output_dir, run_results)

    # 2. The exports, rebuilt from all results so they're one ledger across runs.
    written = write_exports(config)
    kept.outputs, kept.warnings = written.outputs, written.warnings

    # 3. Move the PDFs.
    for doc in outcome.documents:
        target = targets.get(doc.name)
        if target is None:
            continue
        target.parent.mkdir(exist_ok=True)
        os.replace(doc.found.path, target)
        folder = target.parent.name
        kept.moved[folder] = kept.moved.get(folder, 0) + 1

    # 4. Remember what was seen. A failed document isn't, so dropping it in again retries it.
    record_seen(
        output_dir, [d.found for d in outcome.documents if d.invoice.status != Status.FAILED]
    )
    memory = Memory.load(output_dir)
    for doc in outcome.documents:
        key = invoice_key(doc.invoice)
        flagged = {r.code for r in doc.invoice.reasons} & {"duplicate", "duplicate_file"}
        if key and not flagged and doc.invoice.status != Status.FAILED:
            memory.invoices.setdefault(key, doc.name)
    memory.date_orders.update(outcome.learned_orders)
    memory.save(output_dir)
    return kept
