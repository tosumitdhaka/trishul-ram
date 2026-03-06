"""Integration test: local→local pipeline end-to-end (no mocks, real files).

Writes real JSON files to a temp directory, runs a full pipeline that reads,
transforms, and writes them to a second temp directory, then asserts output.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


@pytest.fixture
def tmp_dirs(tmp_path):
    src = tmp_path / "source"
    dst = tmp_path / "sink"
    src.mkdir()
    dst.mkdir()
    return src, dst


def _write_records(directory: Path, filename: str, records: list[dict]) -> None:
    (directory / filename).write_text(json.dumps(records))


def _read_sink(directory: Path) -> list[dict]:
    """Read all JSON files from the sink directory and collect records."""
    records = []
    for f in sorted(directory.iterdir()):
        data = json.loads(f.read_text())
        if isinstance(data, list):
            records.extend(data)
        else:
            records.append(data)
    return records


class TestLocalPipelineEndToEnd:
    def test_basic_local_to_local(self, tmp_dirs):
        """Full source→sink pipeline run with real files, no transforms."""
        src, dst = tmp_dirs
        records = [{"id": 1, "value": "alpha"}, {"id": 2, "value": "beta"}]
        _write_records(src, "data.json", records)

        config = load_pipeline_from_yaml(textwrap.dedent(f"""
            pipeline:
              name: local-to-local
              source:
                type: local
                path: {src}
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: {dst}
        """))

        executor = PipelineExecutor()
        result = executor.batch_run(config)

        assert result.status.value == "success"
        assert result.records_in == 2
        assert result.records_out == 2
        output = _read_sink(dst)
        assert len(output) == 2

    def test_pipeline_with_transforms(self, tmp_dirs):
        """Pipeline applies rename+cast+filter transforms correctly."""
        src, dst = tmp_dirs
        records = [
            {"old_name": "Alice", "score": "95", "internal": True},
            {"old_name": "Bob", "score": "40", "internal": True},
        ]
        _write_records(src, "input.json", records)

        config = load_pipeline_from_yaml(textwrap.dedent(f"""
            pipeline:
              name: transform-test
              source:
                type: local
                path: {src}
              serializer_in:
                type: json
              transforms:
                - type: rename
                  fields:
                    old_name: name
                - type: cast
                  fields:
                    score: int
                - type: drop
                  fields: [internal]
                - type: filter
                  condition: "score >= 50"
              serializer_out:
                type: json
              sink:
                type: local
                path: {dst}
        """))

        executor = PipelineExecutor()
        result = executor.batch_run(config)

        assert result.status.value == "success"
        assert result.records_in == 2
        assert result.records_out == 1  # Bob filtered out (score < 50)

        output = _read_sink(dst)
        assert len(output) == 1
        r = output[0]
        assert r["name"] == "Alice"
        assert isinstance(r["score"], int)
        assert r["score"] == 95
        assert "internal" not in r
        assert "old_name" not in r

    def test_pipeline_with_on_error_continue(self, tmp_dirs):
        """Pipeline continues processing after a record error when on_error=continue."""
        src, dst = tmp_dirs
        # Mix valid + invalid cast targets
        records = [
            {"id": 1, "count": "10"},
            {"id": 2, "count": "not_a_number"},
            {"id": 3, "count": "30"},
        ]
        _write_records(src, "mixed.json", records)

        config = load_pipeline_from_yaml(textwrap.dedent(f"""
            pipeline:
              name: error-continue
              on_error: continue
              source:
                type: local
                path: {src}
              serializer_in:
                type: json
              transforms:
                - type: cast
                  fields:
                    count: int
              serializer_out:
                type: json
              sink:
                type: local
                path: {dst}
        """))

        executor = PipelineExecutor()
        result = executor.batch_run(config)

        # Should succeed even with one error, skipping bad record
        assert result.status.value in ("success", "partial")

    def test_pipeline_multiple_input_files(self, tmp_dirs):
        """Reads and processes multiple input files from source directory."""
        src, dst = tmp_dirs
        for i in range(3):
            _write_records(src, f"file{i}.json", [{"seq": i, "val": f"v{i}"}])

        config = load_pipeline_from_yaml(textwrap.dedent(f"""
            pipeline:
              name: multi-file
              source:
                type: local
                path: {src}
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: {dst}
        """))

        executor = PipelineExecutor()
        result = executor.batch_run(config)

        assert result.status.value == "success"
        assert result.records_in == 3

    def test_dry_run_does_not_write_to_sink(self, tmp_dirs):
        """dry_run=True reads and transforms but does not write to sink."""
        src, dst = tmp_dirs
        _write_records(src, "data.json", [{"x": 1}])

        config = load_pipeline_from_yaml(textwrap.dedent(f"""
            pipeline:
              name: dry-run-test
              source:
                type: local
                path: {src}
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: {dst}
        """))

        executor = PipelineExecutor()
        result = executor.dry_run(config)

        # dry_run returns {"valid": bool, "issues": list}
        assert isinstance(result, dict)
        assert result["valid"] is True
        assert result["issues"] == []
        # No files should be written to sink in dry run
        assert list(dst.iterdir()) == []

    def test_empty_source_directory(self, tmp_dirs):
        """Empty source directory produces successful 0-record run."""
        src, dst = tmp_dirs

        config = load_pipeline_from_yaml(textwrap.dedent(f"""
            pipeline:
              name: empty-source
              source:
                type: local
                path: {src}
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: {dst}
        """))

        executor = PipelineExecutor()
        result = executor.batch_run(config)

        assert result.status.value == "success"
        assert result.records_in == 0
        assert result.records_out == 0
