import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ibis_docs.config import Config
from ibis_docs.discover import Found, Skipped, sha256_of
from ibis_docs.report import (
    OUTPUT_PER_SCANNED_PAGE,
    RunReport,
    cost_of,
    dollars,
    estimate,
    expected_output,
)
from tests.fakes import FakeClaude, document_of

DEMO = Path(__file__).resolve().parent.parent / "demo" / "invoices"
START = datetime(2026, 10, 23, 9, 30, 0)


def report(**kw) -> RunReport:
    base = {
        "run_id": "20261023-093000",
        "started": START,
        "finished": datetime(2026, 10, 23, 9, 31, 5),
        "dry_run": False,
        "counts": {"ok": 45, "review": 3, "failed": 0},
        "skipped": [("notes.txt", "not a PDF")],
        "tokens": {"claude-haiku-5-5": (200_000, 40_000), "claude-sonnet-5-5": (13_000, 1_500)},
    }
    return RunReport(**{**base, **kw})


def test_summary_in_the_brand_voice():
    assert report().summary() == "Ibis read 48 documents. 45 checked out. 3 need a look."
    one = report(counts={"ok": 0, "review": 1, "failed": 1})
    assert (
        one.summary() == "Ibis read 2 documents. 0 checked out. 1 needs a look. 1 couldn't be read."
    )


def test_cost():
    assert cost_of("claude-haiku-5-5", 1_000_000, 1_000_000) == Decimal("0.60")
    assert cost_of("claude-sonnet-5-5", 13_000, 1_500) == Decimal("0.041")
    assert cost_of("some-new-model", 1, 1) is None
    assert report().cost == Decimal("0.02") + Decimal("0.02") + Decimal("0.041")
    assert report(tokens={"some-new-model": (1, 1)}).cost is None
    assert dollars(Decimal("0.08123")) == "$0.0812" and dollars(None) == "unknown"


def test_report_lines():
    lines = report(moved={"processed": 45, "needs-review": 3}).lines()
    assert lines[0] == "Ibis read 48 documents. 45 checked out. 3 need a look."
    assert "  claude-haiku-5-5: 200,000 in / 40,000 out tokens, $0.0400" in lines
    assert "Estimated API cost $0.0810; took 65 s." in lines
    assert "Moved 45 PDF(s) to processed/." in lines and "Moved 3 PDF(s) to needs-review/." in lines


def test_saved_report_has_counts_not_contents(tmp_path):
    path = report().save(tmp_path)
    assert path == tmp_path / "reports" / "run-20261023-093000.json"
    data = json.loads(path.read_text())
    assert data["documents"] == 48 and data["counts"]["review"] == 3
    assert data["models"]["claude-sonnet-5-5"] == {
        "input_tokens": 13000,
        "output_tokens": 1500,
        "cost": "0.041",
    }
    assert data["skipped"] == [{"file": "notes.txt", "reason": "not a PDF"}]
    assert data["seconds"] == 65.0


def test_build_from_nothing():
    built = RunReport.build("r", START, None, [Skipped(Path("x.txt"), "not a PDF")], dry_run=True)
    assert built.documents == 0 and built.skipped == [("x.txt", "not a PDF")]


def test_expected_output_grows_with_the_text():
    one_page = expected_output(DEMO / "001-inv-classic.pdf")
    two_pages = expected_output(DEMO / "006-inv-euro.pdf")
    scan = expected_output(DEMO / "044-inv-euro.pdf")
    assert 500 < one_page < 1500 < two_pages
    assert scan == 300 + OUTPUT_PER_SCANNED_PAGE


def test_estimate_counts_input_exactly_and_skips_identical_copies():
    files = [
        Found(DEMO / "001-inv-classic.pdf", sha256_of(DEMO / "001-inv-classic.pdf"), 1),
        Found(DEMO / "006-inv-euro.pdf", sha256_of(DEMO / "006-inv-euro.pdf"), 2),
        Found(DEMO / "045-inv-copy-of-001.pdf", "x" * 64, 1, duplicate_of="001-inv-classic.pdf"),
    ]
    counted = {DEMO / "001-inv-classic.pdf": 4359, DEMO / "006-inv-euro.pdf": 7273}
    by_bytes = {p.read_bytes(): n for p, n in counted.items()}
    fake = FakeClaude(route=lambda body: {"input_tokens": by_bytes[document_of(body)]})
    result = estimate(files, Config(), fake.client)

    assert len(fake.requests) == 2
    assert "max_tokens" not in fake.requests[0]
    assert fake.requests[0]["output_config"]["format"]["type"] == "json_schema"
    haiku_in, haiku_out = result.tokens["claude-haiku-5-5"]
    assert haiku_in == 4359 + 7273
    assert haiku_out == expected_output(files[0].path) + expected_output(files[1].path)
    sonnet_in, sonnet_out = result.tokens["claude-sonnet-5-5"]
    assert sonnet_in == int(haiku_in * Decimal("0.10"))
    assert sonnet_out == int(haiku_out * Decimal("0.10") * Decimal("0.6"))
    assert (result.documents, result.pages) == (3, 4)
    assert result.cost > 0


def test_estimate_without_retries():
    path = DEMO / "001-inv-classic.pdf"
    fake = FakeClaude(route=lambda body: {"input_tokens": 4000})
    config = Config.model_validate({"model": {"escalate_to": None}})
    result = estimate([Found(path, sha256_of(path), 1)], config, fake.client)
    assert list(result.tokens) == ["claude-haiku-5-5"]
