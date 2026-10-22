"""What Ibis remembers between runs, in output/.ibis/ (never invoice contents or amounts).

- invoices.json: supplier + invoice number of every document processed, to catch a resend (E6).
- date_orders.json: each supplier's day/month order, learned from documents that settle it.

Which files were processed is kept by discovery (`discover.SEEN_FILE`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ibis_docs.dates import Order
from ibis_docs.models import ExtractedInvoice, supplier_key

FOLDER = Path(".ibis")
INVOICES = FOLDER / "invoices.json"
DATE_ORDERS = FOLDER / "date_orders.json"


def invoice_key(invoice: ExtractedInvoice) -> str | None:
    """Supplier + number, compared loosely: 'INV 0697' from 'Harbourline Ltd' == 'inv0697'."""
    supplier = supplier_key(invoice.supplier_name)
    if not supplier or not invoice.invoice_number:
        return None
    number = re.sub(r"\s+", "", invoice.invoice_number.casefold())
    return f"{supplier}|{number}"


@dataclass
class Memory:
    invoices: dict[str, str] = field(default_factory=dict)  # invoice_key -> file name
    date_orders: dict[str, Order] = field(default_factory=dict)  # supplier_key -> order

    @classmethod
    def load(cls, output_dir: Path) -> Memory:
        return cls(
            invoices=_read(output_dir / INVOICES),
            date_orders={k: Order(v) for k, v in _read(output_dir / DATE_ORDERS).items()},
        )

    def save(self, output_dir: Path) -> None:
        _write(output_dir / INVOICES, self.invoices)
        _write(output_dir / DATE_ORDERS, {k: v.value for k, v in self.date_orders.items()})


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
