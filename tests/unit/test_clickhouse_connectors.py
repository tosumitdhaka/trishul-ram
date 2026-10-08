"""Tests for ClickHouse source and sink connectors."""
from __future__ import annotations

import json
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.clickhouse.sink import ClickHouseSink
from tram.connectors.clickhouse.source import ClickHouseSource
from tram.core.exceptions import SinkError, SourceError
from tram.interfaces.base_sink import DeliveryTier

# ── ClickHouseSource tests ───────────────────────────────────────────────────


class TestClickHouseSource:
    def _make_source(self, extra: dict | None = None) -> ClickHouseSource:
        cfg = {"query": "SELECT id, name FROM test_table"}
        if extra:
            cfg.update(extra)
        return ClickHouseSource(cfg)

    def _mock_client(self, rows, columns):
        """Return a mock clickhouse_driver.Client that returns given rows/columns."""
        mock_client = MagicMock()
        mock_client.execute.return_value = (rows, [(c, None) for c in columns])
        mock_client.disconnect = MagicMock()
        return mock_client

    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"clickhouse_driver": None}):
            source = self._make_source()
            with pytest.raises(SourceError, match="clickhouse-driver"):
                list(source.read())

    def test_default_config_values(self):
        source = self._make_source()
        assert source.host == "localhost"
        assert source.port == 9000
        assert source.database == "default"
        assert source.username == "default"
        assert source.password == ""
        assert source.chunk_size == 0
        assert source.secure is False
        assert source.verify is True

    def test_custom_config_values(self):
        source = self._make_source({
            "host": "ch.example.com",
            "port": 9440,
            "database": "analytics",
            "username": "admin",
            "password": "secret",
            "secure": True,
            "verify": False,
        })
        assert source.host == "ch.example.com"
        assert source.port == 9440
        assert source.database == "analytics"
        assert source.secure is True
        assert source.verify is False

    def test_read_returns_rows_as_json(self):
        mock_module = MagicMock()
        mock_client = self._mock_client(
            rows=[(1, "alice"), (2, "bob")],
            columns=["id", "name"],
        )
        mock_module.Client.return_value = mock_client

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            source = self._make_source()
            results = list(source.read())

        assert len(results) == 1
        data, meta = results[0]
        records = json.loads(data)
        assert records == [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]
        assert meta["row_count"] == 2

    def test_read_empty_result(self):
        mock_module = MagicMock()
        mock_client = self._mock_client(rows=[], columns=["id", "name"])
        mock_module.Client.return_value = mock_client

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            source = self._make_source()
            results = list(source.read())

        assert len(results) == 1
        records = json.loads(results[0][0])
        assert records == []

    def test_query_error_raises_source_error(self):
        mock_module = MagicMock()
        mock_client = MagicMock()
        mock_client.execute.side_effect = RuntimeError("Connection refused")
        mock_client.disconnect = MagicMock()
        mock_module.Client.return_value = mock_client

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            source = self._make_source()
            with pytest.raises(SourceError, match="ClickHouse query failed"):
                list(source.read())

    def test_disconnect_called_after_read(self):
        mock_module = MagicMock()
        mock_client = self._mock_client(rows=[], columns=["x"])
        mock_module.Client.return_value = mock_client

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            source = self._make_source()
            list(source.read())

        mock_client.disconnect.assert_called_once()

    def test_chunk_size_config(self):
        source = self._make_source({"chunk_size": 100})
        assert source.chunk_size == 100

    def test_meta_contains_source_query(self):
        mock_module = MagicMock()
        mock_client = self._mock_client(rows=[(42,)], columns=["val"])
        mock_module.Client.return_value = mock_client

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            source = self._make_source({"query": "SELECT val FROM metrics"})
            results = list(source.read())

        _, meta = results[0]
        assert meta["source_query"] == "SELECT val FROM metrics"

    def test_registry_key(self):
        from tram.registry.registry import get_source
        cls = get_source("clickhouse")
        assert cls is ClickHouseSource


# ── ClickHouseSink tests ─────────────────────────────────────────────────────


