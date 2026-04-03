"""Integration tests for SFTP pipeline end-to-end flow.

These tests use mocked SFTP connections to simulate a full
source → transform → sink pipeline run without requiring a real SFTP server.
"""

from __future__ import annotations

import json
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


@pytest.fixture
def sftp_pipeline_config():
    yaml_text = textwrap.dedent("""
        pipeline:
          name: sftp-integration-test
          source:
            type: sftp
            host: localhost
            port: 22
            username: testuser
            password: testpass
            remote_path: /export/pm
            file_pattern: "*.json"
          serializer_in:
            type: json
          transforms:
            - type: rename
              fields:
                ne_id: network_element_id
            - type: cast
              fields:
                rx_bytes: int
            - type: add_field
              fields:
                rx_mbps: "rx_bytes / 1000000"
            - type: drop
              fields: [debug]
            - type: filter
              condition: "rx_bytes > 0"
          serializer_out:
            type: json
            indent: 2
          sink:
            type: sftp
            host: localhost
            port: 22
            username: testuser
            password: testpass
            remote_path: /ingest/pm
            filename_template: "{pipeline}_{timestamp}.json"
    """)
    return load_pipeline_from_yaml(yaml_text)


def _make_mock_sftp(files: dict[str, bytes]):
    """Create a mock SFTP client with the given filename→content mapping."""
    mock_sftp = MagicMock()
    mock_sftp.listdir.return_value = list(files.keys())

    def open_side_effect(path, mode="r"):
        filename = path.split("/")[-1]
        content = files.get(filename, b"")
        mock_fh = MagicMock()
        mock_fh.__enter__ = lambda s: s
        mock_fh.__exit__ = MagicMock(return_value=False)
        mock_fh.read.return_value = content
        return mock_fh

    mock_sftp.open.side_effect = open_side_effect
    mock_sftp.stat.side_effect = FileNotFoundError()
    return mock_sftp


def _make_mock_paramiko(mock_sftp):
    """Patch paramiko.Transport and SFTPClient.from_transport."""
    mock_transport = MagicMock()
    mock_sftp_cls = MagicMock()
    mock_sftp_cls.from_transport.return_value = mock_sftp
    return mock_transport, mock_sftp_cls


class TestSFTPPipelineIntegration:
    def test_full_pipeline_run(self, sftp_pipeline_config):
        """Test complete source → transforms → sink pipeline flow."""
        executor = PipelineExecutor()

        input_records = [
            {"ne_id": "NE001", "rx_bytes": "1500000", "debug": "internal", "status": "ok"},
            {"ne_id": "NE002", "rx_bytes": "2500000", "debug": "internal", "status": "ok"},
            {"ne_id": "NE003", "rx_bytes": "0", "debug": "internal", "status": "ok"},
        ]
        input_bytes = json.dumps(input_records).encode()

        source_sftp = _make_mock_sftp({"data.json": input_bytes})
        sink_sftp = MagicMock()
        sink_sftp.stat.side_effect = FileNotFoundError()

        written_data = {}

        def capture_write(path, mode):
            fh = MagicMock()
            fh.__enter__ = lambda s: s
            fh.__exit__ = MagicMock(return_value=False)
            fh.write = lambda data: written_data.update({"data": data})
            return fh

        sink_sftp.open.side_effect = capture_write


        with (
            patch("paramiko.Transport") as mock_transport_cls,
            patch("paramiko.SFTPClient") as mock_sftp_client,
        ):
            mock_transport_cls.return_value.__enter__ = lambda s: s
            mock_transport_cls.return_value.connect = MagicMock()

            # Source SFTP
            mock_sftp_client.from_transport.side_effect = [source_sftp, sink_sftp]

            result = executor.batch_run(sftp_pipeline_config)

        assert result.status.value == "success"
        # 3 in, but NE003 has rx_bytes=0 so filtered out → 2 out
        assert result.records_in == 3
        assert result.records_out == 2
        assert result.records_skipped == 0

    def test_pipeline_applies_transforms_correctly(self, sftp_pipeline_config):
        """Verify transform chain produces expected field transformations."""
        from tram.pipeline.executor import PipelineExecutor

        executor = PipelineExecutor()

        # Test just the transform chain in isolation
        transforms = executor._build_transforms(sftp_pipeline_config)

        records = [
            {"ne_id": "NE001", "rx_bytes": "1500000", "debug": "x"},
        ]
        for t in transforms:
            records = t.apply(records)

        assert len(records) == 1
        r = records[0]
        assert "network_element_id" in r  # renamed
        assert "ne_id" not in r           # old name gone
        assert isinstance(r["rx_bytes"], int)  # cast
        assert "rx_mbps" in r              # added
        assert abs(r["rx_mbps"] - 1.5) < 0.001
        assert "debug" not in r            # dropped

    def test_pipeline_with_no_matching_files(self, sftp_pipeline_config):
        """Empty source should produce a successful run with 0 records."""
        executor = PipelineExecutor()
        empty_sftp = _make_mock_sftp({})  # No files

        sink_sftp = MagicMock()

        with patch("paramiko.Transport") as mock_transport_cls, patch("paramiko.SFTPClient") as mock_sftp_cls:
            mock_transport_cls.return_value.connect = MagicMock()
            mock_sftp_cls.from_transport.side_effect = [empty_sftp, sink_sftp]

            result = executor.batch_run(sftp_pipeline_config)

        assert result.status.value == "success"
        assert result.records_in == 0
        sink_sftp.open.assert_not_called()
