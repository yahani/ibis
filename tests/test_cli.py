import pytest
from typer.testing import CliRunner

from ibis_docs import __version__
from ibis_docs.cli import app
from tests.conftest import EXAMPLE_CONFIG

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
        (["run"], 2),
        (["run", "--dry-run"], 2),
        (["show", "x.pdf"], 2),
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
