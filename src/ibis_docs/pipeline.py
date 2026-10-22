"""From found files to checked invoices: extract, check, retry hard cases, find duplicates.

    found PDFs -> read with the default model (in parallel)
               -> learn each supplier's date order from documents that settle it
               -> check every document (E5)
               -> retry the ones a stronger model might read differently (E4)
               -> flag duplicates (E6) and set each status (E7)

Nothing here writes files or moves anything; the caller decides what to keep.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import anthropic

from ibis_docs.config import Config
from ibis_docs.dates import Order, Settled
from ibis_docs.discover import Found
from ibis_docs.extract import Extraction, ReadContext, extract
from ibis_docs.memory import Memory, invoice_key
from ibis_docs.models import ExtractedInvoice, Invoice, Reason, SourceFile, Status, supplier_key
from ibis_docs.money import format_amount
from ibis_docs.validate import status_for, validate, worth_retrying

# Documents read at once. The SDK client is thread-safe; this keeps well inside rate limits.
WORKERS = 4

# Fields compared when two models read the same document differently.
_COMPARED = ["invoice_number", "invoice_date", "due_date", "currency"]
_COMPARED_AMOUNTS = ["subtotal", "tax", "discount", "shipping", "total"]


@dataclass
class Document:
    """One file's result: the final invoice and every model call made for it."""

    found: Found
    invoice: Invoice
    extractions: list[Extraction] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.found.name


@dataclass
class Outcome:
    documents: list[Document]
    learned_orders: dict[str, Order]  # supplier_key -> order settled by their own documents

    def count(self, status: Status) -> int:
        return sum(1 for d in self.documents if d.invoice.status == status)

    def tokens(self) -> dict[str, tuple[int, int]]:
        """(input, output) tokens per model."""
        totals: dict[str, tuple[int, int]] = {}
        for doc in self.documents:
            for ex in doc.extractions:
                i, o = totals.get(ex.model, (0, 0))
                totals[ex.model] = (i + ex.input_tokens, o + ex.output_tokens)
        return totals


def process(
    files: list[Found],
    config: Config,
    client: anthropic.Anthropic,
    *,
    today: date,
    memory: Memory | None = None,
    workers: int = WORKERS,
    on_progress: Callable[[str], None] | None = None,
) -> Outcome:
    """Read and check `files`. Raises ApiSetupError if the API can't be used at all."""
    memory = memory or Memory()
    note = on_progress or (lambda _msg: None)
    originals = [f for f in files if f.duplicate_of is None]

    # 1. Read every distinct file with the default model.
    first = _extract_all(originals, config, client, config.model.default, workers, note)

    # 2. A supplier's date order, learned from their documents that print it unambiguously
    #    (13/09/2026), settles their other documents in this run.
    learned = _learn_orders(first.values())
    context = ReadContext(today=today, date_orders={**memory.date_orders, **learned})

    # 3. Check everything; collect the documents worth a second read.
    checked: dict[str, tuple[Extraction, list[Reason]]] = {}
    for f in originals:
        ex = first[f.name]
        ex.reread(config, context)
        checked[f.name] = (ex, _check(ex, config, today))

    # 4. One retry on the escalation model for what a stronger model might read differently.
    escalate_to = config.model.escalate_to
    retry = [
        f
        for f in originals
        if escalate_to and (not checked[f.name][0].ok or worth_retrying(checked[f.name][1]))
    ]
    second = (
        _extract_all(retry, config, client, escalate_to, workers, note, fallback=True)
        if retry and escalate_to
        else {}
    )

    # 5. Pick each document's result, then flag duplicates in file order.
    by_name: dict[str, Document] = {}
    for f in originals:
        ex, reasons = checked[f.name]
        calls = [ex]
        if f.name in second:
            retried = second[f.name]
            retried.reread(config, context)
            calls.append(retried)
            ex, reasons = _choose(ex, reasons, retried, _check(retried, config, today))
        by_name[f.name] = Document(f, _invoice(f, ex, reasons), calls)

    documents: list[Document] = []
    seen = dict(memory.invoices)
    for f in files:
        if f.duplicate_of is not None:
            documents.append(_copy_of(f, by_name[f.duplicate_of]))
            continue
        doc = by_name[f.name]
        key = invoice_key(doc.invoice)
        if key and key in seen:
            earlier = seen[key]
            doc.invoice.reasons.append(
                Reason(
                    code="duplicate",
                    message=f"same supplier and invoice number ({doc.invoice.invoice_number}) "
                    f"as {earlier}",
                    field="invoice_number",
                )
            )
            doc.invoice.status = Status.REVIEW
        elif key:
            seen[key] = f.name
        documents.append(doc)
    return Outcome(documents, learned)


