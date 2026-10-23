"""What the review page does (E10), as plain functions the page calls and the tests can check.

A person opens a flagged document, corrects fields against the PDF, sees the checks re-run, and
approves it (saved as `ok`, PDF to processed/) or discards it (left out of the sheets, PDF to
needs-review/discarded/). Every decision is kept on the result as an audit trail: what changed,
from what, when, and any note. The exports are then rebuilt by `run.write_exports`.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from ibis_docs.config import Config
from ibis_docs.files import NEEDS_REVIEW, PROCESSED, free_name
from ibis_docs.memory import Memory, invoice_key
from ibis_docs.models import DocType, Invoice, Reason, Status
from ibis_docs.money import normalise_currency
from ibis_docs.results import Change, Result, Review, save
from ibis_docs.validate import validate

DISCARDED = "discarded"

HEADER_FIELDS = [
    "doc_type",
    "supplier_name",
    "supplier_tax_id",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "po_number",
    "subtotal",
    "tax",
    "discount",
    "shipping",
    "total",
]
LINE_FIELDS = ["description", "sku", "quantity", "unit_price", "line_total", "tax_rate"]
LABELS = {name: name.replace("_", " ") for name in HEADER_FIELDS + LINE_FIELDS}


class EditError(ValueError):
    """Form values that can't be read; `problems` are one plain sentence each."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _show(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, DocType):
        return value.value
    return str(value)


def form_values(invoice: Invoice) -> dict[str, str]:
    """The header fields as the form shows them: plain text, ISO dates, "" for empty."""
    return {name: _show(getattr(invoice, name)) for name in HEADER_FIELDS}


def line_values(invoice: Invoice) -> list[dict[str, str]]:
    return [{f: _show(getattr(line, f)) for f in LINE_FIELDS} for line in invoice.line_items]


def apply_edits(
    invoice: Invoice, header: dict[str, str], lines: list[dict[str, str]], config: Config
) -> tuple[Invoice, list[Change]]:
    """The invoice with the form's values, and what changed. Raises EditError for values that
    can't be read (a date not in YYYY-MM-DD form, an amount that isn't a number, ...)."""
    problems: list[str] = []
    values: dict[str, object] = {}
    for name in HEADER_FIELDS:
        text = (header.get(name) or "").strip()
        if name in ("invoice_date", "due_date") and text:
            try:
                values[name] = date.fromisoformat(text)
            except ValueError:
                problems.append(f"{LABELS[name]} '{text}': use the form YYYY-MM-DD")
            continue
        if name == "currency" and text:
            code = normalise_currency(text, config.validation.symbols)
            if code is None:
                problems.append(f"currency '{text}' isn't a currency code like GBP")
            values[name] = code
            continue
        values[name] = text or None
    values["doc_type"] = values["doc_type"] or DocType.INVOICE.value

    kept_lines = []
    for i, row in enumerate(lines, start=1):
        row = {f: (row.get(f) or "").strip() for f in LINE_FIELDS}
        if not any(row.values()):
            continue  # an empty row added by mistake
        if not row["description"]:
            problems.append(f"line {i} needs a description")
        kept_lines.append({f: v or None for f, v in row.items()})
    if problems:
        raise EditError(problems)

    data = invoice.model_dump(mode="json")
    data.update({k: _show(v) if isinstance(v, date) else v for k, v in values.items()})
    data["line_items"] = kept_lines
    try:
        edited = Invoice.model_validate(data)
    except ValidationError as exc:
        raise EditError([_plain(err) for err in exc.errors()]) from exc
    return edited, _changes(invoice, edited)


def _plain(err: dict) -> str:
    where = " ".join(str(p).replace("_", " ") for p in err["loc"] if p != "line_items")
    message = err["msg"].removeprefix("Value error, ")
    return f"{where}: {message}" if where else message


def _changes(before: Invoice, after: Invoice) -> list[Change]:
    old, new = form_values(before), form_values(after)
    changes = [
        Change(name, old[name], new[name]) for name in HEADER_FIELDS if old[name] != new[name]
    ]
    old_lines, new_lines = line_values(before), line_values(after)
    if len(old_lines) != len(new_lines):
        changes.append(Change("line_items", f"{len(old_lines)} lines", f"{len(new_lines)} lines"))
    for i, (a, b) in enumerate(zip(old_lines, new_lines, strict=False), start=1):
        changes += [
            Change(f"line {i} {LABELS[f]}", a[f], b[f]) for f in LINE_FIELDS if a[f] != b[f]
        ]
    return changes


def recheck(
    invoice: Invoice, result: Result, all_results: list[Result], config: Config, today: date
) -> list[Reason]:
    """The checks a run would make on these values, including duplicates of earlier documents."""
    reasons = validate(invoice, config, today=today)
    earlier: list[Result] = []
    for other in all_results:
        if other.filename() == result.filename():
            break
        if not other.discarded:
            earlier.append(other)
    key = invoice_key(invoice)
    for other in earlier:
        if other.invoice.source_file.sha256 == invoice.source_file.sha256:
            reasons.append(Reason(code="duplicate_file", message=f"identical file to {other.name}"))
            break
    for other in earlier:
        if key and invoice_key(other.invoice) == key:
            reasons.append(
                Reason(
                    code="duplicate",
                    message=f"same supplier and invoice number ({invoice.invoice_number}) "
                    f"as {other.name}",
                    field="invoice_number",
                )
            )
            break
    return reasons


def approve(
    config: Config,
    result: Result,
    invoice: Invoice,
    changes: list[Change],
    *,
    open_reasons: list[Reason],
    note: str,
    now: datetime,
) -> Result:
    """Sign a document off as `ok`. With checks still failing, a note saying why is required;
    the open reasons are kept in the audit trail."""
    note = note.strip()
    if open_reasons and not note:
        raise EditError(["say why you're approving it with checks still failing"])
    invoice = invoice.model_copy(update={"status": Status.OK, "reasons": []})
    result.invoice = invoice
    result.review = Review(
        action="approved",
        at=now,
        changes=changes,
        note=note,
        overridden=[r.message for r in open_reasons],
    )
    result.pdf_path = _move(result.pdf_path, config.input_path / PROCESSED)
    save(config.output_path, [result])

    key = invoice_key(invoice)
    if key:
        memory = Memory.load(config.output_path)
        memory.invoices.setdefault(key, result.name)
        memory.save(config.output_path)
    return result


def discard(config: Config, result: Result, *, note: str, now: datetime) -> Result:
    """Take a document out of the sheets (a duplicate, or not an invoice at all). Nothing is
    deleted: the result stays, marked, and the PDF moves to needs-review/discarded/."""
    result.review = Review(action="discarded", at=now, note=note.strip())
    result.pdf_path = _move(result.pdf_path, config.input_path / NEEDS_REVIEW / DISCARDED)
    save(config.output_path, [result])
    return result


def _move(pdf_path: str | None, folder: Path) -> str | None:
    """Move the PDF if it's still waiting in the inbox or needs-review/; returns where it is."""
    if not pdf_path or not Path(pdf_path).is_file():
        return pdf_path
    path = Path(pdf_path)
    if path.parent == folder:
        return pdf_path
    folder.mkdir(parents=True, exist_ok=True)
    target = free_name(folder, path.name)
    os.replace(path, target)
    return str(target)
