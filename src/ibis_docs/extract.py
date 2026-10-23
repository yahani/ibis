"""Read a PDF with Claude (E3).

Claude only reads: it copies what is printed into `RawInvoice`, as text. Python then reads the
amounts, dates and currency (`to_extracted`), noting anything it couldn't read as a reason rather
than guessing. Checking that the numbers add up is validation's job (E5).
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import anthropic
from pydantic import ValidationError

from ibis_docs.config import Config, SupplierHint
from ibis_docs.dates import (
    DateOrder,
    Order,
    Settled,
    is_ambiguous,
    is_numeric,
    parse_date,
    resolve_order,
)
from ibis_docs.models import (
    DocType,
    ExtractedInvoice,
    LineItem,
    RawInvoice,
    RawLineItem,
    Reason,
    supplier_key,
)
from ibis_docs.money import (
    THREE_DECIMAL_CURRENCIES,
    infer_decimal_mark,
    is_ambiguous_amount,
    normalise_currency,
    parse_amount,
)

# Symbols used by more than one currency; a supplier hint in config decides which.
SHARED_SYMBOLS = frozenset({"$", "kr", "¥", "R"})

# A 20-page invoice with ~50 lines a page fits comfortably; non-streaming stays under timeouts.
MAX_TOKENS = 16000

# Server-side refusal fallback: if the model declines by mistake, the API re-runs the request on
# a model Anthropic picks for that kind of refusal. Used for the escalation model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You read supplier invoices, credit notes and packing slips, and copy what is printed on them \
into the given JSON schema. Your output is checked by software and then by a person, so an \
honest empty field is always better than a plausible guess.

Rules:
- Copy every value exactly as printed. Keep amounts as text with their thousands separators, \
decimal marks, signs, brackets and currency symbols, e.g. "1.299,00 €", "(12.00)", "-45.00". \
Keep dates exactly as printed, e.g. "13/09/2026" or "18 August 2026".
- Never guess or work anything out. If a field isn't printed on the document, return an empty \
string "". Don't calculate a missing subtotal, tax or total, don't turn a tax rate into an \
amount, don't convert currencies or date formats, and don't derive a due date from payment terms \
such as "Net 30".
- Don't correct the document. If its numbers don't add up, copy them as printed anyway.
- If a value is printed but you can't read it with confidence (blurred, cut off, smudged), return \
an empty string rather than your best reading.
- Include every line item from every page, in order. Subtotal, tax, discount, shipping and total \
rows are not line items.
- The supplier is the business that issued the document, not the customer it is addressed to."""

# Fields read as amounts, with the plain names used in reasons.
_AMOUNTS = {
    "subtotal": "subtotal",
    "tax": "tax",
    "discount": "discount",
    "shipping": "shipping",
    "total": "total",
}
_LINE_AMOUNTS = {
    "quantity": "quantity",
    "unit_price": "unit price",
    "line_total": "line total",
    "tax_rate": "tax rate",
}

# Printed countries that say which way round a supplier writes numeric dates. Canada writes both.
_MONTH_FIRST = {"us", "usa", "u s", "u s a", "united states", "united states of america"}
_DAY_FIRST = {
    "uk", "united kingdom", "gb", "great britain", "england", "scotland", "wales",
    "northern ireland", "ireland", "eire", "australia", "new zealand", "germany", "deutschland",
    "france", "spain", "espana", "españa", "italy", "italia", "netherlands", "nederland",
    "the netherlands", "belgium", "portugal", "austria", "österreich", "switzerland", "schweiz",
    "suisse", "denmark", "sweden", "norway", "finland", "poland", "india", "singapore",
    "south africa",
}  # fmt: skip


class ApiSetupError(Exception):
    """The API can't be used at all (no key, bad key, unknown model): stop the run."""


@dataclass(frozen=True)
class ReadContext:
    """What reading a document can use beyond the document itself."""

    today: date
    # Date order learned per supplier (by `supplier_key`) from documents that settle it.
    date_orders: Mapping[str, Order] = field(default_factory=dict)


@dataclass
class Reading:
    """A `RawInvoice` read into typed values."""

    invoice: ExtractedInvoice
    issues: list[Reason]  # values that couldn't be read cleanly
    date_order: DateOrder


