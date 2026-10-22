"""Generate fake supplier invoices, packing slips and credit notes, plus an answer key.

Every company, address and number here is invented. The output is deterministic for a given
seed and currency list, so the PDFs in the repo only change when this script does.

    uv run python demo/generate.py                          # USD, GBP, EUR
    uv run python demo/generate.py --currencies USD,AUD,NZD
    uv run python demo/generate.py --contact-sheet          # also write demo/contact-sheet.png

answers.json holds each document's fields exactly as printed (ISO dates, amounts as strings),
the status Ibis should give it, and for deliberately broken documents the reason it should be
flagged. Reason codes are:

    line_math            a line total isn't quantity x unit price
    lines_vs_subtotal    the lines don't add up to the subtotal
    total_mismatch       subtotal + tax - discount + shipping isn't the total
    due_before_invoice   the due date is before the invoice date
    missing_invoice_number
    currency_not_allowed the currency isn't in the allowed list used for this set
    duplicate            same supplier and invoice number as an earlier document
    duplicate_file       byte-for-byte copy of an earlier file
    hard_to_read         a blurred, tilted scan: must be read correctly or flagged, never wrong
"""

from __future__ import annotations

import argparse
import io
import json
import random
import shutil
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ibis_docs.money import is_iso_currency

# Same bytes on every run: no timestamps or random IDs inside the PDFs.
rl_config.invariant = 1

DEMO_DIR = Path(__file__).resolve().parent
DEFAULT_CURRENCIES = ["USD", "GBP", "EUR"]
DEFAULT_SEED = 22102026
CLEAN_COUNT = 36

INK = colors.HexColor("#1C2321")
GREY = colors.HexColor("#6B7370")
RULE = colors.HexColor("#C9CEC9")


# --------------------------------------------------------------------------------------------
# Currencies: anything not listed here still works, printed as "NZD 1,234.00".


@dataclass(frozen=True)
class CurrencyStyle:
    symbol: str
    after: bool = False  # "12,00 €" rather than "€12.00"
    decimals: int = 2
    tax_label: str = "VAT"
    tax_rates: tuple[str, ...] = ("20",)
    tax_id_label: str | None = "VAT No."
    tax_id_format: str = "GB{d9}"


CURRENCY_STYLES: dict[str, CurrencyStyle] = {
    "USD": CurrencyStyle(
        "$",
        tax_label="Sales tax",
        tax_rates=("8.25", "6", "0"),
        tax_id_label="EIN",
        tax_id_format="{d2}-{d7}",
    ),
    "GBP": CurrencyStyle("£", tax_rates=("20",), tax_id_format="GB{d9}"),
    "EUR": CurrencyStyle("€", tax_rates=("19", "21", "23"), tax_id_format="DE{d9}"),
    "AUD": CurrencyStyle(
        "A$",
        tax_label="GST",
        tax_rates=("10",),
        tax_id_label="ABN",
        tax_id_format="{d2} {d3} {d3} {d3}",
    ),
    "CAD": CurrencyStyle(
        "C$",
        tax_label="GST/HST",
        tax_rates=("13", "5"),
        tax_id_label="BN",
        tax_id_format="{d9}RT0001",
    ),
    "NZD": CurrencyStyle(
        "NZ$",
        tax_label="GST",
        tax_rates=("15",),
        tax_id_label="GST No.",
        tax_id_format="{d3}-{d3}-{d3}",
    ),
    "JPY": CurrencyStyle(
        "¥",
        decimals=0,
        tax_label="Consumption tax",
        tax_rates=("10",),
        tax_id_label="Reg. No.",
        tax_id_format="T{d9}{d4}",
    ),
    "CHF": CurrencyStyle("CHF ", tax_rates=("8.1",), tax_id_format="CHE-{d3}.{d3}.{d3}"),
    "SEK": CurrencyStyle(
        " kr",
        after=True,
        tax_label="Moms",
        tax_rates=("25",),
        tax_id_label="Momsreg.nr",
        tax_id_format="SE{d9}01",
    ),
}


def currency_style(code: str) -> CurrencyStyle:
    return CURRENCY_STYLES.get(code, CurrencyStyle(f"{code} ", tax_rates=("10",)))


# --------------------------------------------------------------------------------------------
# Suppliers and what they sell. All invented.

BUYER = ("Wrenfield Home & Garden", ["Unit 4, Mill Lane Trading Estate", "Ashby, AB1 2CD"])


@dataclass(frozen=True)
class Supplier:
    name: str
    address: tuple[str, ...]
    layout: str
    number_format: str  # e.g. "INV-{y}-{n:04d}"
    products: tuple[tuple[str, str, str, str], ...]  # description, sku prefix, min, max price
    home_currency: str = "GBP"  # used when it's in the requested list
    tax_id_format: str | None = None  # overrides the currency's usual format
    comma_decimals: bool = False  # prints 1.299,00
    date_style: str = "iso"


