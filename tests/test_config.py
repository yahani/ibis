from decimal import Decimal

import pytest

from ibis_docs.config import ConfigError, load_config
from tests.conftest import EXAMPLE_CONFIG


def test_example_config_is_valid():
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.model.default == "claude-haiku-5-5"
    assert cfg.model.escalate_to == "claude-sonnet-5-5"
    assert cfg.validation.total_tolerance == Decimal("0.01")
    assert cfg.validation.allowed_currencies == ["USD", "GBP", "EUR"]
    assert cfg.suppliers[0].currency == "GBP"


def test_folders_are_relative_to_config(tmp_path, write_config):
    path = write_config({"input_dir": "in", "output_dir": "out"})
    cfg = load_config(path)
    assert cfg.input_path == tmp_path / "in"
    assert cfg.output_path == tmp_path / "out"


def test_empty_file_uses_defaults(write_config):
    cfg = load_config(write_config(""))
    assert cfg.validation.allowed_currencies == ["USD", "GBP", "EUR"]
    assert cfg.outputs.csv and cfg.outputs.excel
    assert cfg.move_files


def test_any_iso_currency_can_be_added(example_data, write_config):
    example_data["validation"]["allowed_currencies"] = ["usd", "AUD", "NZD", "JPY", "AUD"]
    example_data["suppliers"] = [{"name": "Kiwi Co", "currency": "nzd"}]
    cfg = load_config(write_config(example_data))
    assert cfg.validation.allowed_currencies == ["USD", "AUD", "NZD", "JPY"]


def test_non_iso_currency_is_rejected(example_data, write_config):
    example_data["validation"]["allowed_currencies"] = ["USD", "EURO", "XYZ"]
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(example_data))
    msg = str(exc.value)
    assert "validation.allowed_currencies" in msg
    assert "'EURO'" in msg and "'XYZ'" in msg


def test_empty_currency_list_is_rejected(example_data, write_config):
    example_data["validation"]["allowed_currencies"] = []
    with pytest.raises(ConfigError, match="allowed_currencies"):
        load_config(write_config(example_data))


def test_currency_symbols_remap(example_data, write_config):
    example_data["validation"]["allowed_currencies"] = ["AUD"]
    example_data["validation"]["currency_symbols"] = {"$": "aud"}
    example_data["suppliers"] = []
    cfg = load_config(write_config(example_data))
    assert cfg.validation.currency_symbols == {"$": "AUD"}
    assert cfg.validation.symbols["$"] == "AUD"
    assert cfg.validation.symbols["£"] == "GBP"


def test_currency_symbol_to_bad_code(example_data, write_config):
    example_data["validation"]["currency_symbols"] = {"$": "DOLLARS"}
    with pytest.raises(ConfigError, match=r"symbol '\$'.*not an ISO 4217"):
        load_config(write_config(example_data))


def test_blank_currency_symbol(example_data, write_config):
    example_data["validation"]["currency_symbols"] = {" ": "USD"}
    with pytest.raises(ConfigError, match="can't be blank"):
        load_config(write_config(example_data))


def test_supplier_currency_must_be_allowed(example_data, write_config):
    example_data["suppliers"] = [{"name": "Kiwi Co", "currency": "NZD"}]
    with pytest.raises(ConfigError, match=r"suppliers\[0\].currency: NZD is not in"):
        load_config(write_config(example_data))


def test_supplier_currency_must_be_iso(example_data, write_config):
    example_data["suppliers"] = [{"name": "Kiwi Co", "currency": "kiwi"}]
    with pytest.raises(ConfigError, match="not an ISO 4217"):
        load_config(write_config(example_data))


def test_duplicate_supplier(example_data, write_config):
    example_data["suppliers"] = [{"name": "Acme"}, {"name": "ACME"}]
    with pytest.raises(ConfigError, match=r"suppliers\[1\].name: duplicate of suppliers\[0\]"):
        load_config(write_config(example_data))


def test_tolerance_float_stays_exact(example_data, write_config):
    example_data["validation"]["total_tolerance"] = 0.01
    cfg = load_config(write_config(example_data))
    assert cfg.validation.total_tolerance == Decimal("0.01")


def test_tolerance_range(example_data, write_config):
    example_data["validation"]["total_tolerance"] = "-1"
    with pytest.raises(ConfigError, match="total_tolerance"):
        load_config(write_config(example_data))


def test_sheet_needs_id_when_enabled(example_data, write_config):
    example_data["outputs"]["google_sheet"]["enabled"] = True
    with pytest.raises(ConfigError, match="spreadsheet_id"):
        load_config(write_config(example_data))


def test_sheet_with_id(example_data, write_config):
    example_data["outputs"]["google_sheet"] = {"enabled": True, "spreadsheet_id": "abc"}
    assert load_config(write_config(example_data)).outputs.google_sheet.enabled


def test_at_least_one_output(example_data, write_config):
    example_data["outputs"] = {"csv": False, "excel": False}
    with pytest.raises(ConfigError, match="at least one output"):
        load_config(write_config(example_data))


def test_input_and_output_differ(example_data, write_config):
    example_data["output_dir"] = "inbox"
    with pytest.raises(ConfigError, match="output_dir: must be a different folder"):
        load_config(write_config(example_data))


def test_escalation_can_be_turned_off(example_data, write_config):
    example_data["model"]["escalate_to"] = None
    assert load_config(write_config(example_data)).model.escalate_to is None


def test_max_pages_range(example_data, write_config):
    example_data["model"]["max_pages"] = 0
    with pytest.raises(ConfigError, match=r"model.max_pages: .*\(got 0\)"):
        load_config(write_config(example_data))


def test_unknown_key_is_reported(example_data, write_config):
    example_data["modle"] = {}
    with pytest.raises(ConfigError, match="modle: unknown field"):
        load_config(write_config(example_data))


def test_base_dir_cannot_be_set(write_config):
    with pytest.raises(ConfigError, match="base_dir: unknown field"):
        load_config(write_config({"base_dir": "/"}))


@pytest.mark.parametrize("key", ["api_key", "ANTHROPIC_API_KEY", "google_credentials", "token"])
def test_secrets_are_refused(example_data, write_config, key):
    example_data["outputs"]["google_sheet"][key] = "x"
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(example_data))
    assert "environment variables" in str(exc.value)
    assert f"outputs.google_sheet.{key}" in str(exc.value)


def test_secret_inside_list(example_data, write_config):
    example_data["suppliers"][0]["password"] = "x"
    with pytest.raises(ConfigError, match=r"suppliers\[0\].password"):
        load_config(write_config(example_data))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml(write_config):
    with pytest.raises(ConfigError, match=r"invalid YAML at line 2, column \d+"):
        load_config(write_config("input_dir: in\n  bad: [\n"))


def test_top_level_must_be_mapping(write_config):
    with pytest.raises(ConfigError, match="expected a mapping"):
        load_config(write_config("- a\n- b\n"))