class TestClickHouseSink:
    def _make_sink(self, extra: dict | None = None) -> ClickHouseSink:
        # batch_size=1 forces immediate flush on write; batch_timeout_seconds=0 disables timer
        cfg = {"table": "events", "batch_size": 1, "batch_timeout_seconds": 0}
        if extra:
            cfg.update(extra)
        return ClickHouseSink(cfg)

    def _mock_module(self):
        mock_module = MagicMock()
        mock_client = MagicMock()
        mock_client.execute = MagicMock()
        mock_client.disconnect = MagicMock()
        mock_module.Client.return_value = mock_client
        return mock_module, mock_client

    def test_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {"clickhouse_driver": None}):
            sink = self._make_sink()
            with pytest.raises(SinkError, match="clickhouse-driver"):
                sink.write(json.dumps([{"x": 1}]).encode(), {})

    def test_default_config_values(self):
        sink = self._make_sink()
        assert sink.host == "localhost"
        assert sink.port == 9000
        assert sink.database == "default"
        assert sink.username == "default"
        assert sink.password == ""
        assert sink.table == "events"

    @pytest.mark.parametrize(
        "bad_table",
        ["events; DROP TABLE x", "1events", "bad-name", "events(x) VALUES", "tbl name", "`tbl`"],
    )
    def test_invalid_table_name_rejected(self, bad_table):
        """Table names are validated as identifiers — no SQL injection surface."""
        with pytest.raises(SinkError, match="not a valid identifier"):
            ClickHouseSink({"table": bad_table, "batch_timeout_seconds": 0})

    @pytest.mark.parametrize("good_table", ["events", "pm_events", "_tmp", "Table2", "EVENTS"])
    def test_valid_table_names_accepted(self, good_table):
        sink = ClickHouseSink({"table": good_table, "batch_timeout_seconds": 0})
        assert sink.table == good_table
        sink.close()

    @pytest.mark.parametrize("good_table", ["analytics.events", "db.table", "default.events_2026", "_db.events"])
    def test_qualified_table_names_accepted(self, good_table):
        """Two-part `db.table` identifiers were previously rejected — keep them working."""
        sink = ClickHouseSink({"table": good_table, "batch_timeout_seconds": 0})
        assert sink.table == good_table
        sink.close()

    @pytest.mark.parametrize(
        "bad_table",
        [
            "db.table.col",  # only two parts allowed
            "db.`evil`",  # backticked identifier still rejected
            'db."evil"',  # quoted identifier still rejected
            "db.table; DROP TABLE x",  # injection attempt on the qualified form
            "db..table",
            "db.1table",
            ".hidden",
            "table.",
        ],
    )
    def test_invalid_qualified_table_names_rejected(self, bad_table):
        """Qualified names stay injection-safe: no quotes, dots run, or extra parts."""
        with pytest.raises(SinkError, match="not a valid identifier"):
            ClickHouseSink({"table": bad_table, "batch_timeout_seconds": 0})

    def test_write_inserts_records(self):
        mock_module, mock_client = self._mock_module()
        records = [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}]

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = self._make_sink()
            sink.write(json.dumps(records).encode(), {})

        mock_client.execute.assert_called_once()
        call_args = mock_client.execute.call_args
        assert "INSERT INTO events" in call_args[0][0]
        assert call_args[0][1] == records

    def test_write_empty_data_is_noop(self):
        mock_module, mock_client = self._mock_module()

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = self._make_sink()
            sink.write(json.dumps([]).encode(), {})

        mock_client.execute.assert_not_called()

    def test_invalid_json_raises_sink_error(self):
        mock_module, _ = self._mock_module()
        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = self._make_sink()
            with pytest.raises(SinkError, match="failed to parse"):
                sink.write(b"not json", {})

    def test_insert_error_raises_sink_error(self):
        mock_module, mock_client = self._mock_module()
        mock_client.execute.side_effect = RuntimeError("Table not found")

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = self._make_sink()
            with pytest.raises(SinkError, match="ClickHouse insert failed"):
                sink.write(json.dumps([{"x": 1}]).encode(), {})

    def test_disconnect_called_after_write(self):
        mock_module, mock_client = self._mock_module()

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = self._make_sink()
            sink.write(json.dumps([{"x": 1}]).encode(), {})

        mock_client.disconnect.assert_called_once()

    def test_disconnect_called_even_on_error(self):
        mock_module, mock_client = self._mock_module()
        mock_client.execute.side_effect = RuntimeError("error")

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = self._make_sink()
            with pytest.raises(SinkError):
                sink.write(json.dumps([{"x": 1}]).encode(), {})

        mock_client.disconnect.assert_called_once()

    def test_registry_key(self):
        from tram.registry.registry import get_sink
        cls = get_sink("clickhouse")
        assert cls is ClickHouseSink

    def test_secure_config(self):
        sink = self._make_sink({"secure": True, "verify": False})
        assert sink.secure is True
        assert sink.verify is False

    def test_batch_config_defaults(self):
        sink = ClickHouseSink({"table": "t", "batch_timeout_seconds": 0})
        assert sink.batch_size == 5000
        assert sink.batch_timeout_seconds == 0
        assert sink.batch_flush_on_stop is True
        sink.close()

    def test_batching_accumulates_until_batch_size(self):
        mock_module, mock_client = self._mock_module()
        records = [{"id": i} for i in range(3)]

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = ClickHouseSink({"table": "events", "batch_size": 3, "batch_timeout_seconds": 0})
            # First two writes should NOT flush
            sink.write(json.dumps([records[0]]).encode(), {})
            sink.write(json.dumps([records[1]]).encode(), {})
            mock_client.execute.assert_not_called()
            # Third write fills the buffer — should flush
            sink.write(json.dumps([records[2]]).encode(), {})

        mock_client.execute.assert_called_once()
        assert mock_client.execute.call_args[0][1] == records

    def test_close_flushes_remaining_buffer(self):
        mock_module, mock_client = self._mock_module()
        records = [{"id": 1}, {"id": 2}]

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = ClickHouseSink({"table": "events", "batch_size": 100, "batch_timeout_seconds": 0})
            sink.write(json.dumps(records).encode(), {})
            mock_client.execute.assert_not_called()
            sink.close()

        mock_client.execute.assert_called_once()
        assert mock_client.execute.call_args[0][1] == records

    def test_close_with_flush_on_stop_false_retains_buffer(self):
        """Stop/cancellation never erases the pending buffer (R1 fix): with
        ``batch_flush_on_stop=False`` no flush is attempted and the rows stay
        buffered for replay/retry — the sink never destroys unconfirmed input.
        """
        mock_module, mock_client = self._mock_module()

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = ClickHouseSink({
                "table": "events", "batch_size": 100,
                "batch_timeout_seconds": 0, "batch_flush_on_stop": False,
            })
            sink.write(json.dumps([{"id": 1}]).encode(), {})
            sink.close()

        mock_client.execute.assert_not_called()
        assert len(sink._buffer) == 1

    # ── Delivery tier / commit barrier (R1 fix, V18-01 section 6) ──────────

    def test_delivery_capability_declared_remote_durable(self):
        cap = ClickHouseSink.delivery_capability
        assert cap is not None
        assert cap.tier == DeliveryTier.REMOTE_DURABLE
        assert cap.replay_safe is False

    def test_insert_failure_retains_rows_and_commit_fails(self):
        """Insert failure: rows retained, no confirmed receipt, the failure is
        latched, and commit() fails — the executor cannot ack the source."""
        mock_module, mock_client = self._mock_module()
        mock_client.execute.side_effect = RuntimeError("Table not found")

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = self._make_sink()  # batch_size=1 forces a flush on write
            with pytest.raises(SinkError, match="ClickHouse insert failed"):
                sink.write(json.dumps([{"x": 1}]).encode(), {})
            # R1: the buffer is cleared only after the insert is confirmed.
            assert len(sink._buffer) == 1
            # The failure is latched and surfaces.
            assert sink.latched_error() is not None
            # commit() fails — no confirmed receipt.
            with pytest.raises(SinkError):
                sink.commit()

    def test_timer_flush_failure_latched_and_visible(self):
        """Background (timer) flush failure is latched, rows retained, and
        commit() refuses to report clean success."""
        mock_module, mock_client = self._mock_module()
        mock_client.execute.side_effect = RuntimeError("insert boom")

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = ClickHouseSink({"table": "events", "batch_size": 100, "batch_timeout_seconds": 0})
            sink.write(json.dumps([{"id": 1}]).encode(), {})
            assert sink.latched_error() is None
            sink._timer_flush()  # timer path invoked directly (no thread race)
            assert sink.latched_error() is not None
            assert len(sink._buffer) == 1  # retained
            with pytest.raises(SinkError):
                sink.commit()

    def test_flushes_serialized_no_interleaving(self):
        """Timer/foreground/close flushes are serialized: a second flush must
        not enter the insert while a first is in flight."""
        mock_module, mock_client = self._mock_module()
        active = 0
        peak = 0
        state_lock = threading.Lock()
        entered = threading.Event()
        release = threading.Event()

        def slow_execute(*args, **kwargs):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            entered.set()
            release.wait(timeout=5)
            with state_lock:
                active -= 1

        mock_client.execute.side_effect = slow_execute

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = ClickHouseSink({"table": "events", "batch_size": 100, "batch_timeout_seconds": 0})
            sink.write(json.dumps([{"id": 1}]).encode(), {})  # buffered, no flush yet
            first = threading.Thread(target=sink._flush)
            first.start()
            assert entered.wait(timeout=2)
            second = threading.Thread(target=sink._flush)
            second.start()
            time.sleep(0.2)  # the second flush must be blocked on the flush lock
            with state_lock:
                assert active == 1
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)
            with state_lock:
                assert active == 0
        assert peak == 1  # at most one insert in flight at any time

    def test_stop_path_close_flush_failure_retains_rows_and_latches(self):
        """A failed final flush on close latches the error and keeps the rows
        buffered — cancellation cannot erase the pending buffer."""
        mock_module, mock_client = self._mock_module()
        mock_client.execute.side_effect = RuntimeError("down")

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = ClickHouseSink({"table": "events", "batch_size": 100, "batch_timeout_seconds": 0})
            sink.write(json.dumps([{"id": 1}]).encode(), {})
            sink.close()
            assert sink.latched_error() is not None
            assert len(sink._buffer) == 1

    def test_commit_success_confirms_remote_durable(self):
        mock_module, mock_client = self._mock_module()

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = self._make_sink({"batch_size": 100})
            sink.write(json.dumps([{"id": 1}, {"id": 2}]).encode(), {})
            receipt = sink.commit()

        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True
        assert len(sink._buffer) == 0
        mock_client.execute.assert_called_once()
        assert mock_client.execute.call_args[0][1] == [{"id": 1}, {"id": 2}]

    def test_commit_with_empty_buffer_confirms(self):
        mock_module, mock_client = self._mock_module()

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = ClickHouseSink({"table": "events", "batch_timeout_seconds": 0})
            receipt = sink.commit()

        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True
        mock_client.execute.assert_not_called()

    def test_commit_fails_on_latched_error_without_retrying_flush(self):
        """commit() fails on a latched background failure even when the next
        flush would succeed — conservative barrier, no false clean success."""
        mock_module, mock_client = self._mock_module()
        calls = {"n": 0}

        def flaky_execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first attempt fails")

        mock_client.execute.side_effect = flaky_execute

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = ClickHouseSink({"table": "events", "batch_size": 100, "batch_timeout_seconds": 0})
            sink.write(json.dumps([{"id": 1}]).encode(), {})
            sink._timer_flush()  # fails -> latched
            with pytest.raises(SinkError):
                sink.commit()

        assert calls["n"] == 1  # commit did not even retry the insert

    def test_commit_recovers_after_latched_error_observed(self):
        """Once the latched error is observed, a healthy commit succeeds and
        delivers the retained rows."""
        mock_module, mock_client = self._mock_module()
        calls = {"n": 0}

        def flaky_execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first attempt fails")

        mock_client.execute.side_effect = flaky_execute

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = ClickHouseSink({"table": "events", "batch_size": 100, "batch_timeout_seconds": 0})
            sink.write(json.dumps([{"id": 1}]).encode(), {})
            sink._timer_flush()
            assert sink.latched_error() is not None
            receipt = sink.commit()  # latch observed -> flush retry succeeds

        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True
        assert len(sink._buffer) == 0
        assert calls["n"] == 2

    def test_commit_honors_deadline(self):
        mock_module, mock_client = self._mock_module()

        with patch.dict(sys.modules, {"clickhouse_driver": mock_module}):
            sink = self._make_sink({"batch_size": 100})
            with pytest.raises(SinkError, match="deadline"):
                sink.commit(deadline=time.monotonic() - 1)
        mock_client.execute.assert_not_called()
