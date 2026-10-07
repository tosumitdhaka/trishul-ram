"""Golden-corpus tests for timestamp_normalize parsing and ISO formatting.

Fixture: ``tests/unit/data/timestamp_parse_corpus.json`` — 176 parse cases and
14 format cases captured from the pre-change implementation. Both
parametrizations are strict: every fixture entry must reproduce exactly; there
is no filtering or skipping.
"""

from __future__ import annotations

import builtins
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.timestamp_normalize import TimestampNormalizeTransform, _parse_timestamp

_FIXTURE_PATH = Path(__file__).parent / "data" / "timestamp_parse_corpus.json"

_CORPUS = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _build_val(case: dict) -> Any:
    kind = case["val_kind"]
    if kind == "str":
        return case["val"]
    if kind == "int":
        return int(case["val"])
    if kind == "float":
        return float(case["val"])
    if kind == "dt":
        return datetime.fromisoformat(case["val"])
    raise AssertionError(f"unknown val_kind {kind!r}")


def _build_source_tz(case: dict) -> ZoneInfo | None:
    name = case.get("source_tz")
    return ZoneInfo(name) if name else None


def _exception_type(name: str) -> type[Exception]:
    if name == "TransformError":
        return TransformError
    return getattr(builtins, name)


def test_corpus_case_counts():
    # Strictness guard: no fixture entry may be added or dropped silently.
    assert len(_CORPUS["parse_cases"]) == 176
    assert len(_CORPUS["format_cases"]) == 14


@pytest.mark.parametrize(
    "case",
    _CORPUS["parse_cases"],
    ids=[f"{i}:{c['val']!r}" for i, c in enumerate(_CORPUS["parse_cases"])],
)
def test_parse_corpus(case: dict):
    val = _build_val(case)
    expected = case["expected"]
    input_format = case.get("input_format")
    source_tz = _build_source_tz(case)

    if "dt" in expected:
        result = _parse_timestamp(val, input_format, source_tz)
        assert result.isoformat(timespec="microseconds") == expected["dt"]
    else:
        with pytest.raises(_exception_type(expected["error"])) as exc_info:
            _parse_timestamp(val, input_format, source_tz)
        assert str(exc_info.value) == expected["msg"]


@pytest.mark.parametrize(
    "case",
    _CORPUS["format_cases"],
    ids=[f"{i}:{c['dt']!r}" for i, c in enumerate(_CORPUS["format_cases"])],
)
def test_format_corpus(case: dict):
    dt = datetime.fromisoformat(case["dt"])
    transform = TimestampNormalizeTransform({"fields": ["ts"]})
    assert transform._format(dt) == case["expected_output"]