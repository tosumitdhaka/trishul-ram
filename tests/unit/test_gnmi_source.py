"""Tests for gNMI source connector."""
from __future__ import annotations

import json
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.gnmi.source import GnmiSource
from tram.core.exceptions import SourceError


def _response(val, path="/interfaces/interface[name=eth0]/state/counters"):
    return {
        "update": {
            "timestamp": 1234567890,
            "update": [{"path": path, "val": val}],
        }
    }


def _install_pygnmi(mock_client: MagicMock) -> MagicMock:
    """Patch sys.modules so ``from pygnmi.client import gNMIclient`` resolves."""
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
    mock_pygnmi_client = MagicMock()
    mock_pygnmi_client.gNMIclient = mock_client_cls
    return patch.dict(sys.modules, {"pygnmi": MagicMock(), "pygnmi.client": mock_pygnmi_client})


class TestGnmiSourceConfigModel:
    def test_defaults(self):
        from tram.models.pipeline import GnmiSourceConfig

        cfg = GnmiSourceConfig(type="gnmi", host="router1", subscriptions=[])
        assert cfg.subscription_mode == "stream"
        assert cfg.poll_interval_seconds == 60
        assert cfg.reconnect_delay_seconds == 5.0
        assert cfg.max_reconnect_attempts == 0

    def test_modes_validated(self):
        from pydantic import ValidationError

        from tram.models.pipeline import GnmiSourceConfig

        cfg = GnmiSourceConfig(type="gnmi", host="router1", subscription_mode="once")
        assert cfg.subscription_mode == "once"
        cfg = GnmiSourceConfig(type="gnmi", host="router1", subscription_mode="poll")
        assert cfg.subscription_mode == "poll"
        with pytest.raises(ValidationError):
            GnmiSourceConfig(type="gnmi", host="router1", subscription_mode="bogus")

    def test_model_dump_includes_new_fields(self):
        from tram.models.pipeline import GnmiSourceConfig

        cfg = GnmiSourceConfig(
            type="gnmi",
            host="router1",
            subscription_mode="poll",
            poll_interval_seconds=15,
            reconnect_delay_seconds=2.5,
            max_reconnect_attempts=3,
        )
        d = cfg.model_dump()
        assert d["subscription_mode"] == "poll"
        assert d["poll_interval_seconds"] == 15
        assert d["reconnect_delay_seconds"] == 2.5
        assert d["max_reconnect_attempts"] == 3


