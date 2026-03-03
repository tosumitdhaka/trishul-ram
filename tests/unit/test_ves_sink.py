"""Tests for the VES sink connector."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.ves.sink import VESSink
from tram.core.exceptions import SinkError


class TestVESSink:
    def _make_mock_client(self, status_code: int = 202):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.text = "Accepted"

        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        return mock_client, mock_resp

    def test_posts_event_list(self):
        mock_client, _ = self._make_mock_client(202)

        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__ = lambda s: mock_client
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            sink = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            sink.write(b'[{"alarm": "critical"}]', {})

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args[1]
        body = json.loads(call_kwargs["content"])
        assert "eventList" in body
        assert len(body["eventList"]) == 1

    def test_wraps_each_record_in_envelope(self):
        mock_client, _ = self._make_mock_client(202)

        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__ = lambda s: mock_client
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            sink = VESSink({
                "url": "http://ves.example.com/eventListener/v7",
                "domain": "fault",
            })
            sink.write(b'[{"a": 1}, {"b": 2}]', {})

        body = json.loads(mock_client.post.call_args[1]["content"])
        assert len(body["eventList"]) == 2
        for event in body["eventList"]:
            assert "commonEventHeader" in event["event"]
            assert event["event"]["commonEventHeader"]["domain"] == "fault"

    def test_unexpected_status_raises_sink_error(self):
        mock_client, _ = self._make_mock_client(500)

        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__ = lambda s: mock_client
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            sink = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            with pytest.raises(SinkError, match="unexpected status 500"):
                sink.write(b'[{"x": 1}]', {})

    def test_bearer_auth_header(self):
        mock_client, _ = self._make_mock_client(202)

        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__ = lambda s: mock_client
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            sink = VESSink({
                "url": "http://ves.example.com/eventListener/v7",
                "auth_type": "bearer",
                "token": "mytoken",
            })
            sink.write(b'[{"x": 1}]', {})

        headers = mock_client.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer mytoken"

    def test_basic_auth(self):
        mock_client, _ = self._make_mock_client(202)

        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__ = lambda s: mock_client
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            sink = VESSink({
                "url": "http://ves.example.com/eventListener/v7",
                "auth_type": "basic",
                "username": "admin",
                "password": "secret",
            })
            sink.write(b'[{"x": 1}]', {})

        assert mock_client.post.call_args[1]["auth"] == ("admin", "secret")

    def test_invalid_json_raises_sink_error(self):
        sink = VESSink({"url": "http://ves.example.com/eventListener/v7"})
        with pytest.raises(SinkError, match="failed to parse data as JSON"):
            sink.write(b"not-json", {})

    def test_single_dict_wrapped_in_list(self):
        mock_client, _ = self._make_mock_client(202)

        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__ = lambda s: mock_client
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            sink = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            sink.write(b'{"alarm": "critical"}', {})

        body = json.loads(mock_client.post.call_args[1]["content"])
        assert len(body["eventList"]) == 1
