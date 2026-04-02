"""Tests for Elasticsearch source and sink connectors (v0.5.0)."""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── ElasticsearchSource ────────────────────────────────────────────────────


def test_elasticsearch_source_missing_dep():
    from tram.connectors.elasticsearch.source import ElasticsearchSource
    from tram.core.exceptions import SourceError

    source = ElasticsearchSource({
        "type": "elasticsearch",
        "hosts": ["http://localhost:9200"],
        "index": "my-index",
    })

    with patch.dict(sys.modules, {"elasticsearch": None}):
        with pytest.raises(SourceError, match="elasticsearch"):
            list(source.read())


def test_elasticsearch_source_config_defaults():
    from tram.connectors.elasticsearch.source import ElasticsearchSource

    source = ElasticsearchSource({
        "type": "elasticsearch",
        "hosts": ["http://localhost:9200"],
        "index": "logs",
    })
    assert source.query == {"match_all": {}}
    assert source.scroll == "2m"
    assert source.batch_size == 500
    assert source.verify_certs is True


def test_elasticsearch_source_reads_via_scroll():
    from tram.connectors.elasticsearch.source import ElasticsearchSource

    source = ElasticsearchSource({
        "type": "elasticsearch",
        "hosts": ["http://localhost:9200"],
        "index": "test",
    })

    mock_es = MagicMock()
    mock_es.search.return_value = {
        "_scroll_id": "scroll1",
        "hits": {"hits": [{"_source": {"a": 1}}, {"_source": {"b": 2}}]},
    }
    mock_es.scroll.return_value = {
        "_scroll_id": "scroll1",
        "hits": {"hits": []},  # empty = done
    }
    mock_es.clear_scroll.return_value = {}

    mock_module = MagicMock()
    mock_module.Elasticsearch.return_value = mock_es

    with patch.dict(sys.modules, {"elasticsearch": mock_module}):
        results = list(source.read())

    assert len(results) == 1
    body, meta = results[0]
    records = json.loads(body)
    assert records == [{"a": 1}, {"b": 2}]
    assert meta["index"] == "test"


# ── ElasticsearchSink ──────────────────────────────────────────────────────


def test_elasticsearch_sink_missing_dep():
    from tram.connectors.elasticsearch.sink import ElasticsearchSink
    from tram.core.exceptions import SinkError

    sink = ElasticsearchSink({
        "type": "elasticsearch",
        "hosts": ["http://localhost:9200"],
        "index_template": "my-index",
    })

    with patch.dict(sys.modules, {"elasticsearch": None}):
        with pytest.raises(SinkError, match="elasticsearch"):
            sink.write(b'[{"a":1}]', {})


def test_elasticsearch_sink_config_defaults():
    from tram.connectors.elasticsearch.sink import ElasticsearchSink

    sink = ElasticsearchSink({
        "type": "elasticsearch",
        "hosts": ["http://localhost:9200"],
        "index_template": "my-index",
    })
    assert sink.chunk_size == 500
    assert sink.refresh == "false"
    assert sink.id_field is None


def test_elasticsearch_sink_calls_bulk():
    from tram.connectors.elasticsearch.sink import ElasticsearchSink

    sink = ElasticsearchSink({
        "type": "elasticsearch",
        "hosts": ["http://localhost:9200"],
        "index_template": "test-{pipeline}",
    })

    records = [{"msg": "hello"}]
    data = json.dumps(records).encode()

    mock_es_instance = MagicMock()
    mock_helpers = MagicMock()
    mock_module = MagicMock()
    mock_module.Elasticsearch.return_value = mock_es_instance
    mock_module.helpers = mock_helpers

    with patch.dict(sys.modules, {"elasticsearch": mock_module}):
        sink.write(data, {"pipeline_name": "mypipe"})

    mock_helpers.bulk.assert_called_once()
    call_kwargs = mock_helpers.bulk.call_args
    actions = call_kwargs[0][1]
    assert len(actions) == 1
    assert "test-mypipe" in actions[0]["_index"]


def test_elasticsearch_sink_uses_id_field():
    from tram.connectors.elasticsearch.sink import ElasticsearchSink

    sink = ElasticsearchSink({
        "type": "elasticsearch",
        "hosts": ["http://localhost:9200"],
        "index_template": "test",
        "id_field": "doc_id",
    })

    records = [{"doc_id": "abc123", "val": 1}]
    data = json.dumps(records).encode()

    mock_module = MagicMock()
    mock_module.Elasticsearch.return_value = MagicMock()

    with patch.dict(sys.modules, {"elasticsearch": mock_module}):
        sink.write(data, {})

    call_kwargs = mock_module.helpers.bulk.call_args
    actions = call_kwargs[0][1]
    assert actions[0]["_id"] == "abc123"
