"""Keeping a run: results, exports, moves and memory (results.py, run.py, files.py)."""

import csv
import json
import shutil
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ibis_docs import export
from ibis_docs import run as run_module
from ibis_docs.config import Config, GoogleSheet
from ibis_docs.dates import Order
from ibis_docs.discover import discover, load_seen
from ibis_docs.files import free_name
from ibis_docs.memory import Memory
from ibis_docs.models import Status
from ibis_docs.pipeline import process
from ibis_docs.results import Call, load_all
from ibis_docs.run import keep, plan_moves
from tests.fakes import FakeClaude, by_document, error_response, message
from tests.test_pipeline import GOOD, HAIKU, SONNET, TODAY

DEMO = Path(__file__).resolve().parent.parent / "demo" / "invoices"
A, B, C = (DEMO / n for n in ("001-inv-classic.pdf", "002-inv-modern.pdf", "003-inv-us_sales.pdf"))
NOW = datetime(2026, 10, 23, 9, 30)


@pytest.fixture
def project(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for p in (A, B, C):
        shutil.copy(p, inbox / p.name)
    shutil.copy(A, inbox / "045-copy.pdf")
    return Config.model_validate({"base_dir": tmp_path})


ANSWERS = {
    A: {HAIKU: message(GOOD)},
    B: {
        HAIKU: message({**GOOD, "invoice_number": "INV-2", "total": "£1.00"}),
        SONNET: message({**GOOD, "invoice_number": "INV-2", "total": "£1.00"}, model=SONNET),
    },
    C: {HAIKU: error_response(500, "busy"), SONNET: error_response(500, "busy")},
}


def outcome_for(config: Config, answers=None, memory=None):
    fake = FakeClaude(route=by_document(answers or ANSWERS))
    found = discover(config.input_path, 20, load_seen(config.output_path)).found
    return process(found, config, fake.client, today=TODAY, memory=memory)


def test_a_run_saves_results_writes_exports_and_moves_files(project):
    outcome = outcome_for(project)
    kept = keep(outcome, project, "20261023-093000", NOW)

    inbox, output = project.input_path, project.output_path
    assert sorted(p.name for p in inbox.glob("*.pdf")) == []
    assert sorted(p.name for p in (inbox / "processed").iterdir()) == [A.name]
    assert sorted(p.name for p in (inbox / "needs-review").iterdir()) == [
        B.name,
        C.name,
        "045-copy.pdf",
    ]
    assert kept.moved == {"processed": 1, "needs-review": 3}
    assert kept.outputs == ["invoices.csv", "line_items.csv", "ibis.xlsx"]
    assert kept.warnings == []
    for name in kept.outputs:
        assert (output / name).is_file()

    saved = {r.name: r for r in load_all(output)}
    assert set(saved) == {A.name, B.name, C.name, "045-copy.pdf"}
    assert saved[A.name].pdf_path == str(inbox / "processed" / A.name)
    assert saved[A.name].calls == [Call(HAIKU, 4000, 500, None)]
    assert saved[C.name].invoice.status == Status.FAILED
    assert [c.error for c in saved[C.name].calls] == [
        "the API returned an error (500): busy",
        "the API returned an error (500): busy",
    ]


def test_memory_after_a_run(project):
    keep(outcome_for(project), project, "r1", NOW)
    output = project.output_path
    seen = load_seen(output)
    # The failed document isn't remembered, so dropping it in again retries it.
    assert len(seen) == 2  # A (and its identical copy) and B
    memory = Memory.load(output)
    assert memory.invoices == {
        "harbourline packaging ltd|inv-2026-0697": A.name,
        "harbourline packaging ltd|inv-2": B.name,
    }


def test_learned_date_orders_are_remembered(project):
    answers = {
        A: {HAIKU: message({**GOOD, "invoice_date": "13/09/2026", "due_date": ""})},
        B: {
            HAIKU: message(
                {**GOOD, "invoice_number": "2", "invoice_date": "14/09/2026", "due_date": ""}
            )
        },
        C: {
            HAIKU: message(
                {**GOOD, "invoice_number": "3", "supplier_name": "Other", "due_date": ""}
            )
        },
    }
    keep(outcome_for(project, answers), project, "r1", NOW)
    assert Memory.load(project.output_path).date_orders == {
        "harbourline packaging ltd": Order.DAY_FIRST
    }


def test_running_again_processes_nothing_new(project):
    keep(outcome_for(project), project, "r1", NOW)
    # The same files dropped in again (renamed) are skipped as already processed.
    shutil.copy(A, project.input_path / "again.pdf")
    found = discover(project.input_path, 20, load_seen(project.output_path))
    assert found.found == []
    assert [s.reason for s in found.skipped] == [
        "already processed in an earlier run (same contents)"
    ]


def test_exports_are_cumulative_across_runs(project):
    keep(outcome_for(project), project, "r1", NOW)
    second = {
        A: {HAIKU: message({**GOOD, "invoice_number": "INV-NEW"})},
    }
    inbox = project.input_path
    shutil.copy(A, inbox / "new.pdf")
    # Different bytes are needed for a new document: append a harmless PDF comment.
    (inbox / "new.pdf").write_bytes(A.read_bytes() + b"\n% another copy\n")
    route = by_document({**second, inbox / "new.pdf": second[A]})
    fake = FakeClaude(route=route)
    found = discover(inbox, 20, load_seen(project.output_path)).found
    outcome = process(
        found, project, fake.client, today=TODAY, memory=Memory.load(project.output_path)
    )
    keep(outcome, project, "r2", NOW + timedelta(hours=1))

    with (project.output_path / "invoices.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # Oldest run first, then by file name.
    assert [r["File"] for r in rows] == [A.name, B.name, C.name, "045-copy.pdf", "new.pdf"]
    assert rows[-1]["Invoice number"] == "INV-NEW"


def test_a_resend_in_a_later_run_is_a_duplicate(project):
    keep(outcome_for(project), project, "r1", NOW)
    inbox = project.input_path
    (inbox / "resend.pdf").write_bytes(A.read_bytes() + b"\n% COPY\n")
    fake = FakeClaude(route=by_document({inbox / "resend.pdf": {HAIKU: message(GOOD)}}))
    found = discover(inbox, 20, load_seen(project.output_path)).found
    outcome = process(
        found, project, fake.client, today=TODAY, memory=Memory.load(project.output_path)
    )
    (doc,) = outcome.documents
    assert doc.invoice.reasons[0].message == (
        f"same supplier and invoice number (INV-2026-0697) as {A.name}"
    )


def test_move_files_off_leaves_the_inbox_alone(project):
    config = project.model_copy(update={"move_files": False})
    kept = keep(outcome_for(config), config, "r1", NOW)
    assert kept.moved == {}
    assert len(list(config.input_path.glob("*.pdf"))) == 4
    saved = {r.name: r for r in load_all(config.output_path)}
    assert saved[A.name].pdf_path == str(config.input_path / A.name)


def test_only_the_outputs_switched_on_are_written(project):
    outputs = project.outputs.model_copy(update={"csv": False})
    config = project.model_copy(update={"outputs": outputs})
    kept = keep(outcome_for(config), config, "r1", NOW)
    assert kept.outputs == ["ibis.xlsx"]
    assert not (config.output_path / "invoices.csv").exists()


def test_names_already_taken_get_a_suffix(project):
    taken = project.input_path / "processed"
    taken.mkdir()
    (taken / A.name).write_bytes(b"an older file")
    keep(outcome_for(project), project, "r1", NOW)
    assert (taken / A.name).read_bytes() == b"an older file"
    assert (taken / "001-inv-classic-2.pdf").read_bytes() == A.read_bytes()


def test_free_name_avoids_names_planned_in_the_same_run(tmp_path):
    (tmp_path / "a.pdf").touch()
    assert free_name(tmp_path, "a.pdf") == tmp_path / "a-2.pdf"
    assert free_name(tmp_path, "a.pdf", {tmp_path / "a-2.pdf"}) == tmp_path / "a-3.pdf"
    assert free_name(tmp_path, "b.pdf") == tmp_path / "b.pdf"


def test_plan_moves_never_gives_two_files_one_name(project):
    outcome = outcome_for(project)
    targets = list(plan_moves(outcome, project.input_path).values())
    assert len(targets) == len(set(targets))


def test_nothing_moves_if_the_exports_fail(project, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(export, "write_excel", broken)
    with pytest.raises(OSError):
        keep(outcome_for(project), project, "r1", NOW)
    assert len(list(project.input_path.glob("*.pdf"))) == 4


def with_sheet(config: Config) -> Config:
    sheet = GoogleSheet(enabled=True, spreadsheet_id="KEY")
    return config.model_copy(
        update={"outputs": config.outputs.model_copy(update={"google_sheet": sheet})}
    )


def test_google_sheet_without_credentials_is_a_warning(project, monkeypatch):
    monkeypatch.delenv(run_module.GOOGLE_CREDENTIALS_ENV, raising=False)
    config = with_sheet(project)
    kept = keep(outcome_for(config), config, "r1", NOW)
    assert kept.warnings == [
        "Google Sheet not updated: set GOOGLE_SERVICE_ACCOUNT_JSON in .env to the service "
        "account's JSON key file"
    ]
    assert kept.moved  # the other outputs were written, so files still move


def test_google_sheet_errors_are_a_warning(project, monkeypatch):
    monkeypatch.setenv(run_module.GOOGLE_CREDENTIALS_ENV, "/nowhere/sa.json")

    def fail(*_a, **_k):
        raise PermissionError("the sheet isn't shared with the service account")

    monkeypatch.setattr(export, "write_google_sheet", fail)
    config = with_sheet(project)
    kept = keep(outcome_for(config), config, "r1", NOW)
    assert kept.warnings == [
        "Google Sheet not updated: PermissionError: the sheet isn't shared with the service account"
    ]


def test_google_sheet_written(project, monkeypatch):
    monkeypatch.setenv(run_module.GOOGLE_CREDENTIALS_ENV, "/keys/sa.json")
    calls = {}

    def write(spreadsheet, sheets, credentials):
        calls.update(
            spreadsheet=spreadsheet, sheets=[s.name for s in sheets], credentials=credentials
        )
        return "https://docs.google.com/spreadsheets/d/KEY"

    monkeypatch.setattr(export, "write_google_sheet", write)
    config = with_sheet(project)
    kept = keep(outcome_for(config), config, "r1", NOW)
    assert kept.outputs[-1] == "https://docs.google.com/spreadsheets/d/KEY"
    assert calls == {
        "spreadsheet": "KEY",
        "sheets": ["Invoices", "Line items", "Needs review"],
        "credentials": Path("/keys/sa.json"),
    }


def test_results_round_trip_exactly(project):
    keep(outcome_for(project), project, "r1", NOW)
    (result,) = [r for r in load_all(project.output_path) if r.name == A.name]
    assert result.invoice.total == Decimal("1777.20")
    assert result.processed_at == NOW and result.run_id == "r1"
    files = sorted(p.name for p in (project.output_path / "results").iterdir())
    assert len(files) == 4  # 001 and its copy have the same hash but different names
    data = json.loads((project.output_path / "results" / files[0]).read_text())
    assert set(data) == {"invoice", "processed_at", "run_id", "calls", "pdf_path", "review"}
    assert data["review"] is None