SUPPLIERS = [
    Supplier(
        "Harbourline Packaging Ltd",
        ("12 Quay Street", "Portsea, PO1 3HT"),
        "classic",
        "INV-{y}-{n:04d}",
        (
            ("Double-wall shipping box 40x30x20", "BX", "0.85", "2.40"),
            ("Kraft mailer bag, 250 pack", "MB", "18.00", "32.00"),
            ("Paper void fill roll 450mm", "VF", "24.00", "41.00"),
            ("Fragile tape 48mm x 66m", "TP", "1.10", "2.90"),
        ),
        date_style="long",
    ),
    Supplier(
        "Copperleaf Print Co.",
        ("88 Foundry Road", "Leeds LS9 8AB"),
        "modern",
        "CP{n:05d}",
        (
            ("Thank-you cards A6, 500", "TC", "29.00", "55.00"),
            ("Product labels 50x30 roll", "LB", "12.50", "19.90"),
            ("Branded tissue paper, 480 sheets", "TS", "38.00", "64.00"),
            ("Kraft swing tags, 1000", "ST", "21.00", "34.00"),
        ),
        date_style="short_uk",
    ),
    Supplier(
        "Larchmont Labels Inc.",
        ("2201 Commerce Ave, Suite 300", "Dayton, OH 45402"),
        "us_sales",
        "{n:06d}",
        (
            ("Thermal shipping labels 4x6, 500/roll", "TL-46", "11.00", "18.50"),
            ("Barcode labels 2x1, 1000/roll", "BC-21", "7.25", "12.75"),
            ("Ribbon wax 110mm x 74m", "RB-110", "6.40", "9.80"),
            ("Label printer service kit", "SK-01", "45.00", "89.00"),
        ),
        home_currency="USD",
        date_style="us",
    ),
    Supplier(
        "Fenwick & Dray Supplies",
        ("Bay 7, Northgate Industrial Park", "Carlisle CA3 0FD"),
        "compact",
        "FD/{y2}/{n:04d}",
        (
            ("Nitrile gloves, box of 100", "GL", "4.20", "7.90"),
            ("Surface cleaner 5L", "SC", "9.50", "16.00"),
            ("Blue roll 2-ply, 6 pack", "BR", "8.80", "13.40"),
            ("Bin liners heavy duty, 200", "BL", "11.00", "19.00"),
        ),
        date_style="iso",
    ),
    Supplier(
        "Kittiwake Coffee Roasters",
        ("Unit 2, Harbour Arches", "Whitby YO21 1DN"),
        "plain_text",
        "KW{n:05d}",
        (
            ("House blend beans 1kg", "HB1K", "14.50", "19.00"),
            ("Single origin Ethiopia 1kg", "ET1K", "21.00", "27.50"),
            ("Decaf blend 500g", "DC500", "8.90", "12.00"),
            ("Compostable cups 8oz, 1000", "CU8", "39.00", "58.00"),
        ),
        date_style="short_uk",
    ),
    Supplier(
        "Brandt & Vogel Textil GmbH",
        ("Wiesenstraße 14", "50667 Köln"),
        "euro",
        "RE-{y}-{n:05d}",
        (
            ("Cotton tote bag natural", "TB-N", "1.35", "2.80"),
            ("Organic tea towel 50x70", "TT-O", "3.10", "5.40"),
            ("Linen napkin set of 4", "LN-4", "9.80", "15.00"),
            ("Canvas apron, adjustable", "AP-C", "7.60", "12.20"),
        ),
        home_currency="EUR",
        comma_decimals=True,
        date_style="dotted",
    ),
    Supplier(
        "Saltmarsh Candle Works",
        ("The Old Granary, 9 Water Street", "Mystic, CT 06355"),
        "classic",
        "SCW-{n:04d}",
        (
            ("Soy wax flakes 20kg", "WX20", "52.00", "78.00"),
            ("Cotton wicks 150mm, 100", "WK15", "4.50", "8.00"),
            ("Amber jar 30cl with lid", "JA30", "0.95", "1.80"),
            ("Fragrance oil 500ml", "FO50", "18.00", "29.00"),
        ),
        home_currency="USD",
        date_style="us_long",
    ),
    Supplier(
        "Quillon Office Goods",
        ("410 Market Street", "Burlington, VT 05401"),
        "modern",
        "Q-{y}{n:04d}",
        (
            ("Copy paper A4 80gsm, box of 5", "CPA4", "21.00", "29.50"),
            ("Gel pens black, 12", "GP12", "6.20", "9.90"),
            ("Archive box, 10 pack", "AB10", "14.00", "22.00"),
            ("Desk organiser, bamboo", "DO-B", "17.50", "26.00"),
        ),
        home_currency="USD",
        date_style="us_long",
    ),
    Supplier(
        "Oriel Botanicals",
        ("3 Glasshouse Yard, Pearse Street", "Dublin 2, D02 X285"),
        "compact",
        "OB{y2}-{n:03d}",
        (
            ("Dried lavender bunch", "LAV", "2.10", "3.90"),
            ("Eucalyptus stems, 10", "EUC", "6.50", "9.00"),
            ("Pampas grass natural, 5", "PMP", "8.00", "13.50"),
            ("Florist wire 0.7mm reel", "FWR", "1.90", "3.20"),
        ),
        home_currency="EUR",
        tax_id_format="IE{d7}WA",
        date_style="dotted",
    ),
]


# --------------------------------------------------------------------------------------------
# Documents


@dataclass
class Line:
    description: str
    sku: str | None
    quantity: Decimal
    unit_price: Decimal | None
    line_total: Decimal | None
    tax_rate: Decimal | None = None


@dataclass
class Doc:
    file: str
    supplier: Supplier
    currency: str | None
    doc_type: str
    invoice_number: str | None
    invoice_date: date
    due_date: date | None
    po_number: str | None
    tax_id: str | None
    tax_id_label: str | None
    lines: list[Line]
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    discount: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal | None = None
    per_line_tax: bool = False
    tax_rate: Decimal | None = None
    reference: str | None = None  # credit notes: the invoice they credit
    stamp: str | None = None  # e.g. "COPY" on a resent invoice
    expected_status: str = "ok"
    expected_reasons: list[str] = field(default_factory=list)
    note: str = ""


def _q(amount: Decimal, decimals: int) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def _digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def _tax_id(rng: random.Random, fmt: str) -> str:
    return fmt.format(**{f"d{n}": _digits(rng, n) for n in (2, 3, 4, 7, 9)})


def _number(supplier: Supplier, n: int, year: int) -> str:
    return supplier.number_format.format(n=n, y=year, y2=year % 100)


