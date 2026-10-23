"""Every processed document's final result, kept in output/results/ so exports are cumulative
(one ledger across runs) and the review page (E10) can correct a result and re-export.

One JSON file per document, named after the file and its hash, so two documents with the same
name in different runs never overwrite each other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ibis_docs.models import Invoice

FOLDER = Path("results")


@dataclass
class Call:
    """One model call: who answered, what it cost in tokens, and whether it failed."""

    model: str
    input_tokens: int
    output_tokens: int
    error: str | None = None


@dataclass
class Change:
    field: str
    before: str
    after: str


@dataclass
class Review:
    """What a person decided on the review page (E10): the audit trail for a result."""

    action: str  # "approved" or "discarded"
    at: datetime
    changes: list[Change] = field(default_factory=list)
    note: str = ""
    overridden: list[str] = field(default_factory=list)  # reasons still open when approved

    def to_json(self) -> dict:
        return {
            "action": self.action,
            "at": self.at.isoformat(timespec="seconds"),
            "changes": [vars(c) for c in self.changes],
            "note": self.note,
            "overridden": self.overridden,
        }

    @classmethod
    def from_json(cls, data: dict) -> Review:
        return cls(
            action=data["action"],
            at=datetime.fromisoformat(data["at"]),
            changes=[Change(**c) for c in data.get("changes", [])],
            note=data.get("note", ""),
            overridden=data.get("overridden", []),
        )


@dataclass
class Result:
    invoice: Invoice
    processed_at: datetime
    run_id: str
    calls: list[Call] = field(default_factory=list)
    pdf_path: str | None = None  # where the PDF is now (after moving), for the review page
    review: Review | None = None

    @property
    def discarded(self) -> bool:
        return self.review is not None and self.review.action == "discarded"

    @property
    def name(self) -> str:
        return self.invoice.source_file.name

    def filename(self) -> str:
        return f"{Path(self.name).stem}-{self.invoice.source_file.sha256[:12]}.json"

    def to_json(self) -> dict:
        return {
            "invoice": self.invoice.model_dump(mode="json"),
            "processed_at": self.processed_at.isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "calls": [vars(c) for c in self.calls],
            "pdf_path": self.pdf_path,
            "review": self.review.to_json() if self.review else None,
        }

    @classmethod
    def from_json(cls, data: dict) -> Result:
        return cls(
            invoice=Invoice.model_validate(data["invoice"]),
            processed_at=datetime.fromisoformat(data["processed_at"]),
            run_id=data["run_id"],
            calls=[Call(**c) for c in data.get("calls", [])],
            pdf_path=data.get("pdf_path"),
            review=Review.from_json(data["review"]) if data.get("review") else None,
        )


def save(output_dir: Path, results: list[Result]) -> None:
    folder = output_dir / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    for result in results:
        text = json.dumps(result.to_json(), indent=2, ensure_ascii=False) + "\n"
        (folder / result.filename()).write_text(text, encoding="utf-8")


def load_all(output_dir: Path) -> list[Result]:
    """Every saved result, oldest run first, then by file name."""
    folder = output_dir / FOLDER
    if not folder.is_dir():
        return []
    results = [
        Result.from_json(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(folder.glob("*.json"))
    ]
    return sorted(results, key=lambda r: (r.processed_at, r.name.casefold()))
