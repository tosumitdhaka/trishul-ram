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

    def _patch_pool(self, mock_client):
        """Patch the shared-client construction seam.

        V18-08: the VES sink reuses the process-lifetime pooled httpx.Client;
        tests patch the pool's construction seam and reset the pool after so
        a mock never leaks into a later test.
        """
        from tram.connectors import http_pool

        http_pool.reset_pool_for_tests()
        mock_class = MagicMock()
        mock_class.return_value = mock_client
        patcher = patch("tram.connectors.http_pool.httpx.Client", mock_class)
        patcher.start()
        return mock_client, mock_class, patcher, http_pool

    def _cleanup_pool(self, http_pool, patcher):
        patcher.stop()
        http_pool.reset_pool_for_tests()

    def test_posts_event_list(self):
        mock_client, _ = self._make_mock_client(202)
        _mock_class, patcher, http_pool = self._patch_pool(mock_client)[1:]
        try:
            sink = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            sink.write(b'[{"alarm": "critical"}]', {})
        finally:
            self._cleanup_pool(http_pool, patcher)

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args[1]
        body = json.loads(call_kwargs["content"])
        assert "eventList" in body
        assert len(body["eventList"]) == 1

    def test_wraps_each_record_in_envelope(self):
        mock_client, _ = self._make_mock_client(202)
        _mock_class, patcher, http_pool = self._patch_pool(mock_client)[1:]
        try:
            sink = VESSink({
                "url": "http://ves.example.com/eventListener/v7",
                "domain": "fault",
            })
            sink.write(b'[{"a": 1}, {"b": 2}]', {})
        finally:
            self._cleanup_pool(http_pool, patcher)

        body = json.loads(mock_client.post.call_args[1]["content"])
        assert len(body["eventList"]) == 2
        for event in body["eventList"]:
            assert "commonEventHeader" in event["event"]
            assert event["event"]["commonEventHeader"]["domain"] == "fault"

    def test_unexpected_status_raises_sink_error(self):
        mock_client, _ = self._make_mock_client(500)
        _mock_class, patcher, http_pool = self._patch_pool(mock_client)[1:]
        try:
            sink = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            with pytest.raises(SinkError, match="unexpected status 500"):
                sink.write(b'[{"x": 1}]', {})
        finally:
            self._cleanup_pool(http_pool, patcher)

    def test_bearer_auth_header(self):
        mock_client, _ = self._make_mock_client(202)
        _mock_class, patcher, http_pool = self._patch_pool(mock_client)[1:]
        try:
            sink = VESSink({
                "url": "http://ves.example.com/eventListener/v7",
                "auth_type": "bearer",
                "token": "mytoken",
            })
            sink.write(b'[{"x": 1}]', {})
        finally:
            self._cleanup_pool(http_pool, patcher)

        headers = mock_client.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer mytoken"

    def test_basic_auth(self):
        mock_client, _ = self._make_mock_client(202)
        _mock_class, patcher, http_pool = self._patch_pool(mock_client)[1:]
        try:
            sink = VESSink({
                "url": "http://ves.example.com/eventListener/v7",
                "auth_type": "basic",
                "username": "admin",
                "password": "secret",
            })
            sink.write(b'[{"x": 1}]', {})
        finally:
            self._cleanup_pool(http_pool, patcher)

        assert mock_client.post.call_args[1]["auth"] == ("admin", "secret")

    def test_invalid_json_raises_sink_error(self):
        sink = VESSink({"url": "http://ves.example.com/eventListener/v7"})
        with pytest.raises(SinkError, match="failed to parse data as JSON"):
            sink.write(b"not-json", {})

    def test_single_dict_wrapped_in_list(self):
        mock_client, _ = self._make_mock_client(202)
        _mock_class, patcher, http_pool = self._patch_pool(mock_client)[1:]
        try:
            sink = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            sink.write(b'{"alarm": "critical"}', {})
        finally:
            self._cleanup_pool(http_pool, patcher)

        body = json.loads(mock_client.post.call_args[1]["content"])
        assert len(body["eventList"]) == 1

    def test_shared_client_pooled_across_requests_and_sinks(self):
        """V18-08: one pooled httpx.Client serves all VES writes across sink
        instances — construction count pinned, not one client per write."""
        mock_client, _ = self._make_mock_client(202)
        mock_class, patcher, http_pool = self._patch_pool(mock_client)[1:]
        try:
            sink_a = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            sink_b = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            for _ in range(2):
                sink_a.write(b'[{"x": 1}]', {})
                sink_b.write(b'[{"y": 2}]', {})
        finally:
            self._cleanup_pool(http_pool, patcher)

        assert mock_client.post.call_count == 4
        assert mock_class.call_count == 1

    def test_close_is_per_sink_and_does_not_tear_down_shared_client(self):
        """V18-08: per-sink close() stays a no-op for VES — closing a sink
        must not break the delivery of a later run on the shared client."""
        mock_client, _ = self._make_mock_client(202)
        mock_class, patcher, http_pool = self._patch_pool(mock_client)[1:]
        try:
            sink_a = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            sink_a.write(b'[{"x": 1}]', {})
            close = getattr(sink_a, "close", None)
            assert callable(close)
            close()
            sink_b = VESSink({"url": "http://ves.example.com/eventListener/v7"})
            sink_b.write(b'[{"y": 2}]', {})
            sink_b.close()
        finally:
            self._cleanup_pool(http_pool, patcher)

        assert mock_client.post.call_count == 2
        assert mock_class.call_count == 1