def compute_totals(doc: Doc, decimals: int) -> None:
    """Fill subtotal/tax/total so that everything adds up (broken docs are edited after)."""
    for line in doc.lines:
        line.line_total = _q(line.quantity * line.unit_price, decimals)
    doc.subtotal = sum((line.line_total for line in doc.lines), Decimal(0))
    base = doc.subtotal - (doc.discount or 0)
    if doc.per_line_tax:
        share = base / doc.subtotal if doc.subtotal else Decimal(1)
        doc.tax = _q(sum(lt.line_total * lt.tax_rate / 100 for lt in doc.lines) * share, decimals)
    elif doc.tax_rate is not None:
        doc.tax = _q(base * doc.tax_rate / 100, decimals)
    doc.total = doc.subtotal + (doc.tax or 0) - (doc.discount or 0) + (doc.shipping or 0)


VARIANTS = [
    "",
    ", white",
    ", black",
    ", large",
    ", small",
    ", natural",
    ", blue",
    ", green",
    ", grey",
    ", red",
    ", oak",
    ", navy",
]


def make_lines(
    rng: random.Random,
    supplier: Supplier,
    count: int,
    decimals: int,
    scale: Decimal,
    priced: bool = True,
) -> list[Line]:
    lines = []
    for i in range(count):
        desc, sku, lo, hi = supplier.products[i % len(supplier.products)]
        if count > len(supplier.products):
            desc += VARIANTS[(i // len(supplier.products)) % len(VARIANTS)]
        price = Decimal(rng.randint(int(Decimal(lo) * 100), int(Decimal(hi) * 100))) / 100
        price = _q(price * scale, decimals)
        qty = Decimal(rng.choice([1, 2, 3, 5, 6, 10, 12, 20, 24, 25, 50, 100]))
        sku_code = f"{sku}-{rng.randint(100, 999)}" if rng.random() < 0.85 else None
        lines.append(Line(desc, sku_code, qty, price if priced else None, None))
    return lines


def build_documents(currencies: list[str], seed: int) -> tuple[list[Doc], list[str]]:
    """Return (documents, allowed currencies) for one demo set."""
    rng = random.Random(seed)
    # Each supplier bills in its home currency when that's requested, else one of the requested.
    supplier_currency = {
        s.name: s.home_currency
        if s.home_currency in currencies
        else currencies[i % len(currencies)]
        for i, s in enumerate(SUPPLIERS)
    }
    counters = {s.name: rng.randint(40, 900) for s in SUPPLIERS}
    docs: list[Doc] = []

    def next_number(s: Supplier, year: int) -> str:
        counters[s.name] += rng.randint(1, 7)
        return _number(s, counters[s.name], year)

    def new_doc(
        i: int,
        s: Supplier,
        doc_type: str = "invoice",
        n_lines: int | None = None,
        currency: str | None = None,
    ) -> Doc:
        cur = currency or supplier_currency[s.name]
        style = currency_style(cur)
        scale = Decimal(150) if style.decimals == 0 else Decimal(1)
        issued = date(2026, 7, 1) + timedelta(days=rng.randint(0, 110))
        n_lines = n_lines or rng.randint(1, 8)
        priced = doc_type != "packing_slip"
        doc = Doc(
            file="",
            supplier=s,
            currency=cur if priced else None,
            doc_type=doc_type,
            invoice_number=next_number(s, issued.year),
            invoice_date=issued,
            due_date=issued + timedelta(days=rng.choice([14, 30, 30, 45, 60]))
            if priced and doc_type == "invoice" and rng.random() < 0.8
            else None,
            po_number=f"PO-{rng.randint(10000, 99999)}" if rng.random() < 0.6 else None,
            tax_id=_tax_id(rng, s.tax_id_format or style.tax_id_format)
            if style.tax_id_label
            else None,
            tax_id_label=style.tax_id_label,
            lines=make_lines(rng, s, n_lines, style.decimals, scale, priced),
        )
        if not priced:
            doc.invoice_number = "PS-" + doc.invoice_number
            return doc
        doc.per_line_tax = s.layout == "compact"
        rate = Decimal(rng.choice(style.tax_rates))
        if doc.per_line_tax:
            for line in doc.lines:
                line.tax_rate = rate if rng.random() < 0.75 else Decimal(0)
        else:
            doc.tax_rate = rate
        if rng.random() < 0.35:
            doc.shipping = _q(Decimal(rng.randint(395, 2500)) / 100 * scale, style.decimals)
        compute_totals(doc, style.decimals)
        if rng.random() < 0.2 and doc.subtotal > 50 * scale:
            doc.discount = _q(doc.subtotal * Decimal("0.05"), style.decimals)
            compute_totals(doc, style.decimals)
        if doc_type == "credit_note":
            # Returns are printed as negative quantities, so every amount is negative.
            for line in doc.lines:
                line.quantity = -line.quantity
            doc.shipping = None
            doc.discount = None
            doc.po_number = None
            doc.reference = next_number(s, issued.year)
            doc.invoice_number = "CN-" + doc.invoice_number
            compute_totals(doc, style.decimals)
        return doc

    # Clean set: invoices, two long multi-page ones, packing slips and credit notes.
    kinds = ["invoice"] * (CLEAN_COUNT - 6) + ["packing_slip"] * 3 + ["credit_note"] * 3
    rng.shuffle(kinds)
    long_ones = {5, 17}
    for i, kind in enumerate(kinds):
        s = SUPPLIERS[i % len(SUPPLIERS)]
        n_lines = 46 if i in long_ones and kind == "invoice" else None
        doc = new_doc(i, s, kind, n_lines)
        if n_lines:
            doc.note = "multi-page"
        if doc.po_number is None or doc.due_date is None:
            doc.note = (doc.note + "; " if doc.note else "") + "some optional fields not printed"
        docs.append(doc)

    def broken(reason: str, note: str, s: Supplier, **kw) -> Doc:
        doc = new_doc(len(docs), s, **kw)
        doc.expected_status = "review"
        doc.expected_reasons = [reason]
        doc.note = note
        return doc

    style_of = {s.name: currency_style(supplier_currency[s.name]) for s in SUPPLIERS}
    unit = {name: Decimal(1).scaleb(-st.decimals) for name, st in style_of.items()}

    d = broken("line_math", "line 2 total is 10x qty x price", SUPPLIERS[0], n_lines=4)
    d.lines[1].line_total = d.lines[1].line_total * 10
    d.subtotal = sum(line.line_total for line in d.lines)
    base = d.subtotal - (d.discount or 0)
    d.tax = _q(base * d.tax_rate / 100, style_of[d.supplier.name].decimals)
    d.total = d.subtotal + d.tax - (d.discount or 0) + (d.shipping or 0)
    docs.append(d)

    d = broken(
        "lines_vs_subtotal", "subtotal is 12.40 more than the lines", SUPPLIERS[1], n_lines=5
    )
    bump = Decimal("12.40") if unit[d.supplier.name] < 1 else Decimal(1240)
    d.subtotal += bump
    d.total += bump
    docs.append(d)

    d = broken("total_mismatch", "total is off by 100", SUPPLIERS[2], n_lines=3)
    d.total += Decimal(100)
    docs.append(d)

    d = broken("due_before_invoice", "due date 15 days before invoice date", SUPPLIERS[3])
    d.due_date = d.invoice_date - timedelta(days=15)
    docs.append(d)

    d = broken("missing_invoice_number", "no invoice number printed", SUPPLIERS[4])
    d.invoice_number = None
    docs.append(d)

    other = next(c for c in ("CHF", "SEK", "JPY", "NZD", "CAD") if c not in currencies)
    d = broken(
        "currency_not_allowed", f"{other} is not in the allowed list", SUPPLIERS[6], currency=other
    )
    docs.append(d)

    # Same supplier and number as a clean invoice, resent with a COPY stamp (different bytes).
    original = next(
        x
        for x in docs
        if x.doc_type == "invoice" and not x.expected_reasons and x.supplier.layout != "plain_text"
    )
    d = replace(
        original,
        lines=list(original.lines),
        stamp="COPY",
        expected_status="review",
        expected_reasons=["duplicate"],
        note=f"resend of {original.invoice_number}",
    )
    docs.append(d)

    # A blurred, slightly rotated scan of an otherwise clean invoice.
    d = new_doc(len(docs), SUPPLIERS[5], n_lines=3)
    d.expected_status = "ok_or_review"
    d.expected_reasons = ["hard_to_read"]
    d.note = "scanned: blurred and tilted; flag it or read it right"
    docs.append(d)

    for i, doc in enumerate(docs, start=1):
        prefix = {"invoice": "inv", "packing_slip": "slip", "credit_note": "credit"}[doc.doc_type]
        doc.file = f"{i:03d}-{prefix}-{doc.supplier.layout}.pdf"
    return docs, list(currencies)


# --------------------------------------------------------------------------------------------
# Formatting as printed


def fmt_money(amount: Decimal | None, doc: Doc, symbol: bool = True, parens: bool = False) -> str:
    """As the supplier prints it: "£1,299.00", "1.299,00 €", "(12.00)" or "-12.00"."""
    if amount is None:
        return ""
    style = currency_style(doc.currency or "USD")
    number = f"{abs(amount):,.{style.decimals}f}"
    if doc.supplier.comma_decimals:
        number = number.translate(str.maketrans(",.", ".,"))
    if symbol:
        if style.after or doc.supplier.comma_decimals:
            number = f"{number} {style.symbol.strip()}"
        else:
            number = f"{style.symbol}{number}"
    if amount < 0:
        return f"({number})" if parens else f"-{number}"
    return number


def fmt_qty(qty: Decimal) -> str:
    return f"{qty:,f}".rstrip("0").rstrip(".") if qty % 1 else f"{int(qty):,}"


def fmt_date(d: date | None, style: str) -> str:
    if d is None:
        return ""
    return {
        "iso": d.isoformat(),
        "long": f"{d.day} {d:%B %Y}",
        "short_uk": f"{d:%d/%m/%Y}",
        "us": f"{d:%m/%d/%Y}",
        "us_long": f"{d:%b} {d.day}, {d.year}",
        "dotted": f"{d:%d.%m.%Y}",
    }[style]


def fmt_rate(rate: Decimal | None) -> str:
    return "" if rate is None else f"{rate.normalize():f}%"


# --------------------------------------------------------------------------------------------
# Layouts


def _p(
    text: str,
    size: float = 9,
    bold: bool = False,
    align: int = 0,
    color=INK,
    font: str = "Helvetica",
) -> Paragraph:
    style = ParagraphStyle(
        "p",
        fontName=f"{font}-Bold" if bold else font,
        fontSize=size,
        leading=size * 1.25,
        textColor=color,
        alignment=align,
    )
    return Paragraph(text.replace("&", "&amp;").replace("\n", "<br/>"), style)


def _target(path):
    """reportlab wants a str filename or a file-like object, not a Path."""
    return str(path) if isinstance(path, Path) else path


def _title(doc: Doc) -> str:
    return {"invoice": "Invoice", "packing_slip": "Packing slip", "credit_note": "Credit note"}[
        doc.doc_type
    ]


def _details(doc: Doc) -> list[tuple[str, str]]:
    s = doc.supplier
    number_label = {
        "invoice": "Invoice no.",
        "packing_slip": "Slip no.",
        "credit_note": "Credit note no.",
    }[doc.doc_type]
    rows = []
    if doc.invoice_number:
        rows.append((number_label, doc.invoice_number))
    rows.append(
        (
            "Date" if doc.doc_type != "packing_slip" else "Ship date",
            fmt_date(doc.invoice_date, s.date_style),
        )
    )
    if doc.due_date:
        rows.append(("Due date", fmt_date(doc.due_date, s.date_style)))
    if doc.po_number:
        rows.append(("PO number" if doc.doc_type != "packing_slip" else "Order ref", doc.po_number))
    if doc.reference:
        rows.append(("Credits invoice", doc.reference))
    if doc.currency and doc.doc_type != "packing_slip":
        rows.append(("Currency", doc.currency))
    return rows


def _line_rows(
    doc: Doc, with_sku: bool, with_rate: bool, symbol: bool = False, parens: bool = False
) -> tuple[list[str], list[list]]:
    priced = doc.doc_type != "packing_slip"
    head = (["SKU"] if with_sku else []) + ["Description", "Qty"]
    if priced:
        head += ["Unit price"] + (["Tax"] if with_rate else []) + ["Amount"]
    rows = []
    for line in doc.lines:
        row = ([line.sku or ""] if with_sku else []) + [line.description, fmt_qty(line.quantity)]
        if priced:
            row += [fmt_money(line.unit_price, doc, symbol)]
            if with_rate:
                row += [fmt_rate(line.tax_rate)]
            row += [fmt_money(line.line_total, doc, symbol, parens)]
        rows.append(row)
    if not with_sku:
        for line, row in zip(doc.lines, rows, strict=True):
            if line.sku:
                row[0] = f"{line.description} ({line.sku})"
    return head, rows


def _totals(doc: Doc, symbol: bool = True, parens: bool = False) -> list[tuple[str, str]]:
    if doc.doc_type == "packing_slip":
        units = sum(line.quantity for line in doc.lines)
        return [("Total units", fmt_qty(units))]
    style = currency_style(doc.currency or "USD")
    rate = f" ({fmt_rate(doc.tax_rate)})" if doc.tax_rate is not None else ""
    rows = [("Subtotal", fmt_money(doc.subtotal, doc, symbol, parens))]
    if doc.discount:
        rows.append(("Discount", fmt_money(-doc.discount, doc, symbol, parens)))
    if doc.tax is not None:
        rows.append((f"{style.tax_label}{rate}", fmt_money(doc.tax, doc, symbol, parens)))
    if doc.shipping:
        rows.append(("Shipping", fmt_money(doc.shipping, doc, symbol, parens)))
    label = "Total credit" if doc.doc_type == "credit_note" else "Total due"
    rows.append((label, fmt_money(doc.total, doc, symbol, parens)))
    return rows


def _supplier_block(doc: Doc) -> str:
    s = doc.supplier
    lines = [s.name, *s.address]
    if doc.tax_id:
        lines.append(f"{doc.tax_id_label} {doc.tax_id}")
    return "\n".join(lines)


def _buyer_block(label: str = "Bill to") -> str:
    return "\n".join([label, BUYER[0], *BUYER[1]])


def _stamp(c: canvas.Canvas, doc: Doc, pagesize) -> None:
    if doc.stamp:
        c.saveState()
        c.setFont("Helvetica-Bold", 60)
        c.setFillColor(colors.Color(0.75, 0.2, 0.2, alpha=0.25))
        c.translate(pagesize[0] / 2, pagesize[1] / 2)
        c.rotate(30)
        c.drawCentredString(0, 0, doc.stamp)
        c.restoreState()


def _page_number(c: canvas.Canvas, doc_tpl, text_doc: Doc, font: str = "Helvetica") -> None:
    c.saveState()
    c.setFont(font, 7)
    c.setFillColor(GREY)
    c.drawRightString(
        doc_tpl.pagesize[0] - 15 * mm, 10 * mm, f"{text_doc.supplier.name} - page {doc_tpl.page}"
    )
    c.restoreState()
    _stamp(c, text_doc, doc_tpl.pagesize)


def _build(
    doc: Doc,
    path: Path,
    story: list,
    pagesize=A4,
    on_first=None,
    font: str = "Helvetica",
    margins: float = 15,
) -> None:
    tpl = SimpleDocTemplate(
        _target(path),
        pagesize=pagesize,
        leftMargin=margins * mm,
        rightMargin=margins * mm,
        topMargin=margins * mm,
        bottomMargin=18 * mm,
        title=f"{_title(doc)} {doc.invoice_number}",
        author=doc.supplier.name,
        creator="ibis demo generator",
    )

    def first(c, d):
        if on_first:
            on_first(c, d)
        _page_number(c, d, doc, font)

    tpl.build(story, onFirstPage=first, onLaterPages=lambda c, d: _page_number(c, d, doc, font))


def _kv_table(
    rows: list[tuple[str, str]],
    widths=(32 * mm, 45 * mm),
    size=9,
    bold_last=False,
    font="Helvetica",
) -> Table:
    data = [
        [_p(k, size, font=font, color=GREY), _p(v, size, font=font, align=TA_RIGHT)]
        for k, v in rows
    ]
    if bold_last and data:
        k, v = rows[-1]
        data[-1] = [
            _p(k, size + 1, bold=True, font=font),
            _p(v, size + 1, bold=True, font=font, align=TA_RIGHT),
        ]
    t = Table(data, colWidths=widths, hAlign="RIGHT")
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return t


def _items_table(
    head, rows, widths, size=8.5, font="Helvetica", zebra=False, grid=False, header_bg=None
) -> LongTable:
    right_from = 1 if len(head) < 3 else head.index("Qty")
    data = [
        [
            _p(
                h,
                size,
                bold=True,
                font=font,
                color=colors.white if header_bg else INK,
                align=TA_RIGHT if i >= right_from else 0,
            )
            for i, h in enumerate(head)
        ]
    ]
    for row in rows:
        data.append(
            [
                _p(str(v), size, font=font, align=TA_RIGHT if i >= right_from else 0)
                for i, v in enumerate(row)
            ]
        )
    t = LongTable(data, colWidths=widths, repeatRows=1)
    cmds = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK)]
    if header_bg:
        cmds.append(("BACKGROUND", (0, 0), (-1, 0), header_bg))
    if grid:
        cmds.append(("GRID", (0, 0), (-1, -1), 0.4, RULE))
    else:
        cmds.append(("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE))
    if zebra:
        cmds += [
            ("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F2F4F2"))
            for r in range(2, len(data), 2)
        ]
    t.setStyle(TableStyle(cmds))
    return t


def _widths(head: list[str], total: float, sku: float = 22, num: float = 24) -> list[float]:
    fixed = {
        "SKU": sku,
        "Item": sku,
        "Item no.": sku,
        "Qty": 14,
        "Unit price": num,
        "Rate": num,
        "Tax": 14,
        "Amount": num,
    }
    known = sum(fixed.get(h, 0) for h in head)
    return [fixed.get(h, total - known) * mm for h in head]


def layout_classic(doc: Doc, path: Path) -> None:
    head, rows = _line_rows(doc, with_sku=False, with_rate=False)
    top = Table(
        [[_p(_supplier_block(doc), 9), _p(_title(doc).upper(), 22, bold=True, align=TA_RIGHT)]],
        colWidths=[110 * mm, 70 * mm],
    )
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    meta = Table([[_p(_buyer_block(), 9), _kv_table(_details(doc))]], colWidths=[100 * mm, 80 * mm])
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story = [
        top,
        Spacer(1, 10 * mm),
        meta,
        Spacer(1, 8 * mm),
        _items_table(head, rows, _widths(head, 180), grid=True),
        Spacer(1, 5 * mm),
        _kv_table(_totals(doc), widths=(40 * mm, 35 * mm), bold_last=True),
    ]
    if doc.doc_type == "invoice":
        story += [
            Spacer(1, 10 * mm),
            _p("Payment by bank transfer. Thank you for your order.", 8, color=GREY),
        ]
    _build(doc, path, story)


def layout_modern(doc: Doc, path: Path) -> None:
    band = colors.HexColor("#2F4858")

    def header(c, d):
        c.saveState()
        c.setFillColor(band)
        c.rect(0, d.pagesize[1] - 32 * mm, d.pagesize[0], 32 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(15 * mm, d.pagesize[1] - 17 * mm, doc.supplier.name)
        c.setFont("Helvetica", 9)
        c.drawString(15 * mm, d.pagesize[1] - 24 * mm, ", ".join(doc.supplier.address))
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(d.pagesize[0] - 15 * mm, d.pagesize[1] - 17 * mm, _title(doc))
        c.restoreState()

    head, rows = _line_rows(doc, with_sku=True, with_rate=False, symbol=True)
    tax_line = f"{doc.tax_id_label}: {doc.tax_id}" if doc.tax_id else ""
    meta = Table(
        [
            [
                _p(_buyer_block("Billed to"), 9),
                _kv_table(_details(doc), widths=(30 * mm, 40 * mm), size=8.5),
            ]
        ],
        colWidths=[110 * mm, 70 * mm],
    )
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story = [
        Spacer(1, 22 * mm),
        meta,
        Spacer(1, 8 * mm),
        _items_table(head, rows, _widths(head, 180, num=26), zebra=True, header_bg=band),
        Spacer(1, 4 * mm),
        _kv_table(_totals(doc), widths=(40 * mm, 35 * mm), bold_last=True),
        Spacer(1, 8 * mm),
        _p(tax_line, 8, color=GREY),
    ]
    _build(doc, path, story, on_first=header)


def layout_us_sales(doc: Doc, path: Path) -> None:
    head, rows = _line_rows(doc, with_sku=True, with_rate=False, symbol=False)
    head = ["Item" if h == "SKU" else "Rate" if h == "Unit price" else h for h in head]
    blocks = Table(
        [
            [
                _p(_buyer_block("BILL TO"), 8.5),
                _p(_buyer_block("SHIP TO"), 8.5),
                _kv_table(_details(doc), widths=(26 * mm, 30 * mm), size=8.5),
            ]
        ],
        colWidths=[58 * mm, 58 * mm, 70 * mm],
    )
    blocks.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (1, 0), 0.5, RULE)])
    )
    story = [
        _p(doc.supplier.name, 14, bold=True),
        _p("\n".join(doc.supplier.address), 8.5),
        Spacer(1, 3 * mm),
        _p(_title(doc).upper(), 18, bold=True, color=colors.HexColor("#5A3E85")),
        Spacer(1, 4 * mm),
        blocks,
        Spacer(1, 6 * mm),
        _items_table(
            head, rows, _widths(head, 186, sku=24, num=24), header_bg=colors.HexColor("#5A3E85")
        ),
        Spacer(1, 4 * mm),
        _kv_table(_totals(doc, symbol=True), widths=(40 * mm, 35 * mm), bold_last=True),
    ]
    if doc.doc_type == "invoice":
        story += [
            Spacer(1, 6 * mm),
            _p(f"Terms: Net 30. Make checks payable to {doc.supplier.name}.", 8, color=GREY),
        ]
    _build(doc, path, story, pagesize=LETTER, margins=13)


