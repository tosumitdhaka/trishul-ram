from __future__ import annotations

from datetime import UTC, datetime

from tram.connectors.file_sink_common import file_state_key, render_filename, validate_template_tokens


def _opened_at() -> datetime:
    return datetime(2026, 4, 20, 12, 34, 56, tzinfo=UTC)


def test_render_filename_derives_source_stem_and_suffix() -> None:
    rendered = render_filename(
        "{source_stem}{source_suffix}",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={"source_filename": "input.csv"},
    )

    assert rendered == "input.csv"


def test_render_filename_handles_source_without_suffix() -> None:
    rendered = render_filename(
        "{source_stem}{source_suffix}",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={"source_filename": "README"},
    )

    assert rendered == "README"


def test_render_filename_falls_back_to_source_path_basename() -> None:
    rendered = render_filename(
        "{source_stem}{source_suffix}",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={"source_path": "/var/inbox/session/file.txt"},
    )

    assert rendered == "file.txt"


def test_render_filename_falls_back_to_data_when_source_missing() -> None:
    rendered = render_filename(
        "{source_stem}_{part}.csv",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={},
    )

    assert rendered == "data_00001.csv"


def test_render_filename_resolves_field_token() -> None:
    rendered = render_filename(
        "{field.nf_name}_{part}.ndjson",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={"field_values": {"nf_name": "SMSC"}},
    )

    assert rendered == "SMSC_00001.ndjson"


def test_render_filename_supports_epoch_ms_token() -> None:
    rendered = render_filename(
        "{epoch_ms}_{part}.ndjson",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={},
    )

    assert rendered == "1776688496000_00001.ndjson"


def test_render_filename_uses_unknown_for_missing_field_token() -> None:
    rendered = render_filename(
        "{field.nf_name}_{part}.ndjson",
        opened_at=_opened_at(),
        part_index=1,
        max_index=99999,
        meta={},
    )

    assert rendered == "unknown_00001.ndjson"


def test_file_state_key_excludes_rolling_tokens_and_includes_field_values() -> None:
    key = file_state_key(
        "{field.nf_name}_{source_stem}_{timestamp}_{part}.ndjson",
        meta={
            "source_filename": "input.csv",
            "field_values": {"nf_name": "MME"},
        },
    )

    assert key == (
        ("field.nf_name", "MME"),
        ("source_stem", "input"),
    )


def test_validate_template_tokens_rejects_unknown_tokens() -> None:
    issues = validate_template_tokens("{filename}_{part}.ndjson")

    assert issues == ["unknown template token 'filename'"]
