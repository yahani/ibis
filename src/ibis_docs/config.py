"""Load and validate config.yaml (E1)."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ibis_docs.money import is_iso_currency, symbol_table

DEFAULT_CURRENCIES = ["USD", "GBP", "EUR"]

# Keys that look like secrets. These belong in environment variables, never in the config file.
SECRET_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|api_?key|credentials|private_key", re.IGNORECASE
)


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _iso_code(value: str) -> str:
    code = value.strip().upper()
    if not is_iso_currency(code):
        raise ValueError(f"not an ISO 4217 currency code: {value!r} (e.g. USD, GBP, EUR)")
    return code


class Models(_Strict):
    default: str = Field(default="claude-haiku-5-5", min_length=1)
    escalate_to: str | None = Field(
        default="claude-sonnet-5-5", description="Retry model for hard documents; null to skip"
    )
    max_pages: int = Field(default=20, ge=1, le=100)


class Validation(_Strict):
    total_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0, le=Decimal("1"))
    allowed_currencies: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CURRENCIES), min_length=1
    )
    currency_symbols: dict[str, str] = Field(
        default_factory=dict,
        description='Extra or overriding symbol → code mappings, e.g. {"$": "AUD"}',
    )

    @field_validator("total_tolerance", mode="before")
    @classmethod
    def _float_via_str(cls, value: Any) -> Any:
        # YAML parses 0.01 as a float; go through str so it stays exactly 0.01.
        return str(value) if isinstance(value, float) else value

    @field_validator("allowed_currencies")
    @classmethod
    def _iso_codes(cls, value: list[str]) -> list[str]:
        problems, codes = [], []
        for item in value:
            try:
                code = _iso_code(item)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            if code not in codes:
                codes.append(code)
        if problems:
            raise ValueError("; ".join(problems))
        return codes

    @field_validator("currency_symbols")
    @classmethod
    def _symbol_codes(cls, value: dict[str, str]) -> dict[str, str]:
        out = {}
        for symbol, code in value.items():
            if not str(symbol).strip():
                raise ValueError("a currency symbol can't be blank")
            try:
                out[str(symbol).strip()] = _iso_code(str(code))
            except ValueError as exc:
                raise ValueError(f"symbol {symbol!r}: {exc}") from exc
        return out

    @property
    def symbols(self) -> dict[str, str]:
        """The full symbol table: defaults plus this config's overrides."""
        return symbol_table(self.currency_symbols)


class GoogleSheet(_Strict):
    enabled: bool = False
    spreadsheet_id: str = Field(default="", description="The sheet's ID or its full URL")

    @model_validator(mode="after")
    def _id_when_enabled(self) -> GoogleSheet:
        if self.enabled and not self.spreadsheet_id:
            raise ValueError("'spreadsheet_id' is required when google_sheet is enabled")
        return self


class Outputs(_Strict):
    csv: bool = True
    excel: bool = True
    google_sheet: GoogleSheet = Field(default_factory=GoogleSheet)

    @model_validator(mode="after")
    def _at_least_one(self) -> Outputs:
        if not (self.csv or self.excel or self.google_sheet.enabled):
            raise ValueError("turn on at least one output (csv, excel or google_sheet)")
        return self


class SupplierHint(_Strict):
    name: str = Field(min_length=1)
    currency: str | None = None
    notes: str | None = Field(default=None, description="Layout tips passed to the model")

    @field_validator("currency")
    @classmethod
    def _iso(cls, value: str | None) -> str | None:
        return None if value is None else _iso_code(value)


class Config(_Strict):
    input_dir: str = Field(default="inbox", min_length=1, description="Relative to the config file")
    output_dir: str = Field(default="output", min_length=1)
    move_files: bool = True
    model: Models = Field(default_factory=Models)
    validation: Validation = Field(default_factory=Validation)
    outputs: Outputs = Field(default_factory=Outputs)
    suppliers: list[SupplierHint] = Field(default_factory=list)

    # Folders are resolved against the config file's folder by load_config().
    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        problems: list[str] = []
        if Path(self.input_dir) == Path(self.output_dir):
            problems.append("output_dir: must be a different folder from input_dir")

        seen: dict[str, int] = {}
        allowed = self.validation.allowed_currencies
        for i, supplier in enumerate(self.suppliers):
            key = supplier.name.casefold()
            if key in seen:
                problems.append(
                    f"suppliers[{i}].name: duplicate of suppliers[{seen[key]}] ('{supplier.name}')"
                )
            seen.setdefault(key, i)
            if supplier.currency and supplier.currency not in allowed:
                problems.append(
                    f"suppliers[{i}].currency: {supplier.currency} is not in "
                    f"validation.allowed_currencies ({', '.join(allowed)})"
                )

        if problems:
            raise ValueError("\n".join(problems))
        return self

    @property
    def input_path(self) -> Path:
        return self.base_dir / self.input_dir

    @property
    def output_path(self) -> Path:
        return self.base_dir / self.output_dir


def load_config(path: str | Path) -> Config:
    """Read and validate a config file. Raises ConfigError with a readable message."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{path}: config file not found")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" at line {mark.line + 1}, column {mark.column + 1}"
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(f"{path}: invalid YAML{where}: {problem}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping of settings at the top level")
    if "base_dir" in raw:
        raise ConfigError(
            f"{path}: invalid config:\n  base_dir: unknown field (check the spelling)"
        )

    secret_paths = list(_find_secret_keys(raw))
    if secret_paths:
        lines = [f"{path}: secrets must come from environment variables, not the config file:"]
        lines += [f"  {p}" for p in secret_paths]
        raise ConfigError("\n".join(lines))

    try:
        return Config.model_validate({**raw, "base_dir": path.resolve().parent})
    except ValidationError as exc:
        lines = [f"{path}: invalid config:"]
        lines += [f"  {line}" for line in _format_errors(exc)]
        raise ConfigError("\n".join(lines)) from exc


def _find_secret_keys(node: Any, prefix: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            key_path = f"{prefix}.{key}" if prefix else str(key)
            if SECRET_KEY_PATTERN.search(str(key)):
                yield key_path
            yield from _find_secret_keys(value, key_path)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _find_secret_keys(item, f"{prefix}[{i}]")


def _format_loc(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


def _format_errors(exc: ValidationError) -> list[str]:
    lines: list[str] = []
    for err in exc.errors():
        loc = _format_loc(err["loc"])
        kind = err["type"]
        if kind == "missing":
            msg = "missing required field"
        elif kind == "extra_forbidden":
            msg = "unknown field (check the spelling)"
        else:
            msg = err["msg"].removeprefix("Value error, ")
            value = err.get("input")
            if isinstance(value, str | int | float | bool) and kind != "value_error":
                msg += f" (got {value!r})"

        for msg_line in msg.splitlines():
            # Cross-field checks already include their own path in the message.
            lines.append(f"{loc}: {msg_line}" if loc else msg_line)
    return lines