def layout_compact(doc: Doc, path: Path) -> None:
    head, rows = _line_rows(doc, with_sku=True, with_rate=doc.per_line_tax, symbol=False)
    details = _details(doc)
    grid = [[_p(k, 7, color=GREY) for k, _ in details], [_p(v, 8, bold=True) for _, v in details]]
    info = Table(grid, colWidths=[180 * mm / len(details)] * len(details))
    info.setStyle(
        TableStyle(
            [("BOX", (0, 0), (-1, -1), 0.5, INK), ("INNERGRID", (0, 0), (-1, -1), 0.25, RULE)]
        )
    )
    top = Table(
        [
            [
                _p(_supplier_block(doc), 7.5),
                _p(_buyer_block("Customer"), 7.5),
                _p(_title(doc), 15, bold=True, align=TA_RIGHT),
            ]
        ],
        colWidths=[70 * mm, 60 * mm, 50 * mm],
    )
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    totals = _totals(doc)
    story = [
        top,
        Spacer(1, 4 * mm),
        info,
        Spacer(1, 4 * mm),
        _items_table(head, rows, _widths(head, 180, sku=20, num=22), size=7.5, grid=True),
        Spacer(1, 3 * mm),
        _kv_table(totals, widths=(38 * mm, 30 * mm), size=8, bold_last=True),
    ]
    _build(doc, path, story, margins=12)