@dataclass
class Extraction:
    """What one model call produced for one file."""

    file: str
    model: str  # the model that answered (a refusal fallback can differ from the one asked)
    invoice: ExtractedInvoice | None = None
    raw: RawInvoice | None = None
    issues: list[Reason] = field(default_factory=list)
    date_order: DateOrder | None = None
    error: str | None = None  # why nothing usable came back
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and self.invoice is not None

    def reread(self, config: Config, context: ReadContext) -> None:
        """Read the raw reply again, e.g. once more is known about the supplier's date order."""
        if self.raw is not None:
            self._apply(to_extracted(self.raw, config, context))

    def _apply(self, reading: Reading) -> None:
        self.invoice, self.issues, self.date_order = (
            reading.invoice,
            reading.issues,
            reading.date_order,
        )


def make_client() -> anthropic.Anthropic:
    """A client using credentials from the environment (ANTHROPIC_API_KEY, or a federation or
    `ant auth login` profile). Never a key from config."""
    try:
        return anthropic.Anthropic()
    except anthropic.CredentialsError as exc:
        raise _no_credentials(exc) from exc


def build_request(pdf: bytes, model: str, config: Config) -> dict:
    """The keyword arguments for `messages.parse`, without the output format."""
    text = "Read this document and fill in the schema."
    notes = [f"- {s.name}: {s.notes}" for s in config.suppliers if s.notes]
    if notes:
        text += (
            "\n\nLayout notes for known suppliers (use only if this document is from one):\n"
            + "\n".join(notes)
        )
    request = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(pdf).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": text},
                ],
            }
        ],
    }
    if config.model.effort:
        request["output_config"] = {"effort": config.model.effort}
    return request