def _extract_all(
    files: list[Found],
    config: Config,
    client: anthropic.Anthropic,
    model: str,
    workers: int,
    note: Callable[[str], None],
    *,
    fallback: bool = False,
) -> dict[str, Extraction]:
    def one(f: Found) -> Extraction:
        result = extract(client, f.path, model, config, fallback=fallback)
        note(f"{f.name}: read by {result.model}")
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        # map() re-raises the first ApiSetupError here, which stops the run.
        return {f.name: ex for f, ex in zip(files, pool.map(one, files), strict=True)}


def _learn_orders(extractions: Iterable[Extraction]) -> dict[str, Order]:
    learned: dict[str, Order] = {}
    conflicting: set[str] = set()
    for ex in extractions:
        if not ex.ok or ex.date_order is None or ex.date_order.settled_by != Settled.PRINTED:
            continue
        key = supplier_key(ex.invoice.supplier_name)
        if not key or ex.date_order.order is None:
            continue
        if learned.get(key, ex.date_order.order) != ex.date_order.order:
            conflicting.add(key)
        learned[key] = ex.date_order.order
    return {k: v for k, v in learned.items() if k not in conflicting}


def _check(ex: Extraction, config: Config, today: date) -> list[Reason]:
    if not ex.ok:
        return [Reason(code="extraction_failed", message=f"{ex.model}: {ex.error}")]
    return validate(ex.invoice, config, today=today, issues=ex.issues)


def _choose(
    first: Extraction, first_reasons: list[Reason], second: Extraction, second_reasons: list[Reason]
) -> tuple[Extraction, list[Reason]]:
    """Keep whichever read passes the checks; if neither does, the stronger model's, noting where
    the two reads disagree."""
    if second.ok and not second_reasons:
        return second, second_reasons
    if not second.ok:
        if not first.ok:
            return second, first_reasons + second_reasons
        failed = Reason(
            code="retry_failed", message=f"the retry on {second.model} failed: {second.error}"
        )
        return first, [*first_reasons, failed]
    if not first.ok:
        return second, second_reasons
    differences = _differences(first, second)
    if differences:
        disagree = Reason(
            code="models_disagree",
            message=f"{first.model} and {second.model} read this differently: "
            + "; ".join(differences),
        )
        second_reasons = [*second_reasons, disagree]
    return second, second_reasons


def _differences(a: Extraction, b: Extraction) -> list[str]:
    ia, ib = a.invoice, b.invoice
    out = []
    for name in _COMPARED + _COMPARED_AMOUNTS:
        va, vb = getattr(ia, name), getattr(ib, name)
        if va != vb:
            show = (
                (lambda v: "—" if v is None else format_amount(v, ib.currency))
                if name in _COMPARED_AMOUNTS
                else (lambda v: "—" if v is None else str(v))
            )
            out.append(f"{name.replace('_', ' ')} {show(va)} vs {show(vb)}")
    if len(ia.line_items) != len(ib.line_items):
        out.append(f"{len(ia.line_items)} vs {len(ib.line_items)} line items")
    return out


def _invoice(f: Found, ex: Extraction, reasons: list[Reason]) -> Invoice:
    source = SourceFile(name=f.name, sha256=f.sha256)
    extracted = ex.invoice if ex.ok else ExtractedInvoice()
    invoice = Invoice.from_extracted(extracted, source, model=ex.model)
    invoice.reasons = reasons
    invoice.status = status_for(reasons, failed=not ex.ok)
    return invoice


def _copy_of(f: Found, original: Document) -> Document:
    """A byte-identical copy: same fields as the original, flagged, no second API call."""
    invoice = original.invoice.model_copy(
        update={
            "source_file": SourceFile(name=f.name, sha256=f.sha256),
            "reasons": [
                Reason(code="duplicate_file", message=f"identical file to {original.name}")
            ],
            "status": Status.REVIEW,
        },
        deep=True,
    )
    return Document(f, invoice, [])
