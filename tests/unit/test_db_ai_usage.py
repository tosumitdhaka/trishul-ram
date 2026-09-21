"""Tests for the AI-audit append-only log (A10): ai_usage table + TRAM_AI_AUDIT flag."""
from __future__ import annotations

import logging

import pytest

from tram.core.config import ai_audit_enabled
from tram.persistence.db import TramDB


@pytest.fixture
def db(tmp_path):
    d = TramDB(url=f"sqlite:///{tmp_path}/test.db")
    yield d
    d.close()


def _entry(mode="generate", ok=True, ts="2026-09-21T00:00:00+00:00", tokens=(10, 25)):
    return dict(
        ts=ts, mode=mode, client="10.0.0.1", provider="anthropic",
        model="claude-haiku-4-5-20251001", tokens_in=tokens[0], tokens_out=tokens[1], ok=ok,
    )


def test_append_and_read_ai_usage(db):
    db.append_ai_usage(**_entry())
    rows = db.get_ai_usage()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"]
    assert row["ts"] == "2026-09-21T00:00:00+00:00"
    assert row["mode"] == "generate"
    assert row["client"] == "10.0.0.1"
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["tokens_in"] == 10
    assert row["tokens_out"] == 25
    assert row["ok"] == 1
    assert row["schema_version"] is None  # not passed → NULL


def test_schema_version_roundtrip(db):
    # Issue #24: the audit row carries the schema identity the prompt was
    # built against.
    db.append_ai_usage(**_entry(), schema_version="eecd1712ea4d")
    row = db.get_ai_usage()[0]
    assert row["schema_version"] == "eecd1712ea4d"


def test_append_is_append_only_every_call_gets_new_id(db):
    first = _entry(ts="2026-09-21T00:00:01+00:00")
    db.append_ai_usage(**first)
    db.append_ai_usage(**first)  # same payload twice — must be two distinct rows
    assert len(db.get_ai_usage()) == 2


def test_tokens_null_when_not_reported(db):
    db.append_ai_usage(**_entry(tokens=(None, None)))
    row = db.get_ai_usage()[0]
    assert row["tokens_in"] is None
    assert row["tokens_out"] is None


def test_ok_false_recorded(db):
    db.append_ai_usage(**_entry(ok=False))
    assert db.get_ai_usage()[0]["ok"] == 0


def test_get_ai_usage_newest_first(db):
    db.append_ai_usage(**_entry(mode="a", ts="2026-09-21T00:00:01+00:00"))
    db.append_ai_usage(**_entry(mode="b", ts="2026-09-21T00:00:02+00:00"))
    assert [r["mode"] for r in db.get_ai_usage()] == ["b", "a"]


def test_get_ai_usage_limit(db):
    for i in range(3):
        db.append_ai_usage(**_entry(mode=f"m{i}", ts=f"2026-09-21T00:00:0{i}+00:00"))
    assert len(db.get_ai_usage(limit=2)) == 2


# ── v1.4.3 migration: schema_version column on pre-existing databases ───────


def test_old_shape_ai_usage_table_migrates_in_place(tmp_path):
    # A v1.4.1/v1.4.2 database has ai_usage WITHOUT schema_version. Building a
    # TramDB on it must ALTER the table (following the _add_column_if_missing
    # pattern), keep existing rows readable (schema_version NULL), and accept
    # new rows with the column.
    db_path = tmp_path / "legacy.db"

    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE ai_usage (
                id         TEXT PRIMARY KEY NOT NULL,
                ts         TEXT NOT NULL,
                mode       TEXT NOT NULL,
                client     TEXT NOT NULL,
                provider   TEXT NOT NULL,
                model      TEXT NOT NULL,
                tokens_in  INTEGER,
                tokens_out INTEGER,
                ok         INTEGER NOT NULL DEFAULT 1
            )
        """))
        conn.execute(text("""
            INSERT INTO ai_usage (id, ts, mode, client, provider, model, tokens_in, tokens_out, ok)
            VALUES ('legacy-row', '2026-09-20T00:00:00+00:00', 'generate', '10.0.0.9',
                    'anthropic', 'claude-haiku-4-5-20251001', 1, 2, 1)
        """))
    engine.dispose()

    d = TramDB(url=f"sqlite:///{db_path}")
    try:
        rows = d.get_ai_usage()
        assert len(rows) == 1
        assert rows[0]["id"] == "legacy-row"
        assert rows[0]["schema_version"] is None  # old row predates the field

        # New rows carry the column.
        d.append_ai_usage(**_entry(), schema_version="eecd1712ea4d")
        new_row = next(r for r in d.get_ai_usage() if r["id"] != "legacy-row")
        assert new_row["schema_version"] == "eecd1712ea4d"
    finally:
        d.close()


# ── TRAM_AI_AUDIT feature flag (A10) ────────────────────────────────────────


def test_ai_audit_flag_defaults_on(monkeypatch):
    monkeypatch.delenv("TRAM_AI_AUDIT", raising=False)
    assert ai_audit_enabled() is True


def test_ai_audit_flag_off(monkeypatch):
    monkeypatch.setenv("TRAM_AI_AUDIT", "0")
    assert ai_audit_enabled() is False


def test_ai_audit_flag_unrecognized_value_fails_open(monkeypatch, caplog):
    monkeypatch.setenv("TRAM_AI_AUDIT", "maybe")
    with caplog.at_level(logging.WARNING, logger="tram.core.config"):
        assert ai_audit_enabled() is True
    assert any("TRAM_AI_AUDIT" in rec.getMessage() for rec in caplog.records)