def layout_plain_text(doc: Doc, path: Path) -> None:
    """A system printout: Courier, dashed rules, no currency symbols on the lines."""
    width = 86
    rule = "-" * width
    out = [doc.supplier.name.upper(), *doc.supplier.address]
    if doc.tax_id:
        out.append(f"{doc.tax_id_label} {doc.tax_id}")
    out += ["", f"{'*** ' + _title(doc).upper() + ' ***':^{width}}", ""]
    out += [f"{k + ':':<18}{v}" for k, v in _details(doc)]
    out += ["", "SOLD TO: " + BUYER[0], "         " + ", ".join(BUYER[1]), "", rule]
    priced = doc.doc_type != "packing_slip"
    if priced:
        out.append(f"{'CODE':<9}{'DESCRIPTION':<41}{'QTY':>6}{'PRICE':>14}{'AMOUNT':>16}")
    else:
        out.append(f"{'CODE':<9}{'DESCRIPTION':<41}{'QTY':>6}")
    out.append(rule)
    for line in doc.lines:
        desc = line.description[:39]
        row = f"{(line.sku or ''):<9}{desc:<41}{fmt_qty(line.quantity):>6}"
        if priced:
            row += (
                f"{fmt_money(line.unit_price, doc, symbol=False):>14}"
                f"{fmt_money(line.line_total, doc, symbol=False):>16}"
            )
        out.append(row)
    out.append(rule)
    for k, v in _totals(doc, symbol=priced):
        out.append(f"{k.upper():>62}{v:>24}" if priced else f"{k.upper():>50}{v:>6}")
    if priced:
        out += ["", f"All amounts in {doc.currency}."]

    lines_per_page = 72
    pages = [out[i : i + lines_per_page] for i in range(0, len(out), lines_per_page)]
    c = canvas.Canvas(_target(path), pagesize=A4, pageCompression=1)
    c.setTitle(f"{_title(doc)} {doc.invoice_number}")
    c.setAuthor(doc.supplier.name)
    for n, page in enumerate(pages, start=1):
        text = c.beginText(12 * mm, A4[1] - 15 * mm)
        text.setFont("Courier", 8)
        text.setLeading(10)
        for row in page:
            text.textLine(row)
        c.drawText(text)
        c.setFont("Courier", 7)
        c.drawRightString(A4[0] - 12 * mm, 10 * mm, f"PAGE {n} OF {len(pages)}")
        _stamp(c, doc, A4)
        c.showPage()
    c.save()


