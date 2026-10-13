"""Concrete source/sink publication barriers under threaded execution."""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.local.sink import LocalSink
from tram.core.context import RunStatus
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


@pytest.mark.parametrize("action", ["move", "delete"])
def test_threaded_publication_failure_retains_source_until_replay(tmp_path, action):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    original = incoming / "input.json"
    records = [{"id": 1, "value": "alpha"}, {"id": 2, "value": "beta"}]
    original.write_text(json.dumps(records))
    processed = tmp_path / "processed"
    output = tmp_path / "output"
    source_action = (
        f"move_after_read: {processed}" if action == "move" else "delete_after_read: true"
    )
    config = load_pipeline_from_yaml(f"""pipeline:
  name: threaded-publication
  thread_workers: 2
  on_error: abort
  source:
    type: local
    path: {incoming}
    skip_processed: true
    {source_action}
  serializer_in:
    type: json
  serializer_out:
    type: ndjson
  sink:
    type: local
    path: {output}
    file_mode: single
    filename_template: "{{source_filename}}"
""")
    tracker = MagicMock()
    tracker.is_processed.return_value = False
    executor = PipelineExecutor(file_tracker=tracker)
    sink = LocalSink(config.sinks[0].model_dump())
    with (
        patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
        patch.object(sink._backend, "replace", side_effect=OSError("publication refused")),
    ):
        result = executor.batch_run(config)

    assert result.status == RunStatus.FAILED
    assert json.loads(original.read_text()) == records
    assert not (processed / original.name).exists()
    assert not (output / original.name).exists()
    tracker.mark_processed.assert_not_called()

    replay = executor.batch_run(config)
    assert replay.status == RunStatus.SUCCESS
    assert [json.loads(line) for line in (output / original.name).read_text().splitlines()] == records
    assert not original.exists()
    if action == "move":
        assert json.loads((processed / original.name).read_text()) == records
    tracker.mark_processed.assert_called_once()
    assert not list(output.glob("*.tmp"))


def test_threaded_stream_stop_waits_for_file_output_before_move(tmp_path):
    incoming, processed, output = (tmp_path / name for name in ("in", "processed", "out"))
    incoming.mkdir()
    for index, name in enumerate(("a.json", "b.json", "c.json")):
        (incoming / name).write_text(json.dumps([{"id": index}]))
    config = load_pipeline_from_yaml(f"""pipeline:
  name: threaded-stream-drain
  thread_workers: 2
  stream_flush_records: 100
  stream_flush_interval_s: 60
  schedule:
    type: stream
  source:
    type: local
    path: {incoming}
    move_after_read: {processed}
    skip_processed: true
  serializer_in:
    type: json
  serializer_out:
    type: ndjson
  sink:
    type: local
    path: {output}
    file_mode: single
    filename_template: "{{source_filename}}"
""")
    tracker = MagicMock()
    tracker.is_processed.return_value = False
    executor = PipelineExecutor(file_tracker=tracker)
    sink = LocalSink(config.sinks[0].model_dump())
    write = sink.write
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    results = []

    def blocked_write(data, meta):
        if meta["source_filename"] == "a.json":
            entered.set()
            assert release.wait(5)
        write(data, meta)

    with (
        patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
        patch.object(sink, "write", side_effect=blocked_write),
    ):
        runner = threading.Thread(target=lambda: results.append(executor.stream_run(config, stop)))
        runner.start()
        try:
            assert entered.wait(5)
            assert (incoming / "a.json").exists()
            assert not (processed / "a.json").exists()
            tracker.mark_processed.assert_not_called()
            stop.set()
        finally:
            release.set()
            runner.join(5)
        assert not runner.is_alive()

    assert results[0].status == RunStatus.SUCCESS
    assert (processed / "a.json").exists()
    assert [json.loads(line) for line in (output / "a.json").read_text().splitlines()] == [{"id": 0}]
    assert (incoming / "c.json").exists()
    assert not (processed / "c.json").exists()
