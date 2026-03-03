"""Tests for SQL source and sink connectors."""
from __future__ import annotations
import json
import sys
from unittest.mock import MagicMock, patch
import pytest
from tram.connectors.sql.source import SqlSource
from tram.connectors.sql.sink import SqlSink
from tram.core.exceptions import SinkError, SourceError


class TestSqlSource:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"sqlalchemy": None}):
            source = SqlSource({"connection_url": "sqlite:///:memory:", "query": "SELECT 1"})
            with pytest.raises(SourceError, match="sqlalchemy"):
                list(source.read())

    def test_query_returns_rows(self):
        mock_sqlalchemy = MagicMock()
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.keys.return_value = ["id", "name"]
        mock_result.fetchall.return_value = [(1, "alice"), (2, "bob")]
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()
        mock_sqlalchemy.create_engine.return_value = mock_engine
        mock_sqlalchemy.text = MagicMock(side_effect=lambda q: q)

        with patch.dict(sys.modules, {"sqlalchemy": mock_sqlalchemy}):
            source = SqlSource({"connection_url": "sqlite:///:memory:", "query": "SELECT id, name FROM users"})
            results = list(source.read())

        assert len(results) == 1
        data = json.loads(results[0][0])
        assert data == [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]

    def test_chunk_size_yields_multiple_items(self):
        mock_sqlalchemy = MagicMock()
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.keys.return_value = ["id"]
        # Return 2 rows on first call, empty on second
        mock_result.fetchmany.side_effect = [[(1,), (2,)], []]
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()
        mock_sqlalchemy.create_engine.return_value = mock_engine
        mock_sqlalchemy.text = MagicMock(side_effect=lambda q: q)

        with patch.dict(sys.modules, {"sqlalchemy": mock_sqlalchemy}):
            source = SqlSource({"connection_url": "sqlite:///:memory:", "query": "SELECT id FROM t", "chunk_size": 2})
            results = list(source.read())

        assert len(results) == 1
        data = json.loads(results[0][0])
        assert data == [{"id": 1}, {"id": 2}]

    def test_engine_creation_failure_raises_source_error(self):
        mock_sqlalchemy = MagicMock()
        mock_sqlalchemy.create_engine.side_effect = Exception("bad url")

        with patch.dict(sys.modules, {"sqlalchemy": mock_sqlalchemy}):
            source = SqlSource({"connection_url": "badurl://", "query": "SELECT 1"})
            with pytest.raises(SourceError, match="bad url"):
                list(source.read())

    def test_meta_has_row_count(self):
        mock_sqlalchemy = MagicMock()
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.keys.return_value = ["x"]
        mock_result.fetchall.return_value = [(1,), (2,), (3,)]
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()
        mock_sqlalchemy.create_engine.return_value = mock_engine
        mock_sqlalchemy.text = MagicMock(side_effect=lambda q: q)

        with patch.dict(sys.modules, {"sqlalchemy": mock_sqlalchemy}):
            source = SqlSource({"connection_url": "sqlite:///:memory:", "query": "SELECT x FROM t"})
            _, meta = list(source.read())[0]

        assert meta["row_count"] == 3


class TestSqlSink:
    def _make_mock(self):
        mock_sqlalchemy = MagicMock()
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()
        mock_engine.dialect.name = "sqlite"
        mock_sqlalchemy.create_engine.return_value = mock_engine
        mock_sqlalchemy.text = MagicMock(side_effect=lambda q: q)
        mock_table = MagicMock()
        mock_insert_stmt = MagicMock()
        mock_table.insert.return_value.values.return_value = mock_insert_stmt
        mock_sqlalchemy.Table.return_value = mock_table
        mock_sqlalchemy.MetaData.return_value = MagicMock()
        return mock_sqlalchemy, mock_engine, mock_conn, mock_table

    def test_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {"sqlalchemy": None}):
            sink = SqlSink({"connection_url": "sqlite:///:memory:", "table": "users"})
            with pytest.raises(SinkError, match="sqlalchemy"):
                sink.write(b'[{"id": 1}]', {})

    def test_insert_called(self):
        mock_sa, _, mock_conn, mock_table = self._make_mock()
        with patch.dict(sys.modules, {"sqlalchemy": mock_sa,
                                       "sqlalchemy.dialects.postgresql": MagicMock(),
                                       "sqlalchemy.dialects.sqlite": MagicMock(),
                                       "sqlalchemy.dialects.mysql": MagicMock()}):
            sink = SqlSink({"connection_url": "sqlite:///:memory:", "table": "users"})
            sink.write(b'[{"id": 1, "name": "alice"}]', {})
        mock_conn.execute.assert_called_once()

    def test_empty_data_skipped(self):
        mock_sa, _, mock_conn, _ = self._make_mock()
        with patch.dict(sys.modules, {"sqlalchemy": mock_sa,
                                       "sqlalchemy.dialects.postgresql": MagicMock(),
                                       "sqlalchemy.dialects.sqlite": MagicMock(),
                                       "sqlalchemy.dialects.mysql": MagicMock()}):
            sink = SqlSink({"connection_url": "sqlite:///:memory:", "table": "users"})
            sink.write(b'[]', {})
        mock_conn.execute.assert_not_called()


class TestSqlSinkImportError:
    def test_import_error_dialects(self):
        with patch.dict(sys.modules, {
            "sqlalchemy": None,
            "sqlalchemy.dialects": None,
            "sqlalchemy.dialects.postgresql": None,
            "sqlalchemy.dialects.sqlite": None,
            "sqlalchemy.dialects.mysql": None,
        }):
            sink = SqlSink({"connection_url": "sqlite:///:memory:", "table": "t"})
            with pytest.raises(SinkError, match="sqlalchemy"):
                sink.write(b'[{"x":1}]', {})
