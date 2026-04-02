"""Tests for gNMI source connector."""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.gnmi.source import GnmiSource
from tram.core.exceptions import SourceError


class TestGnmiSource:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"pygnmi": None, "pygnmi.client": None}):
            source = GnmiSource({"host": "localhost", "subscriptions": []})
            with pytest.raises(SourceError, match="pygnmi"):
                list(source.read())

    def test_default_port(self):
        source = GnmiSource({"host": "router1", "subscriptions": []})
        assert source.port == 57400

    def test_subscribe_yields_updates(self):
        mock_client = MagicMock()
        mock_response = {
            "update": {
                "timestamp": 1234567890,
                "update": [
                    {"path": "/interfaces/interface[name=eth0]/state/counters", "val": {"in-octets": 100}},
                ]
            }
        }
        mock_client.subscribe_stream.return_value = iter([mock_response])
        mock_gnmi_client_cls = MagicMock()
        mock_gnmi_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_gnmi_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        mock_pygnmi_client = MagicMock()
        mock_pygnmi_client.gNMIclient = mock_gnmi_client_cls

        with patch.dict(sys.modules, {"pygnmi": MagicMock(), "pygnmi.client": mock_pygnmi_client}):
            source = GnmiSource({
                "host": "router1",
                "subscriptions": [{"path": "/interfaces/interface[name=*]/state"}],
            })
            results = list(source.read())

        assert len(results) == 1
        data = json.loads(results[0][0].decode())
        assert isinstance(data, list)
        assert data[0]["val"] == {"in-octets": 100}

    def test_meta_has_host_port(self):
        mock_client = MagicMock()
        mock_response = {
            "update": {
                "timestamp": 0,
                "update": [{"path": "/x", "val": 1}]
            }
        }
        mock_client.subscribe_stream.return_value = iter([mock_response])
        mock_gnmi_client_cls = MagicMock()
        mock_gnmi_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_gnmi_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_pygnmi_client = MagicMock()
        mock_pygnmi_client.gNMIclient = mock_gnmi_client_cls

        with patch.dict(sys.modules, {"pygnmi": MagicMock(), "pygnmi.client": mock_pygnmi_client}):
            source = GnmiSource({
                "host": "router1",
                "port": 57400,
                "subscriptions": [{"path": "/x"}],
            })
            _, meta = list(source.read())[0]

        assert meta["gnmi_host"] == "router1"
        assert meta["gnmi_port"] == 57400

    def test_empty_update_skipped(self):
        mock_client = MagicMock()
        mock_response = {"update": {"timestamp": 0, "update": []}}
        mock_client.subscribe_stream.return_value = iter([mock_response])
        mock_gnmi_client_cls = MagicMock()
        mock_gnmi_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_gnmi_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_pygnmi_client = MagicMock()
        mock_pygnmi_client.gNMIclient = mock_gnmi_client_cls

        with patch.dict(sys.modules, {"pygnmi": MagicMock(), "pygnmi.client": mock_pygnmi_client}):
            source = GnmiSource({"host": "router1", "subscriptions": []})
            results = list(source.read())

        assert results == []
