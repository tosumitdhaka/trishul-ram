"""Opt-in SFTP source/publication reconciliation against an isolated fixture."""
from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock, patch
from urllib.parse import unquote, urlsplit

import pytest

from tram.connectors.sftp.sink import SFTPSink
from tram.core.context import RunStatus
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml

SFTP_URL = os.environ.get("TRAM_TEST_SFTP_URL", "")
pytestmark = pytest.mark.skipif(not SFTP_URL, reason="TRAM_TEST_SFTP_URL not set")


@pytest.mark.parametrize("action", ["move", "delete"])
def test_live_threaded_sftp_publication_failure_retains_input_then_replays(action):
    import paramiko

    parts = urlsplit(SFTP_URL)
    transport = paramiko.Transport((parts.hostname, parts.port or 22))
    transport.connect(username=unquote(parts.username or ""), password=unquote(parts.password or ""))
    client = paramiko.SFTPClient.from_transport(transport)
    root = f"{parts.path.rstrip('/')}/finalize_{uuid.uuid4().hex}"
    incoming, processed, output = (f"{root}/{name}" for name in ("in", "processed", "out"))
    for directory in (root, incoming, processed, output):
        client.mkdir(directory)
    records = [{"id": 1, "value": "alpha"}, {"id": 2, "value": "beta"}]
    source_path = f"{incoming}/input.json"
    with client.open(source_path, "w") as handle:
        handle.write(json.dumps(records))
    connection = {
        "type": "sftp", "host": parts.hostname, "port": parts.port or 22,
        "username": unquote(parts.username or ""), "password": unquote(parts.password or ""),
    }
    source_config = {
        **connection, "remote_path": incoming, "skip_processed": True,
        "move_after_read": processed if action == "move" else None,
        "delete_after_read": action == "delete",
    }
    sink_config = {
        **connection, "remote_path": output, "file_mode": "single",
        "filename_template": "{source_filename}",
    }
    config = load_pipeline_from_yaml(json.dumps({"pipeline": {
        "name": "sftp-finalization", "thread_workers": 2, "on_error": "abort",
        "source": source_config, "serializer_in": {"type": "json"},
        "serializer_out": {"type": "ndjson"}, "sink": sink_config,
    }}))
    tracker = MagicMock()
    tracker.is_processed.return_value = False
    executor = PipelineExecutor(file_tracker=tracker)
    sink = SFTPSink(sink_config)
    try:
        with (
            patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
            patch.object(sink._backend, "replace", side_effect=OSError("publication refused")),
        ):
            failed = executor.batch_run(config)
        assert failed.status == RunStatus.FAILED
        with client.open(source_path) as handle:
            assert json.loads(handle.read()) == records
        assert client.listdir(processed) == []
        assert "input.json" not in client.listdir(output)
        tracker.mark_processed.assert_not_called()

        replay = executor.batch_run(config)
        assert replay.status == RunStatus.SUCCESS
        assert client.listdir(incoming) == []
        with client.open(f"{output}/input.json") as handle:
            assert [json.loads(line) for line in handle.read().splitlines()] == records
        if action == "move":
            with client.open(f"{processed}/input.json") as handle:
                assert json.loads(handle.read()) == records
        tracker.mark_processed.assert_called_once()
        assert not any(name.endswith(".tmp") for name in client.listdir(output))
    finally:
        for directory in (incoming, processed, output):
            for name in client.listdir(directory):
                client.remove(f"{directory}/{name}")
            client.rmdir(directory)
        client.rmdir(root)
        client.close()
        transport.close()