def layout_euro(doc: Doc, path: Path) -> None:
    head, rows = _line_rows(doc, with_sku=True, with_rate=False, symbol=False)
    head = ["Item no." if h == "SKU" else h for h in head]
    accent = colors.HexColor("#8C2F39")
    sender = f"{doc.supplier.name} · {' · '.join(doc.supplier.address)}"
    story = [
        _p(doc.supplier.name, 15, bold=True, color=accent),
        _p(sender, 7, color=GREY),
        Spacer(1, 8 * mm),
        _p(_buyer_block("Invoice address"), 9),
        Spacer(1, 8 * mm),
        _p(f"{_title(doc)}", 14, bold=True),
        _kv_table(_details(doc), widths=(35 * mm, 45 * mm), size=8.5),
        Spacer(1, 6 * mm),
        _items_table(head, rows, _widths(head, 180), header_bg=accent),
        Spacer(1, 4 * mm),
        _kv_table(_totals(doc), widths=(45 * mm, 35 * mm), bold_last=True),
        Spacer(1, 10 * mm),
    ]
    if doc.tax_id:
        story.append(
            _p(
                f"{doc.tax_id_label} {doc.tax_id} · Registered office: {doc.supplier.address[-1]}",
                7,
                color=GREY,
            )
        )
    _build(doc, path, story)


