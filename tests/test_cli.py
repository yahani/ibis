import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ibis_docs import __version__
from ibis_docs.cli import app
from ibis_docs.discover import discover as discover_files
from ibis_docs.discover import record_seen
from ibis_docs.extract import ApiSetupError
from tests.conftest import EXAMPLE_CONFIG
from tests.fakes import FakeClaude, error_response, message
from tests.test_extract import REPLY

DEMO = Path(__file__).resolve().parent.parent / "demo" / "invoices"

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    assert "check-config" in result.output


def test_check_config_ok():
    result = runner.invoke(app, ["check-config", "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0, result.output
    assert "is valid" in result.output
    assert "USD, GBP, EUR" in result.output
    assert "escalate: claude-sonnet-5-5" in result.output


def test_check_config_shows_symbol_remaps_and_sheet(example_data, write_config):
    example_data["validation"]["allowed_currencies"] = ["AUD", "GBP"]
    example_data["validation"]["currency_symbols"] = {"$": "AUD"}
    example_data["model"]["escalate_to"] = None
    example_data["outputs"] = {
        "csv": False,
        "excel": False,
        "google_sheet": {"enabled": True, "spreadsheet_id": "x"},
    }
    result = runner.invoke(app, ["check-config", "-c", str(write_config(example_data))])
    assert result.exit_code == 0, result.output
    assert "$ → AUD" in result.output
    assert "escalate: off" in result.output
    assert "(Google Sheet)" in result.output


def test_check_config_bad(write_config):
    result = runner.invoke(app, ["check-config", "-c", str(write_config({"modle": 1}))])
    assert result.exit_code == 1
    assert "modle: unknown field" in result.output


@pytest.mark.parametrize(
    ("args", "session"),
    [
        (["run"], 3),
        (["cost", "--estimate", "inbox"], 4),
    ],
)
def test_stub_commands_say_not_built(args, session):
    result = runner.invoke(app, [*args, "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 2
    assert f"session {session}" in result.output


def test_stub_commands_still_check_config(tmp_path):
    result = runner.invoke(app, ["run", "-c", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "not found" in result.output


# --- run --dry-run (E2) ---------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, example_data, write_config):
    """A config file next to an inbox with three PDFs and a stray text file."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("001-inv-classic.pdf", "006-inv-euro.pdf"):
        shutil.copy(DEMO / name, inbox / name)
    shutil.copy(DEMO / "001-inv-classic.pdf", inbox / "resend.pdf")
    (inbox / "notes.txt").write_text("not an invoice")
    return write_config(example_data)


def test_dry_run_lists_what_would_be_processed_and_skipped(project):
    result = runner.invoke(app, ["run", "--dry-run", "-c", str(project)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())  # column widths aside
    assert "nothing is sent to Claude, written or moved" in out
    assert "3 PDF(s) to process" in out
    assert "006-inv-euro.pdf 2 pages" in out
    assert "resend.pdf 1 page (same file as 001-inv-classic.pdf)" in out
    assert "1 skipped:" in out
    assert "notes.txt not a PDF" in out


def test_dry_run_skips_files_from_earlier_runs(project):
    inbox, output = project.parent / "inbox", project.parent / "output"
    record_seen(output, discover_files(inbox, 20).found[:1])
    result = runner.invoke(app, ["run", "--dry-run", "-c", str(project)])
    assert "1 PDF(s) to process" in result.output
    assert result.output.count("already processed in an earlier run") == 2


def test_dry_run_writes_nothing(project):
    before = sorted(p.relative_to(project.parent) for p in project.parent.rglob("*"))
    runner.invoke(app, ["run", "--dry-run", "-c", str(project)])
    after = sorted(p.relative_to(project.parent) for p in project.parent.rglob("*"))
    assert before == after


def test_dry_run_without_an_input_folder(write_config, example_data):
    result = runner.invoke(app, ["run", "--dry-run", "-c", str(write_config(example_data))])
    assert result.exit_code == 1
    assert "input folder not found" in result.output
    assert "change input_dir" in result.output


# --- show (E3) ------------------------------------------------------------------------------


@pytest.fixture
def fake_claude(monkeypatch):
    def install(*responses):
        fake = FakeClaude(*responses)
        monkeypatch.setattr("ibis_docs.cli.make_client", lambda: fake.client)
        return fake

    return install


def test_show_prints_the_fields(fake_claude):
    fake = fake_claude(message(REPLY, input_tokens=5210, output_tokens=812))
    pdf = DEMO / "001-inv-classic.pdf"
    result = runner.invoke(app, ["show", str(pdf), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "001-inv-classic.pdf  (claude-haiku-5-5, 5,210 input / 812 output tokens)" in out
    assert "supplier:  Harbourline Packaging Ltd (tax ID GB861142741)" in out
    assert "date:      2026-08-18" in out
    assert "due:       —" in out
    assert "total:     1812.86" in out
    assert "lines (2):" in out
    assert "20        1.70       34.00  Double-wall shipping box 40x30x20 [BX-964]" in out
    assert "validation comes in session 3" in out
    assert len(fake.requests) == 1


def test_show_lists_values_it_could_not_read(fake_claude):
    fake_claude(message({**REPLY, "total": "illegible"}))
    pdf = DEMO / "001-inv-classic.pdf"
    result = runner.invoke(app, ["show", str(pdf), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0
    assert "! total 'illegible' isn't a readable amount" in result.output


def test_show_reports_a_failed_extraction(fake_claude):
    fake_claude(error_response(500, "overloaded"))
    pdf = DEMO / "001-inv-classic.pdf"
    result = runner.invoke(app, ["show", str(pdf), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 1
    assert "✗ the API returned an error (500): overloaded" in result.output


def test_show_without_credentials(monkeypatch):
    def no_client():
        raise ApiSetupError("no usable API credentials: set ANTHROPIC_API_KEY in .env")

    monkeypatch.setattr("ibis_docs.cli.make_client", no_client)
    pdf = DEMO / "001-inv-classic.pdf"
    result = runner.invoke(app, ["show", str(pdf), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 1
    assert "set ANTHROPIC_API_KEY in .env" in result.output


def test_show_checks_the_file_before_calling_claude(tmp_path, fake_claude):
    fake = fake_claude()
    text = tmp_path / "notes.pdf"
    text.write_text("hello")
    result = runner.invoke(app, ["show", str(text), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 1
    assert "notes.pdf: not a PDF" in result.output

    result = runner.invoke(app, ["show", str(tmp_path / "gone.pdf"), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 1
    assert "file not found" in result.output
    assert fake.requests == []
