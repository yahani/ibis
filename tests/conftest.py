import copy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "config.example.yaml"
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    """Tests never call the Claude API; make sure a real key can't leak into them."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # The CLI loads .env; in tests that would put a real key back into the environment.
    monkeypatch.setattr("ibis_docs.cli.load_dotenv", lambda *a, **k: False)


@pytest.fixture
def example_data() -> dict:
    """The example config as a dict, safe to mutate per test."""
    return copy.deepcopy(yaml.safe_load(EXAMPLE_CONFIG.read_text()))


@pytest.fixture
def write_config(tmp_path):
    """Write a dict (or raw text) to a temp config file and return its path."""

    def _write(data: dict | str) -> Path:
        path = tmp_path / "config.yaml"
        text = data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False)
        path.write_text(text)
        return path

    return _write
