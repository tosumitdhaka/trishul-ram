"""Shared test fixtures."""

from __future__ import annotations

import pytest

# Ensure plugins are registered before tests run
import tram.connectors  # noqa: F401
import tram.serializers  # noqa: F401
import tram.transforms  # noqa: F401


@pytest.fixture
def sample_records() -> list[dict]:
    return [
        {"ne_id": "NE001", "ts": "2024-01-01T00:00:00", "rx_bytes": "1500000", "active": "true", "severity": "1"},
        {"ne_id": "NE002", "ts": "2024-01-01T00:01:00", "rx_bytes": "2500000", "active": "false", "severity": "2"},
        {"ne_id": "NE003", "ts": "2024-01-01T00:02:00", "rx_bytes": "0", "active": "true", "severity": "3"},
    ]


@pytest.fixture
def minimal_pipeline_yaml() -> str:
    return """
version: "1"
pipeline:
  name: test-pipeline
  source:
    type: sftp
    host: localhost
    username: testuser
    password: testpass
    remote_path: /data
  serializer_in:
    type: json
  serializer_out:
    type: json
  sink:
    type: sftp
    host: localhost
    username: testuser
    password: testpass
    remote_path: /out
"""
