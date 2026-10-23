"""The run report (E12) and the cost estimate (`ibis cost --estimate`).

A report holds file names, counts, models, tokens and cost, never invoice contents or amounts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import anthropic
from pypdf import PdfReader

from ibis_docs.config import Config
from ibis_docs.discover import Found, Skipped
from ibis_docs.extract import build_request
from ibis_docs.models import RawInvoice, Status
from ibis_docs.pipeline import Outcome

FOLDER = Path("reports")

# List prices per million tokens (input, output). Haiku 5.5's are for prompts up to 100k tokens,
# which a 20-page invoice stays under. https://platform.claude.com/docs/en/about-claude/pricing
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
}

# Output tokens, measured on the demo set's live runs: the model copies the page's text, so its
# reply grows with it. A scanned page has no text layer to measure.
OUTPUT_BASE = 300
OUTPUT_PER_CHAR = Decimal("1.5")  # per non-space character of the page's text layer
OUTPUT_PER_SCANNED_PAGE = 900
# Share of documents retried on the escalation model, and its output relative to the first read
# (it answers without long thinking). The demo set, with 10 broken documents in 47, retried 11%.
RETRY_SHARE = Decimal("0.10")
RETRY_OUTPUT_SHARE = Decimal("0.6")


def cost_of(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """List-price cost in dollars, or None for a model without a known price."""
    price = PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def dollars(amount: Decimal | None) -> str:
    if amount is None:
        return "unknown"
    return f"${amount.quantize(Decimal('0.0001'), ROUND_HALF_UP)}"


@dataclass
class RunReport:
    run_id: str
    started: datetime
    finished: datetime
    dry_run: bool
    counts: dict[str, int]  # ok / review / failed
    skipped: list[tuple[str, str]]  # (file, reason)
    tokens: dict[str, tuple[int, int]]  # model -> (input, output)
    moved: dict[str, int] = field(default_factory=dict)  # folder -> files moved
    outputs: list[str] = field(default_factory=list)  # files written

    @classmethod
    def build(
        cls,
        run_id: str,
        started: datetime,
        outcome: Outcome | None,
        skipped: list[Skipped],
        *,
        dry_run: bool,
        finished: datetime | None = None,
    ) -> RunReport:
        counts = {s.value: outcome.count(s) if outcome else 0 for s in Status}
        return cls(
            run_id=run_id,
            started=started,
            finished=finished or datetime.now(),
            dry_run=dry_run,
            counts=counts,
            skipped=[(s.name, s.reason) for s in skipped],
            tokens=outcome.tokens() if outcome else {},
        )

    @property
    def documents(self) -> int:
        return sum(self.counts.values())

    @property
    def cost(self) -> Decimal | None:
        costs = [cost_of(m, i, o) for m, (i, o) in self.tokens.items()]
        return None if any(c is None for c in costs) else sum(costs, Decimal(0))

    @property
    def seconds(self) -> float:
        return (self.finished - self.started).total_seconds()

    def summary(self) -> str:
        """In the brand's voice: "Ibis read 48 documents. 45 checked out. 3 need a look." """
        n, ok = self.documents, self.counts["ok"]
        review, failed = self.counts["review"], self.counts["failed"]
        text = f"Ibis read {n} document{'s' if n != 1 else ''}. {ok} checked out."
        if review:
            text += f" {review} need{'s' if review == 1 else ''} a look."
        if failed:
            text += f" {failed} couldn't be read."
        return text

    def lines(self) -> list[str]:
        out = [self.summary()]
        if self.skipped:
            out.append(f"Skipped {len(self.skipped)} file(s) (see above).")
        for model, (i, o) in self.tokens.items():
            out.append(f"  {model}: {i:,} in / {o:,} out tokens, {dollars(cost_of(model, i, o))}")
        out.append(f"Estimated API cost {dollars(self.cost)}; took {self.seconds:.0f} s.")
        for folder, count in self.moved.items():
            out.append(f"Moved {count} PDF(s) to {folder}/.")
        return out

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "started": self.started.isoformat(timespec="seconds"),
            "finished": self.finished.isoformat(timespec="seconds"),
            "seconds": round(self.seconds, 1),
            "dry_run": self.dry_run,
            "documents": self.documents,
            "counts": self.counts,
            "skipped": [{"file": f, "reason": r} for f, r in self.skipped],
            "models": {
                m: {"input_tokens": i, "output_tokens": o, "cost": str(cost_of(m, i, o))}
                for m, (i, o) in self.tokens.items()
            },
            "cost": str(self.cost),
            "moved": self.moved,
            "outputs": self.outputs,
        }

    def save(self, output_dir: Path) -> Path:
        path = output_dir / FOLDER / f"run-{self.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
        return path


# --- estimate -------------------------------------------------------------------------------


@dataclass
class Estimate:
    documents: int
    pages: int
    tokens: dict[str, tuple[int, int]]  # model -> (input, output), retries included

    @property
    def cost(self) -> Decimal | None:
        """With retries at the usual share."""
        costs = [cost_of(m, i, o) for m, (i, o) in self.tokens.items()]
        return None if any(c is None for c in costs) else sum(costs, Decimal(0))

    @property
    def cost_without_retries(self) -> Decimal | None:
        """If every document passes its checks on the first read."""
        model, (i, o) = next(iter(self.tokens.items()))
        return cost_of(model, i, o)


def expected_output(path: Path) -> int:
    """Output tokens a first read of this PDF is likely to take (see OUTPUT_PER_CHAR)."""
    total = OUTPUT_BASE
    for page in PdfReader(path).pages:
        chars = sum(1 for ch in page.extract_text() or "" if not ch.isspace())
        total += int(chars * OUTPUT_PER_CHAR) if chars else OUTPUT_PER_SCANNED_PAGE
    return total


def estimate(files: list[Found], config: Config, client: anthropic.Anthropic) -> Estimate:
    """Input tokens counted exactly with the API's free token counter (the same request a run
    sends, schema included); output tokens and retries estimated from measured runs."""
    model = config.model.default
    input_tokens = output_tokens = 0
    for f in files:
        if f.duplicate_of:
            continue  # never sent
        request = build_request(f.path.read_bytes(), model, config)
        request.pop("max_tokens")
        counted = client.messages.count_tokens(**request, output_format=RawInvoice)
        input_tokens += counted.input_tokens
        output_tokens += expected_output(f.path)

    tokens = {model: (input_tokens, output_tokens)}
    retry = config.model.escalate_to
    if retry and input_tokens:
        tokens[retry] = (
            int(input_tokens * RETRY_SHARE),
            int(output_tokens * RETRY_SHARE * RETRY_OUTPUT_SHARE),
        )
    return Estimate(documents=len(files), pages=sum(f.pages for f in files), tokens=tokens)