def extract(
    client: anthropic.Anthropic,
    path: Path,
    model: str,
    config: Config,
    *,
    context: ReadContext | None = None,
    fallback: bool = False,
) -> Extraction:
    """Send one PDF to `model` and read the result. API problems with this one document come back
    as `Extraction.error`; problems that would hit every document raise ApiSetupError.

    `fallback` turns on the server-side refusal fallback (a beta, used for the escalation model).
    """
    result = Extraction(file=path.name, model=model)
    request = build_request(path.read_bytes(), model, config)

    try:
        if fallback:
            response = client.beta.messages.parse(
                **request, output_format=RawInvoice, betas=[FALLBACK_BETA], fallbacks="default"
            )
        else:
            response = client.messages.parse(**request, output_format=RawInvoice)
    except TypeError as exc:
        if "authentication" in str(exc).lower():
            raise ApiSetupError(
                "no API credentials: set ANTHROPIC_API_KEY in .env (see .env.example)"
            ) from exc
        raise
    except anthropic.CredentialsError as exc:
        raise _no_credentials(exc) from exc
    except anthropic.AuthenticationError as exc:
        raise ApiSetupError("the API key was rejected: check ANTHROPIC_API_KEY in .env") from exc
    except anthropic.PermissionDeniedError as exc:
        raise ApiSetupError(f"the API key can't use {model}: {_api_message(exc)}") from exc
    except anthropic.NotFoundError as exc:
        raise ApiSetupError(f"unknown model {model!r}: check model in config.yaml") from exc
    except ValidationError:
        # parse() validates the reply itself; a refusal or a cut-off reply isn't valid JSON.
        result.error = "Claude's reply didn't match the invoice schema (refused or cut off)"
        return result
    except anthropic.RateLimitError:
        result.error = "rate limited by the API after retries; run again later"
        return result
    except anthropic.APIStatusError as exc:
        result.error = f"the API returned an error ({exc.status_code}): {_api_message(exc)}"
        return result
    except anthropic.APIConnectionError:
        result.error = "couldn't reach the API (network problem or timeout)"
        return result

    result.model = response.model or model
    usage = response.usage
    result.input_tokens = (
        usage.input_tokens
        + (usage.cache_creation_input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
    )
    result.output_tokens = usage.output_tokens

    if response.stop_reason == "refusal":
        result.error = "Claude declined to read this document"
        return result
    if response.stop_reason == "max_tokens":
        result.error = "the reply was cut off (document too long for one request)"
        return result
    if response.parsed_output is None:
        result.error = "Claude returned no data for this document"
        return result

    result.raw = response.parsed_output
    result._apply(to_extracted(result.raw, config, context or ReadContext(today=date.today())))
    return result


def _no_credentials(exc: Exception) -> ApiSetupError:
    return ApiSetupError(f"no usable API credentials: set ANTHROPIC_API_KEY in .env ({exc})")


def _api_message(exc: anthropic.APIStatusError) -> str:
    """The API's own error message, without the SDK's 'Error code: ...' wrapping."""
    body = exc.body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message") or exc.message)
    return exc.message


# --- reading the printed text --------------------------------------------------------------


# Words that label a tax number ("VAT No.", "EIN", "ABN", "USt-IdNr.") rather than being part of it.
_TAX_LABELS = {
    "vat", "gst", "hst", "ein", "abn", "bn", "tin", "tax", "id", "no", "nr", "number", "reg",
    "registration", "momsreg", "momsregnr", "ust", "ustidnr", "ustid", "uid", "siret", "siren",
    "nif", "cif", "btw", "piva", "iva", "mwst", "tva",
}  # fmt: skip


def strip_tax_label(text: str) -> str:
    """'EIN 36-6106824' -> '36-6106824', 'VAT No. GB861142741' -> 'GB861142741'."""
    words = text.split()
    while len(words) > 1 and re.sub(r"[^a-z]", "", words[0].casefold()) in _TAX_LABELS:
        words.pop(0)
    return " ".join(words).lstrip(":").strip()


def find_hint(name: str | None, config: Config) -> SupplierHint | None:
    key = supplier_key(name)
    return next((s for s in config.suppliers if key and supplier_key(s.name) == key), None)


def country_order(country: str | None) -> Order | None:
    """The date order a printed country usually writes, if it's clear."""
    key = supplier_key(country)
    if key in _MONTH_FIRST:
        return Order.MONTH_FIRST
    if key in _DAY_FIRST:
        return Order.DAY_FIRST
    return None


def to_extracted(raw: RawInvoice, config: Config, context: ReadContext) -> Reading:
    """Read the printed text into typed values. Anything unreadable becomes None plus a reason."""
    issues: list[Reason] = []
    hint = find_hint(raw.supplier_name, config)

    currency = _currency(raw, hint, config, issues)
    date_order, dates = _dates(raw, hint, context, issues)

    texts = [getattr(raw, name) for name in _AMOUNTS]
    texts += [getattr(line, name) for line in raw.line_items for name in _LINE_AMOUNTS]
    mark = (hint.decimal_mark if hint else None) or infer_decimal_mark(texts)
    reader = _AmountReader(mark, currency, issues)

    amounts = {name: reader.read(getattr(raw, name), label) for name, label in _AMOUNTS.items()}
    if amounts["discount"] is not None:
        # Printed as "-38.59" or "38.59"; stored as an amount taken off, so the total is
        # subtotal + tax - discount + shipping. A credit note's amounts are all negative.
        magnitude = abs(amounts["discount"])
        amounts["discount"] = -magnitude if raw.doc_type == DocType.CREDIT_NOTE else magnitude
    lines = [_line(i, item, reader, issues) for i, item in enumerate(raw.line_items, start=1)]

    invoice = ExtractedInvoice(
        doc_type=raw.doc_type,
        supplier_name=raw.supplier_name,
        supplier_tax_id=strip_tax_label(raw.supplier_tax_id),
        invoice_number=raw.invoice_number,
        invoice_date=dates["invoice_date"],
        due_date=dates["due_date"],
        currency=currency,
        po_number=raw.po_number,
        line_items=lines,
        **amounts,
    )
    return Reading(invoice, issues, date_order)


def _currency(
    raw: RawInvoice, hint: SupplierHint | None, config: Config, issues: list[Reason]
) -> str | None:
    printed = raw.currency.strip()
    if not printed:
        return None
    code = normalise_currency(printed, config.validation.symbols)
    if code is None:
        issues.append(
            Reason(
                code="unknown_currency",
                message=f"currency '{printed}' isn't a known code or symbol",
                field="currency",
            )
        )
        return None
    if hint and hint.currency and code != hint.currency:
        # A symbol shared by several currencies ("$") is settled by a known supplier's hint.
        if printed in SHARED_SYMBOLS:
            return hint.currency
        issues.append(
            Reason(
                code="currency_conflict",
                message=f"currency printed as '{printed}' ({code}), "
                f"but config says {hint.currency}",
                field="currency",
            )
        )
    return code


def _dates(
    raw: RawInvoice, hint: SupplierHint | None, context: ReadContext, issues: list[Reason]
) -> tuple[DateOrder, dict[str, date | None]]:
    key = supplier_key(raw.supplier_name)
    order_hint = (
        (hint.date_order if hint else None)
        or (context.date_orders.get(key) if key else None)
        or country_order(raw.supplier_country)
    )
    date_order = resolve_order(raw.invoice_date, raw.due_date, today=context.today, hint=order_hint)
    labels = {"invoice_date": "invoice date", "due_date": "due date"}
    dates: dict[str, date | None] = {}

    if date_order.order is None:
        if date_order.settled_by == Settled.CONFLICT:
            issues.append(
                Reason(
                    code="conflicting_dates",
                    message=f"the dates don't agree on day/month order: invoice date "
                    f"'{raw.invoice_date}', due date '{raw.due_date}'",
                    field="invoice_date",
                )
            )
        for name, label in labels.items():
            text = getattr(raw, name)
            conflicted = date_order.settled_by == Settled.CONFLICT and is_numeric(text)
            if conflicted or is_ambiguous(text):
                dates[name] = None
                if date_order.settled_by == Settled.AMBIGUOUS and is_ambiguous(text):
                    day_first = parse_date(text, Order.DAY_FIRST)
                    month_first = parse_date(text, Order.MONTH_FIRST)
                    issues.append(
                        Reason(
                            code="ambiguous_date",
                            message=f"{label} '{text}' could be {_day(day_first)} or "
                            f"{_day(month_first)}; nothing on the document settles which",
                            field=name,
                        )
                    )
                continue
            dates[name] = _date(text, Order.DAY_FIRST, label, name, issues)
        return date_order, dates

    for name, label in labels.items():
        dates[name] = _date(getattr(raw, name), date_order.order, label, name, issues)
    return date_order, dates


def _date(text: str, order: Order, label: str, name: str, issues: list[Reason]) -> date | None:
    try:
        return parse_date(text, order)
    except ValueError:
        issues.append(
            Reason(
                code="unreadable_date",
                message=f"{label} '{text}' isn't a readable date",
                field=name,
            )
        )
        return None


def _day(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


@dataclass
class _AmountReader:
    mark: str | None  # the document's decimal mark, if known
    currency: str | None
    issues: list[Reason]

    def read(self, text: str, label: str, field_name: str | None = None) -> Decimal | None:
        field_name = field_name or label
        if (
            self.mark is None
            and self.currency in THREE_DECIMAL_CURRENCIES
            and is_ambiguous_amount(text)
        ):
            self.issues.append(
                Reason(
                    code="ambiguous_amount",
                    message=f"{label} '{text}' could be read with either mark as the decimal "
                    f"point, and nothing else on the document shows which {self.currency} uses",
                    field=field_name,
                )
            )
            return None
        try:
            return parse_amount(text, self.mark)
        except ValueError:
            self.issues.append(
                Reason(
                    code="unreadable_amount",
                    message=f"{label} '{text}' isn't a readable amount",
                    field=field_name,
                )
            )
            return None


def _line(number: int, item: RawLineItem, reader: _AmountReader, issues: list[Reason]) -> LineItem:
    description = item.description.strip()
    if not description:
        issues.append(
            Reason(
                code="missing_description",
                message=f"line {number} has no description",
                field=f"line_items[{number - 1}].description",
            )
        )
    return LineItem(
        description=description or f"(line {number}: no description printed)",
        sku=item.sku,
        **{
            name: reader.read(
                getattr(item, name), f"line {number} {label}", f"line_items[{number - 1}].{name}"
            )
            for name, label in _LINE_AMOUNTS.items()
        },
    )