# Layouts that don't print the supplier's tax ID (US sales invoices often leave out the EIN).
NO_TAX_ID = frozenset({"us_sales"})

LAYOUTS = {
    "classic": layout_classic,
    "modern": layout_modern,
    "us_sales": layout_us_sales,
    "compact": layout_compact,
    "plain_text": layout_plain_text,
    "euro": layout_euro,
}


def render(doc: Doc, path: Path) -> None:
    LAYOUTS[doc.supplier.layout](doc, path)


def render_scan(doc: Doc, path: Path, seed: int) -> None:
    """Render, then turn the first page into a blurred, tilted, noisy greyscale 'scan'."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter
    from reportlab.lib.utils import ImageReader

    buf = io.BytesIO()
    render(doc, buf)  # type: ignore[arg-type]
    pdf = pdfium.PdfDocument(buf.getvalue())
    image = pdf[0].render(scale=110 / 72).to_pil().convert("L")
    pdf.close()
    image = image.rotate(0.8, resample=Image.BICUBIC, expand=False, fillcolor=255)
    image = image.filter(ImageFilter.GaussianBlur(1.2))
    rng = random.Random(seed)
    noise = Image.frombytes(
        "L", image.size, bytes(rng.randint(0, 40) for _ in range(image.width * image.height))
    )
    image = Image.blend(image, noise, 0.08)
    jpeg = io.BytesIO()
    image.save(jpeg, "JPEG", quality=55)
    jpeg.seek(0)
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawImage(ImageReader(jpeg), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()


# --------------------------------------------------------------------------------------------
# Answer key


def _s(value) -> str | None:
    return None if value is None else str(value)


def answer(doc: Doc) -> dict:
    priced = doc.doc_type != "packing_slip"
    return {
        "file": doc.file,
        "layout": doc.supplier.layout,
        "expected_status": doc.expected_status,
        "expected_reasons": doc.expected_reasons,
        "note": doc.note,
        "fields": {
            "doc_type": doc.doc_type,
            "supplier_name": doc.supplier.name,
            "supplier_tax_id": None if doc.supplier.layout in NO_TAX_ID else doc.tax_id,
            "invoice_number": doc.invoice_number,
            "invoice_date": doc.invoice_date.isoformat(),
            "due_date": doc.due_date.isoformat() if doc.due_date else None,
            "currency": doc.currency if priced else None,
            "po_number": doc.po_number,
            "subtotal": _s(doc.subtotal),
            "tax": _s(doc.tax),
            "discount": _s(doc.discount),
            "shipping": _s(doc.shipping),
            "total": _s(doc.total),
            "line_items": [
                {
                    "description": line.description,
                    "sku": line.sku,
                    "quantity": _s(line.quantity),
                    "unit_price": _s(line.unit_price),
                    "line_total": _s(line.line_total),
                    "tax_rate": _s(line.tax_rate),
                }
                for line in doc.lines
            ],
        },
    }


def generate(out_dir: Path, currencies: list[str], seed: int = DEFAULT_SEED) -> list[dict]:
    """Write the PDFs to out_dir/invoices and the key to out_dir/answers.json."""
    currencies = [c.strip().upper() for c in currencies if c.strip()]
    if not currencies:
        raise ValueError("give at least one currency")
    bad = [c for c in currencies if not is_iso_currency(c)]
    if bad:
        raise ValueError(f"not ISO 4217 codes: {', '.join(bad)}")

    docs, allowed = build_documents(currencies, seed)
    pdf_dir = out_dir / "invoices"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    for old in pdf_dir.glob("*.pdf"):
        old.unlink()

    answers = []
    for doc in docs:
        path = pdf_dir / doc.file
        if "hard_to_read" in doc.expected_reasons:
            render_scan(doc, path, seed)
        else:
            render(doc, path)
        answers.append(answer(doc))

    # A byte-for-byte copy of the first invoice, as if the same email attachment was saved twice.
    first = next(a for a in answers if a["fields"]["doc_type"] == "invoice")
    copy_name = f"{len(answers) + 1:03d}-inv-copy-of-{first['file'][:3]}.pdf"
    shutil.copyfile(pdf_dir / first["file"], pdf_dir / copy_name)
    answers.append(
        {
            **first,
            "file": copy_name,
            "expected_status": "review",
            "expected_reasons": ["duplicate_file"],
            "note": f"identical bytes to {first['file']}",
        }
    )

    key = {
        "generated_by": "demo/generate.py",
        "seed": seed,
        "allowed_currencies": allowed,
        "documents": answers,
    }
    (out_dir / "answers.json").write_text(
        json.dumps(key, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return answers


def contact_sheet(pdf_dir: Path, out: Path, answers: list[dict]) -> None:
    """One thumbnail per layout and document type, for checking the set looks realistic."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw

    picks, seen = [], set()
    for a in answers:
        key = (a["layout"], a["fields"]["doc_type"], tuple(a["expected_reasons"][:1]))
        if key[2] and key[2][0] not in ("hard_to_read", "duplicate"):
            continue
        if key not in seen:
            seen.add(key)
            picks.append(a)
    thumbs = []
    for a in picks:
        pdf = pdfium.PdfDocument(str(pdf_dir / a["file"]))
        img = pdf[0].render(scale=0.6).to_pil().convert("RGB")
        pdf.close()
        img.thumbnail((360, 510))
        tile = Image.new("RGB", (370, 540), "#F7F8F6")
        tile.paste(img, (5, 5))
        ImageDraw.Draw(tile).text((8, 520), a["file"], fill="#1C2321")
        thumbs.append(tile)
    cols = 5
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 370, rows * 540), "#F7F8F6")
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % cols) * 370, (i // cols) * 540))
    sheet.save(out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--currencies",
        default=",".join(DEFAULT_CURRENCIES),
        help="Comma-separated ISO codes (default: USD,GBP,EUR)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=DEMO_DIR)
    parser.add_argument(
        "--contact-sheet",
        action="store_true",
        help="Also write contact-sheet.png in the output folder",
    )
    args = parser.parse_args(argv)

    answers = generate(args.out, args.currencies.split(","), args.seed)
    broken = sum(1 for a in answers if a["expected_reasons"])
    print(
        f"Wrote {len(answers)} documents ({len(answers) - broken} clean, {broken} broken) "
        f"to {args.out / 'invoices'}"
    )
    if args.contact_sheet:
        contact_sheet(args.out / "invoices", args.out / "contact-sheet.png", answers)
        print(f"Contact sheet: {args.out / 'contact-sheet.png'}")


if __name__ == "__main__":
    main()