class TestGnmiSource:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"pygnmi": None, "pygnmi.client": None}):
            source = GnmiSource({"host": "localhost", "subscriptions": []})
            with pytest.raises(SourceError, match="pygnmi"):
                list(source.read())

    def test_default_port(self):
        source = GnmiSource({"host": "router1", "subscriptions": []})
        assert source.port == 57400

    def test_invalid_subscription_mode_raises(self):
        with pytest.raises(SourceError, match="subscription_mode"):
            GnmiSource({"host": "router1", "subscriptions": [], "subscription_mode": "bogus"})

    def test_default_reconnect_settings(self):
        source = GnmiSource({"host": "router1", "subscriptions": []})
        assert source.subscription_mode == "stream"
        assert source.reconnect_delay_seconds == 5.0
        assert source.max_reconnect_attempts == 0

    def test_subscribe_yields_updates(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.return_value = iter([_response({"in-octets": 100})])
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [{"path": "/interfaces/interface[name=*]/state"}],
            })
            it = source.read()
            results = [next(it)]
            it.close()

        assert len(results) == 1
        data = json.loads(results[0][0].decode())
        assert isinstance(data, list)
        assert data[0]["val"] == {"in-octets": 100}

    def test_meta_has_host_port(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.return_value = iter([_response(1)])
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "port": 57400,
                "subscriptions": [{"path": "/x"}],
            })
            it = source.read()
            _, meta = next(it)
            it.close()

        assert meta["gnmi_host"] == "router1"
        assert meta["gnmi_port"] == 57400

    def test_stream_mode_request_uses_stream(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.return_value = iter([_response(1)])
        with _install_pygnmi(mock_client):
            source = GnmiSource({"host": "router1", "subscriptions": []})
            it = source.read()
            next(it)
            it.close()

        subscribe = mock_client.subscribe_stream.call_args.kwargs["subscribe"]
        assert subscribe["mode"] == "STREAM"

    def test_empty_update_skipped_and_session_end_reconnects(self):
        # An empty update yields nothing; a session that ends without an
        # exception is a reconnect trigger (peer closed it), not a clean end.
        mock_client = MagicMock()
        mock_client.subscribe_stream.return_value = iter([{"update": {"timestamp": 0, "update": []}}])
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "max_reconnect_attempts": 1,
                "reconnect_delay_seconds": 0,
            })
            with pytest.raises(SourceError, match="failed session"):
                list(source.read())

    # ── Reconnect behaviour ────────────────────────────────────────────────

    def test_reconnects_after_stream_ends(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.side_effect = [iter([_response(1)]), iter([_response(2)])]
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "reconnect_delay_seconds": 0,
            })
            it = source.read()
            first = next(it)
            second = next(it)
            it.close()

        assert json.loads(first[0])[0]["val"] == 1
        assert json.loads(second[0])[0]["val"] == 2
        assert mock_client.subscribe_stream.call_count == 2

    def test_reconnects_after_connection_error(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.side_effect = [
            RuntimeError("conn lost"),
            iter([_response(3)]),
        ]
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "reconnect_delay_seconds": 0,
            })
            it = source.read()
            first = next(it)
            it.close()

        assert json.loads(first[0])[0]["val"] == 3
        assert mock_client.subscribe_stream.call_count == 2

    def test_reconnect_attempts_exhausted_raises(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.side_effect = RuntimeError("conn lost")
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "max_reconnect_attempts": 2,
                "reconnect_delay_seconds": 0,
            })
            with pytest.raises(SourceError, match="after 2 failed session"):
                list(source.read())

    # ── stop() semantics ───────────────────────────────────────────────────

    def test_stop_interrupts_backoff(self):
        attempted = threading.Event()
        mock_client = MagicMock()

        def _fail(*args, **kwargs):
            attempted.set()
            raise RuntimeError("conn lost")

        mock_client.subscribe_stream.side_effect = _fail
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "reconnect_delay_seconds": 60,
            })
            it = source.read()
            results = []
            reader = threading.Thread(target=lambda: results.append(list(it)))
            reader.start()
            assert attempted.wait(timeout=2.0)
            source.stop()
            reader.join(timeout=2.0)

        assert results == [[]]
        # The backoff was interrupted: no reconnect was attempted after stop.
        assert mock_client.subscribe_stream.call_count == 1

    def test_stop_closes_active_client(self):
        closed = threading.Event()
        mock_client = MagicMock()

        def _blocking_iter():
            yield _response(1)
            closed.wait(timeout=5.0)  # blocked subscription until client.close()

        def _close():
            closed.set()

        mock_client.subscribe_stream.return_value = _blocking_iter()
        mock_client.close.side_effect = _close
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "max_reconnect_attempts": 1,
            })
            it = source.read()
            assert next(it) is not None
            source.stop()  # closes the client → the blocked stream unblocks
            with pytest.raises(StopIteration):
                next(it)

        mock_client.close.assert_called_once()
        assert mock_client.subscribe_stream.call_count == 1

    def test_stop_before_read_returns_without_connecting(self):
        mock_client = MagicMock()
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_pygnmi_client = MagicMock()
        mock_pygnmi_client.gNMIclient = mock_client_cls
        with patch.dict(sys.modules, {"pygnmi": MagicMock(), "pygnmi.client": mock_pygnmi_client}):
            source = GnmiSource({"host": "router1", "subscriptions": []})
            source.stop()
            assert list(source.read()) == []
            mock_client_cls.assert_not_called()

    # ── once / poll modes ──────────────────────────────────────────────────

    def test_once_mode_single_snapshot_then_ends(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.return_value = iter([_response(7)])
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "subscription_mode": "once",
            })
            results = list(source.read())

        assert len(results) == 1
        assert json.loads(results[0][0])[0]["val"] == 7
        subscribe = mock_client.subscribe_stream.call_args.kwargs["subscribe"]
        assert subscribe["mode"] == "ONCE"
        assert mock_client.subscribe_stream.call_count == 1

    def test_poll_mode_periodic_reget(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.side_effect = [iter([_response(1)]), iter([_response(2)])]
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "subscription_mode": "poll",
                "poll_interval_seconds": 0,
            })
            it = source.read()
            first = next(it)
            second = next(it)
            source.stop()
            with pytest.raises(StopIteration):
                next(it)

        assert json.loads(first[0])[0]["val"] == 1
        assert json.loads(second[0])[0]["val"] == 2
        subscribe = mock_client.subscribe_stream.call_args.kwargs["subscribe"]
        assert subscribe["mode"] == "ONCE"
        assert mock_client.subscribe_stream.call_count == 2

    def test_poll_stop_interrupts_poll_interval(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.return_value = iter([_response(1)])
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "subscription_mode": "poll",
                "poll_interval_seconds": 60,
            })
            it = source.read()
            assert next(it) is not None
            source.stop()  # sleeping between polls — must be interrupted
            with pytest.raises(StopIteration):
                next(it)

        assert mock_client.subscribe_stream.call_count == 1

    def test_poll_mode_reconnect_attempts_exhausted_raises(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.side_effect = RuntimeError("conn lost")
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "subscription_mode": "poll",
                "poll_interval_seconds": 0,
                "max_reconnect_attempts": 2,
                "reconnect_delay_seconds": 0,
            })
            with pytest.raises(SourceError, match="gNMI poll failed after 2 failed poll"):
                list(source.read())

    def test_poll_mode_success_resets_failure_counter(self):
        mock_client = MagicMock()
        mock_client.subscribe_stream.side_effect = [
            RuntimeError("conn lost"),  # consecutive failure 1
            iter([_response(1)]),       # healthy snapshot → counter resets
            RuntimeError("conn lost"),  # consecutive failure 1 (again)
            RuntimeError("conn lost"),  # consecutive failure 2 → exhausted
        ]
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "subscription_mode": "poll",
                "poll_interval_seconds": 0,
                "max_reconnect_attempts": 2,
                "reconnect_delay_seconds": 0,
            })
            it = source.read()
            assert json.loads(next(it)[0])[0]["val"] == 1
            with pytest.raises(SourceError, match="after 2 failed poll"):
                list(it)

        assert mock_client.subscribe_stream.call_count == 4

    def test_once_mode_stop_unblocks_blocked_snapshot(self):
        """stop() during a blocked ONCE snapshot must unblock it promptly.

        Regression: once mode never registered ``self._client``, so stop()
        could not close the active session — a blocked snapshot only ended
        when the iterator happened to finish on its own.
        """
        snapshot_entered = threading.Event()
        released = threading.Event()
        mock_client = MagicMock()

        def _blocking_iter():
            snapshot_entered.set()
            yield _response(1)
            released.wait(timeout=10.0)  # blocked snapshot until client.close()

        def _close():
            released.set()

        mock_client.subscribe_stream.return_value = _blocking_iter()
        mock_client.close.side_effect = _close
        with _install_pygnmi(mock_client):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [],
                "subscription_mode": "once",
            })
            it = source.read()
            results = []
            reader = threading.Thread(target=lambda: results.append(list(it)))
            reader.start()
            assert snapshot_entered.wait(timeout=2.0)
            source.stop()  # closes the client → the blocked snapshot unblocks
            reader.join(timeout=2.0)

        assert not reader.is_alive()
        assert len(results) == 1
        assert json.loads(results[0][0][0])[0]["val"] == 1
        mock_client.close.assert_called_once()
        assert mock_client.subscribe_stream.call_count == 1