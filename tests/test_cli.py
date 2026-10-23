import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ibis_docs import __version__
from ibis_docs.cli import app
from ibis_docs.discover import discover as discover_files
from ibis_docs.discover import record_seen
from ibis_docs.extract import ApiSetupError
from tests.conftest import EXAMPLE_CONFIG
from tests.fakes import FakeClaude, by_document, error_response, message
from tests.test_pipeline import GOOD, HAIKU, SONNET, TODAY

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


def test_commands_check_the_config_first(tmp_path):
    result = runner.invoke(app, ["run", "-c", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "not found" in result.output


@pytest.fixture
def fake_claude(monkeypatch):
    """Install a fake Claude answering {pdf path: {model: response}}, and fix today's date."""
    monkeypatch.setattr("ibis_docs.cli.today", lambda: TODAY)

    def install(answers=None):
        fake = FakeClaude(route=by_document(answers or {}))
        monkeypatch.setattr("ibis_docs.cli.make_client", lambda: fake.client)
        return fake

    return install


# --- run --dry-run --------------------------------------------------------------------------


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


CLASSIC, EURO = DEMO / "001-inv-classic.pdf", DEMO / "006-inv-euro.pdf"


def test_dry_run_reads_checks_and_summarises(project, fake_claude):
    fake = fake_claude(
        {
            CLASSIC: {HAIKU: message(GOOD)},
            EURO: {
                HAIKU: message({**GOOD, "invoice_number": "RE-1", "total": "£1,877.20"}),
                SONNET: message(
                    {**GOOD, "invoice_number": "RE-1", "total": "£1,877.20"}, model=SONNET
                ),
            },
        }
    )
    result = runner.invoke(app, ["run", "--dry-run", "-c", str(project)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())  # column widths aside
    assert "documents are read and checked, but nothing is written or moved" in out
    assert "Reading 3 document(s)" in out
    assert "✓ 001-inv-classic.pdf ok" in out
    assert "! 006-inv-euro.pdf review (claude-sonnet-5-5)" in out
    assert "the total doesn't add up" in out
    assert "! resend.pdf review identical file to 001-inv-classic.pdf" in out
    assert "Skipped 1: - notes.txt not a PDF" in out
    assert "Ibis read 3 documents. 1 checked out. 2 need a look." in out
    assert "claude-haiku-5-5: 8,000 in / 1,000 out tokens, $0.0013" in out
    assert "claude-sonnet-5-5: 4,000 in / 500 out tokens, $0.0130" in out
    assert "Estimated API cost $0.0143" in out
    assert len(fake.requests) == 3  # the identical copy costs nothing


def test_dry_run_skips_files_from_earlier_runs(project, fake_claude):
    fake_claude({EURO: {HAIKU: message(GOOD)}})
    inbox, output = project.parent / "inbox", project.parent / "output"
    record_seen(output, discover_files(inbox, 20).found[:1])
    result = runner.invoke(app, ["run", "--dry-run", "-c", str(project)])
    assert "Reading 1 document(s)" in result.output
    assert result.output.count("already processed in an earlier run") == 2
    assert "Ibis read 1 document. 1 checked out." in result.output


def test_dry_run_writes_nothing(project, fake_claude):
    fake_claude({CLASSIC: {HAIKU: message(GOOD)}, EURO: {HAIKU: message(GOOD)}})
    before = sorted(p.relative_to(project.parent) for p in project.parent.rglob("*"))
    runner.invoke(app, ["run", "--dry-run", "-c", str(project)])
    after = sorted(p.relative_to(project.parent) for p in project.parent.rglob("*"))
    assert before == after


def test_dry_run_with_nothing_new(tmp_path, example_data, write_config, fake_claude):
    (tmp_path / "inbox").mkdir()
    result = runner.invoke(app, ["run", "--dry-run", "-c", str(write_config(example_data))])
    assert result.exit_code == 0
    assert "No new PDFs" in result.output


def test_dry_run_without_an_input_folder(write_config, example_data):
    result = runner.invoke(app, ["run", "--dry-run", "-c", str(write_config(example_data))])
    assert result.exit_code == 1
    assert "input folder not found" in result.output
    assert "change input_dir" in result.output


def test_dry_run_reports_failed_documents(project, fake_claude):
    broken = error_response(500, "overloaded")
    fake_claude({CLASSIC: {HAIKU: broken, SONNET: broken}, EURO: {HAIKU: message(GOOD)}})
    result = runner.invoke(app, ["run", "--dry-run", "-c", str(project)])
    assert "Ibis read 3 documents. 1 checked out. 1 needs a look. 1 couldn't be read." in (
        " ".join(result.output.split())
    )


def test_dry_run_without_credentials(project, monkeypatch):
    def no_client():
        raise ApiSetupError("no usable API credentials: set ANTHROPIC_API_KEY in .env")

    monkeypatch.setattr("ibis_docs.cli.make_client", no_client)
    result = runner.invoke(app, ["run", "--dry-run", "-c", str(project)])
    assert result.exit_code == 1
    assert "set ANTHROPIC_API_KEY in .env" in result.output


# --- show -----------------------------------------------------------------------------------


def test_show_prints_the_fields_and_checks(fake_claude):
    fake = fake_claude({CLASSIC: {HAIKU: message(GOOD, input_tokens=5210, output_tokens=812)}})
    result = runner.invoke(app, ["show", str(CLASSIC), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "001-inv-classic.pdf  (claude-haiku-5-5 5,210 in / 812 out)" in out
    assert "supplier:  Harbourline Packaging Ltd (tax ID GB861142741)" in out
    assert "date:      2026-08-18" in out
    assert "due:       2026-09-17" in out
    assert "total:     1777.20" in out
    assert "lines (2):" in out
    assert "20        1.70       34.00  Double-wall shipping box 40x30x20 [BX-964]" in out
    assert "✓ ok (final read: claude-haiku-5-5)" in out
    assert len(fake.requests) == 1


def test_show_lists_the_reasons(fake_claude):
    bad = {**GOOD, "total": "illegible"}
    fake_claude({CLASSIC: {HAIKU: message(bad), SONNET: message(bad, model=SONNET)}})
    result = runner.invoke(app, ["show", str(CLASSIC), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0
    assert "! review (final read: claude-sonnet-5-5)" in result.output
    assert "- total 'illegible' isn't a readable amount" in result.output


def test_show_reports_a_failed_extraction(fake_claude):
    fake_claude({CLASSIC: {HAIKU: error_response(500, "overloaded"), SONNET: error_response(529)}})
    result = runner.invoke(app, ["show", str(CLASSIC), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 1
    assert "(claude-haiku-5-5 failed, claude-sonnet-5-5 failed)" in result.output
    assert "✗ failed" in result.output
    assert "- claude-haiku-5-5: the API returned an error (500): overloaded" in result.output


def test_show_without_credentials(monkeypatch):
    def no_client():
        raise ApiSetupError("no usable API credentials: set ANTHROPIC_API_KEY in .env")

    monkeypatch.setattr("ibis_docs.cli.make_client", no_client)
    result = runner.invoke(app, ["show", str(CLASSIC), "-c", str(EXAMPLE_CONFIG)])
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


def test_today_is_the_real_date():
    from datetime import date

    from ibis_docs.cli import today

    assert today() == date.today()


# --- the full run and the cost estimate -----------------------------------------------------


def test_run_writes_the_sheets_moves_files_and_reports(project, fake_claude, monkeypatch):
    monkeypatch.setattr("ibis_docs.cli.now", lambda: datetime(2026, 10, 23, 9, 30))
    fake_claude(
        {
            CLASSIC: {HAIKU: message(GOOD)},
            EURO: {HAIKU: message({**GOOD, "invoice_number": "RE-1"})},
        }
    )
    result = runner.invoke(app, ["run", "-c", str(project)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "Ibis read 3 documents. 2 checked out. 1 needs a look." in out
    assert "Moved 2 PDF(s) to processed/." in out and "Moved 1 PDF(s) to needs-review/." in out
    assert "invoices.csv, line_items.csv, ibis.xlsx" in out
    output, inbox = project.parent / "output", project.parent / "inbox"
    assert (output / "ibis.xlsx").is_file()
    assert (output / "reports" / "run-20261023-093000.json").is_file()
    assert sorted(p.name for p in (inbox / "processed").iterdir()) == [
        "001-inv-classic.pdf",
        "006-inv-euro.pdf",
    ]
    assert (inbox / "notes.txt").exists()  # skipped files stay put

    # Running again processes nothing new.
    again = runner.invoke(app, ["run", "-c", str(project)])
    assert "No new PDFs" in again.output


def test_run_reports_google_sheet_problems_without_failing(project, fake_claude, monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    config = yaml.safe_load(project.read_text())
    config["outputs"]["google_sheet"] = {"enabled": True, "spreadsheet_id": "KEY"}
    project.write_text(yaml.safe_dump(config))
    fake_claude({CLASSIC: {HAIKU: message(GOOD)}, EURO: {HAIKU: message(GOOD)}})
    result = runner.invoke(app, ["run", "-c", str(project)])
    assert result.exit_code == 0
    assert "! Google Sheet not updated: set GOOGLE_SERVICE_ACCOUNT_JSON" in result.output


def test_cost_estimate(project, fake_claude):
    fake = fake_claude()
    fake.route = lambda body: {"input_tokens": 4400}
    inbox = project.parent / "inbox"
    result = runner.invoke(app, ["cost", "--estimate", str(inbox), "-c", str(project)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "3 document(s), 4 page(s)" in out
    assert "claude-haiku-5-5: about 8,800 in /" in out  # the identical copy isn't counted
    assert "if every document checks out first time; about $" in out
    assert "if 10% need a retry on claude-sonnet-5-5" in out
    assert "Input tokens are counted exactly" in out
    assert "1 file(s) wouldn't be processed" in out


def test_cost_estimate_problems(project, fake_claude, tmp_path):
    fake_claude()
    missing = runner.invoke(app, ["cost", "--estimate", str(tmp_path / "nope"), "-c", str(project)])
    assert missing.exit_code == 1 and "input folder not found" in missing.output

    empty = tmp_path / "empty"
    empty.mkdir()
    none = runner.invoke(app, ["cost", "--estimate", str(empty), "-c", str(project)])
    assert "No PDFs to estimate" in none.output


def test_cost_estimate_api_errors(project, fake_claude, monkeypatch):
    fake = fake_claude()
    fake.route = lambda body: error_response(500, "busy")
    inbox = project.parent / "inbox"
    result = runner.invoke(app, ["cost", "--estimate", str(inbox), "-c", str(project)])
    assert result.exit_code == 1 and "couldn't count tokens" in result.output

    def no_client():
        raise ApiSetupError("no usable API credentials: set ANTHROPIC_API_KEY in .env")

    monkeypatch.setattr("ibis_docs.cli.make_client", no_client)
    result = runner.invoke(app, ["cost", "--estimate", str(inbox), "-c", str(project)])
    assert result.exit_code == 1 and "ANTHROPIC_API_KEY" in result.output


def test_cost_estimate_without_retries(project, fake_claude, example_data, write_config):
    example_data["model"]["escalate_to"] = None
    config = write_config(example_data)
    fake = fake_claude()
    fake.route = lambda body: {"input_tokens": 4400}
    inbox = project.parent / "inbox"
    result = runner.invoke(app, ["cost", "--estimate", str(inbox), "-c", str(config)])
    assert "Estimated cost: $" in result.output and "retry" not in result.output


class FakeServer:
    def __init__(self, command, env):
        FakeServer.command, FakeServer.env = command, env
        self.terminated = False

    def wait(self):
        return 0

    def terminate(self):
        self.terminated = True


def test_review_opens_the_page_on_this_computer_only(monkeypatch):
    opened = []
    monkeypatch.setattr("ibis_docs.cli.subprocess.Popen", FakeServer)
    monkeypatch.setattr("ibis_docs.cli._wait_for", lambda url, seconds: True)
    monkeypatch.setattr("ibis_docs.cli.webbrowser.open", opened.append)
    result = runner.invoke(app, ["review", "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0, result.output
    command = FakeServer.command
    assert command[1:4] == ["-m", "streamlit", "run"]
    assert command[4].endswith("review_app.py")
    assert command[command.index("--server.address") + 1] == "localhost"
    assert command[command.index("--server.headless") + 1] == "true"
    assert command[command.index("--browser.gatherUsageStats") + 1] == "false"
    assert FakeServer.env["IBIS_CONFIG"] == str(EXAMPLE_CONFIG.resolve())
    assert opened == ["http://localhost:8501"]


def test_review_without_a_server_opens_no_browser(monkeypatch):
    opened = []
    monkeypatch.setattr("ibis_docs.cli.subprocess.Popen", FakeServer)
    monkeypatch.setattr("ibis_docs.cli._wait_for", lambda url, seconds: False)
    monkeypatch.setattr("ibis_docs.cli.webbrowser.open", opened.append)
    runner.invoke(app, ["review", "-c", str(EXAMPLE_CONFIG)])
    assert opened == []


def test_review_stops_the_server_on_ctrl_c(monkeypatch):
    servers = []

    class Interrupted(FakeServer):
        def __init__(self, command, env):
            super().__init__(command, env)
            servers.append(self)

        def wait(self):
            raise KeyboardInterrupt

    monkeypatch.setattr("ibis_docs.cli.subprocess.Popen", Interrupted)
    monkeypatch.setattr("ibis_docs.cli._wait_for", lambda url, seconds: False)
    result = runner.invoke(app, ["review", "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0 and servers[0].terminated


def test_wait_for_gives_up():
    from ibis_docs.cli import _wait_for

    assert _wait_for("http://localhost:9/nothing", seconds=0.5) is False


def test_review_checks_the_config_first(tmp_path):
    result = runner.invoke(app, ["review", "-c", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1 and "not found" in result.output
