"""Live ClickHouse broker tests for the ClickHouse connectors (env-gated).

Follows the repo's live-PG pattern (``TestLivePostgres`` in
``test_execution_ledger.py``): the module is skipped entirely unless
``TRAM_TEST_CLICKHOUSE_URL`` points at a live ClickHouse. The connector speaks
the native protocol (port 9000) via ``clickhouse_driver`` — the HTTP URL is
used as the gate plus the host/credential source; the ``trishul`` database is
pre-created on the shared server. Every table is uuid-suffixed and dropped in
teardown, so consecutive runs never collide.

Pins the V18-01/§6 durable-commit contract against a real server:

- ``commit()`` is the barrier: rows stay buffered until it is called, then
  insert + confirm happen together and the rows are queryable;
- ``batch_flush_on_stop=False`` retains the unflushed buffer through a stop
  (no insert, no erase) and the retained rows survive to a later flush;
- the default ``batch_flush_on_stop=True`` flushes the buffer on close.
"""
from __future__ import annotations

import json
import os
import uuid
from urllib.parse import urlsplit

import pytest

from tram.connectors.clickhouse.sink import ClickHouseSink
from tram.interfaces.base_sink import DeliveryTier

CH_URL = os.environ.get("TRAM_TEST_CLICKHOUSE_URL", "")


def _ch_config() -> dict:
    """Connector config derived from the gate URL (native port, pre-created db)."""
    parts = urlsplit(CH_URL)
    return {
        "host": parts.hostname or "127.0.0.1",
        "port": 9000,  # native protocol — the connector never uses the HTTP port
        "database": "trishul",
        "username": parts.username or "default",
        "password": parts.password or "",
    }


def _ch_client():
    from clickhouse_driver import Client

    cfg = _ch_config()
    return Client(
        host=cfg["host"],
        port=cfg["port"],
        database=cfg["database"],
        user=cfg["username"],
        password=cfg["password"],
    )


@pytest.mark.skipif(not CH_URL, reason="TRAM_TEST_CLICKHOUSE_URL not set — no live ClickHouse fixture")
class TestLiveClickHouseBroker:
    """V18-10 broker-test gate: ClickHouse delivery contracts against a live server."""

    @pytest.fixture
    def table(self):
        """A unique MergeTree table; dropped after the test."""
        name = f"live_events_{uuid.uuid4().hex[:12]}"
        qualified = f"{_ch_config()['database']}.{name}"
        client = _ch_client()
        client.execute(
            f"CREATE TABLE {qualified} (id UInt64, val String) "
            "ENGINE = MergeTree ORDER BY id"
        )
        client.disconnect()
        yield qualified
        try:
            client = _ch_client()
            client.execute(f"DROP TABLE IF EXISTS {qualified}")
            client.disconnect()
        except Exception:
            pass

    def _row_count(self, table: str) -> int:
        client = _ch_client()
        rows = client.execute(f"SELECT count() FROM {table}")
        client.disconnect()
        return rows[0][0]

    def _sink(self, table: str, **overrides) -> ClickHouseSink:
        cfg = {**_ch_config(), "table": table}
        cfg.update(overrides)
        return ClickHouseSink(cfg)

    # ── durable commit / retained-buffer pins ──────────────────────────────

    def test_live_durable_commit_confirm_before_barrier(self, table):
        """Rows stay buffered until ``commit()``; the barrier flushes and only
        then confirms — after a confirmed receipt the rows are queryable and
        the buffer is empty (insert + confirm before the ack barrier)."""
        sink = self._sink(table, batch_size=100, batch_timeout_seconds=0)
        records = [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}]
        sink.write(json.dumps(records).encode(), {})
        assert self._row_count(table) == 0  # buffered — nothing inserted yet
        receipt = sink.commit()
        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True
        assert len(sink._buffer) == 0
        assert self._row_count(table) == 2  # confirmed insert is queryable
        sink.close()

    def test_live_batch_flush_on_stop_false_retains_buffer(self, table):
        """``batch_flush_on_stop=False``: ``close()`` never erases the pending
        buffer — rows stay in memory, nothing hits the server, and the
        retained rows survive to a subsequent flush attempt (commit), which
        delivers them."""
        sink = self._sink(
            table,
            batch_size=100,
            batch_timeout_seconds=0,
            batch_flush_on_stop=False,
        )
        records = [{"id": 1, "val": "kept"}, {"id": 2, "val": "retained"}]
        sink.write(json.dumps(records).encode(), {})
        sink.close()  # stop mid-batch — no flush attempted, nothing discarded
        assert len(sink._buffer) == 2  # retained, not erased
        assert self._row_count(table) == 0  # nothing reached the server
        receipt = sink.commit()  # retained buffer survives to this flush
        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True
        assert len(sink._buffer) == 0
        assert self._row_count(table) == 2

    def test_live_close_flushes_when_batch_flush_on_stop_true(self, table):
        """Default ``batch_flush_on_stop=True``: a stop flushes the remaining
        buffer, so acknowledged data is not left behind."""
        sink = self._sink(table, batch_size=100, batch_timeout_seconds=0)
        sink.write(json.dumps([{"id": 7, "val": "stop"}]).encode(), {})
        assert self._row_count(table) == 0
        sink.close()
        assert len(sink._buffer) == 0
        assert self._row_count(table) == 1