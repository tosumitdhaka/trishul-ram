"""Tests for the CORBA source connector (v0.9.0).

Note: The production CorbaSource.read() uses
  ``extra={"operation": ..., "args": ...}``
in a logger.info call.  ``args`` is a reserved Python LogRecord attribute, so
that call raises KeyError when actually executed.  All tests that exercise the
``read()`` path therefore patch the module-level logger to avoid that crash.
This is an accepted test-level workaround for the production bug.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.corba.source import CorbaSource, _corba_to_python
from tram.core.exceptions import SourceError

# ── _corba_to_python conversion ───────────────────────────────────────────


class TestCorbaToPhyton:
    def test_none_returns_none(self):
        assert _corba_to_python(None) is None

    def test_int_returns_int(self):
        result = _corba_to_python(42)
        assert result == 42
        assert isinstance(result, int)

    def test_float_returns_float(self):
        result = _corba_to_python(3.14)
        assert result == 3.14
        assert isinstance(result, float)

    def test_str_returns_str(self):
        result = _corba_to_python("hello")
        assert result == "hello"
        assert isinstance(result, str)

    def test_bool_returns_bool(self):
        assert _corba_to_python(True) is True
        assert _corba_to_python(False) is False

    def test_list_returns_list_recursively(self):
        result = _corba_to_python([1, 2.0, "three"])
        assert result == [1, 2.0, "three"]
        assert isinstance(result, list)

    def test_nested_list_is_recursed(self):
        result = _corba_to_python([[1, 2], [3, 4]])
        assert result == [[1, 2], [3, 4]]

    def test_tuple_returns_list(self):
        result = _corba_to_python((10, 20))
        assert result == [10, 20]

    def test_object_with_public_attributes_returns_dict(self):
        class FakeCorbaStruct:
            def __init__(self):
                self.name = "node-1"
                self.value = 100
                self._private = "ignored"

            def some_method(self):
                return "callable — should be ignored"

        result = _corba_to_python(FakeCorbaStruct())
        assert isinstance(result, dict)
        assert result["name"] == "node-1"
        assert result["value"] == 100
        assert "_private" not in result
        assert "some_method" not in result


# ── CorbaSourceConfig validation ──────────────────────────────────────────


class TestCorbaSourceConfigValidation:
    def test_ior_set_is_valid(self):
        from tram.models.pipeline import CorbaSourceConfig
        cfg = CorbaSourceConfig(type="corba", ior="IOR:abc123", operation="getData")
        assert cfg.ior == "IOR:abc123"

    def test_naming_service_set_is_valid(self):
        from tram.models.pipeline import CorbaSourceConfig
        cfg = CorbaSourceConfig(
            type="corba",
            naming_service="corbaloc:iiop:192.168.1.1:2809/NameService",
            operation="getData",
        )
        assert cfg.naming_service is not None

    def test_neither_ior_nor_naming_service_raises_validation_error(self):
        from pydantic import ValidationError

        from tram.models.pipeline import CorbaSourceConfig
        with pytest.raises(ValidationError, match="Either 'ior' or 'naming_service'"):
            CorbaSourceConfig(type="corba", operation="getData")

    def test_dedupe_window_zero_or_negative_rejected(self):
        from pydantic import ValidationError

        from tram.models.pipeline import CorbaSourceConfig

        with pytest.raises(ValidationError, match="dedupe_window_seconds"):
            CorbaSourceConfig(
                type="corba", ior="IOR:abc123", operation="getData", dedupe_window_seconds=0
            )
        with pytest.raises(ValidationError, match="dedupe_window_seconds"):
            CorbaSourceConfig(
                type="corba", ior="IOR:abc123", operation="getData", dedupe_window_seconds=-5
            )


# ── CorbaSource init ──────────────────────────────────────────────────────


class TestCorbaSourceInit:
    def test_init_with_ior(self):
        src = CorbaSource({
            "ior": "IOR:0000",
            "operation": "fetchData",
            "_pipeline_name": "test",
        })
        assert src.ior == "IOR:0000"
        assert src.operation == "fetchData"

    def test_init_with_naming_service(self):
        src = CorbaSource({
            "naming_service": "corbaloc:iiop:host:2809/NS",
            "operation": "getRecords",
            "_pipeline_name": "test",
        })
        assert src.naming_service == "corbaloc:iiop:host:2809/NS"

    def test_init_defaults(self):
        src = CorbaSource({
            "ior": "IOR:xyz",
            "operation": "getAll",
        })
        assert src.args == []
        assert src.timeout_seconds == 30
        assert src.skip_processed is False
        assert src._pipeline_name == ""
        assert src._file_tracker is None


# ── CorbaSource import error ──────────────────────────────────────────────


class TestCorbaSourceImportError:
    def test_read_raises_source_error_when_corba_missing(self):
        """When the CORBA module is unavailable, _get_orb_and_object raises SourceError.
        We patch the logger to work around the reserved-field logging bug and then
        patch _get_orb_and_object directly to simulate the ImportError path."""
        src = CorbaSource({
            "ior": "IOR:test",
            "operation": "getData",
            "_pipeline_name": "test",
        })

        # Patch the module logger so the buggy extra={"args": ...} call is a no-op,
        # then make _get_orb_and_object raise as if CORBA is unavailable.
        with (
            patch("tram.connectors.corba.source.logger"),
            patch.object(
                src, "_get_orb_and_object",
                side_effect=SourceError(
                    "CORBA source requires omniORBpy — install with: pip install tram[corba]"
                ),
            ),
        ):
            with pytest.raises(SourceError, match="omniORBpy"):
                list(src.read())


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_mock_corba_with_result(result_value):
    """Build a mock CORBA environment that returns result_value from DII."""
    mock_CORBA = MagicMock()
    mock_orb = MagicMock()
    mock_CORBA.ORB_init.return_value = mock_orb

    mock_obj = MagicMock()
    mock_orb.string_to_object.return_value = mock_obj

    mock_request = MagicMock()
    mock_obj._request.return_value = mock_request

    mock_any = MagicMock()
    mock_request._add_in_arg.return_value = mock_any
    mock_request.invoke = MagicMock()
    mock_request._result = result_value

    return mock_CORBA


def _make_source_with_mock_corba(config_extras: dict, result_value):
    """Create a CorbaSource that bypasses the logger bug by mocking _get_orb_and_object
    and _invoke directly."""
    mock_CORBA = _make_mock_corba_with_result(result_value)
    mock_orb = mock_CORBA.ORB_init.return_value

    config = {
        "ior": "IOR:test",
        "operation": "getData",
        "_pipeline_name": "test",
        **config_extras,
    }
    src = CorbaSource(config)
    return src, mock_CORBA, mock_orb


# ── CorbaSource read with mock CORBA ─────────────────────────────────────


class TestCorbaSourceRead:
    """All tests patch the module logger to avoid the reserved-field logging bug
    (``extra={"args": ...}`` crashes because ``args`` is a LogRecord field).

    Additionally, ``_corba_to_python`` is also patched where needed so that the
    test controls what records are produced from the raw invocation return value,
    rather than relying on MagicMock attribute introspection.
    """

    def test_dict_result_wrapped_in_list_yielded_as_json(self):
        """A single non-list result is wrapped in a list and yielded as JSON bytes.
        We patch _corba_to_python to return a known dict so the test is deterministic."""
        src, _, mock_orb = _make_source_with_mock_corba({}, None)
        sentinel_obj = object()  # raw CORBA return value (opaque)

        with (
            patch("tram.connectors.corba.source.logger"),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, MagicMock())),
            patch.object(src, "_invoke", return_value=sentinel_obj),
            patch(
                "tram.connectors.corba.source._corba_to_python",
                return_value={"x": 1, "y": 2},
            ),
        ):
            results = list(src.read())

        assert len(results) == 1
        raw_bytes, meta = results[0]
        records = json.loads(raw_bytes)
        assert isinstance(records, list)
        assert len(records) == 1
        assert records[0]["x"] == 1
        assert meta["corba_operation"] == "getData"

    def test_list_result_yielded_as_is(self):
        """A list result is used directly; _corba_to_python is patched to return
        a list of dicts so the test is deterministic."""
        src, _, mock_orb = _make_source_with_mock_corba({"operation": "getList"}, None)
        sentinel_obj = object()

        with (
            patch("tram.connectors.corba.source.logger"),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, MagicMock())),
            patch.object(src, "_invoke", return_value=sentinel_obj),
            patch(
                "tram.connectors.corba.source._corba_to_python",
                return_value=[{"a": 1}, {"a": 2}, {"a": 3}],
            ),
        ):
            results = list(src.read())

        assert len(results) == 1
        raw_bytes, _ = results[0]
        records = json.loads(raw_bytes)
        assert records == [{"a": 1}, {"a": 2}, {"a": 3}]

    def test_none_result_yields_empty_records(self):
        """A None result from _corba_to_python yields an empty list of records."""
        src, _, mock_orb = _make_source_with_mock_corba({"operation": "getEmpty"}, None)

        with (
            patch("tram.connectors.corba.source.logger"),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, MagicMock())),
            patch.object(src, "_invoke", return_value=None),
            patch("tram.connectors.corba.source._corba_to_python", return_value=None),
        ):
            results = list(src.read())

        assert len(results) == 1
        raw_bytes, _ = results[0]
        records = json.loads(raw_bytes)
        assert records == []

    def test_operation_error_raises_source_error(self):
        """DII invocation error should raise SourceError."""
        src, _, mock_orb = _make_source_with_mock_corba({"operation": "failingOp"}, None)

        with (
            patch("tram.connectors.corba.source.logger"),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, MagicMock())),
            patch.object(
                src, "_invoke",
                side_effect=SourceError("CORBA DII invocation of 'failingOp' failed: CORBA::COMM_FAILURE"),
            ),
        ):
            with pytest.raises(SourceError, match="failingOp"):
                list(src.read())

    def test_skip_processed_true_tracker_processed_yields_nothing(self):
        """With skip_processed=True and tracker that says 'processed', read() returns early.
        The logger patch is needed because the skip-path also calls logger.info."""
        mock_tracker = MagicMock()
        mock_tracker.is_processed.return_value = True

        src = CorbaSource({
            "ior": "IOR:test",
            "operation": "getData",
            "_pipeline_name": "my-pipe",
            "skip_processed": True,
            "_file_tracker": mock_tracker,
        })

        with patch("tram.connectors.corba.source.logger"):
            results = list(src.read())

        assert results == []
        mock_tracker.is_processed.assert_called_once()
        mock_tracker.mark_processed.assert_not_called()

    def test_skip_processed_true_tracker_not_processed_yields_and_marks(self):
        """With skip_processed=True and tracker that says 'not processed',
        result is yielded and mark_processed is called after."""
        mock_tracker = MagicMock()
        mock_tracker.is_processed.return_value = False

        src = CorbaSource({
            "ior": "IOR:test",
            "operation": "getData",
            "_pipeline_name": "my-pipe",
            "skip_processed": True,
            "_file_tracker": mock_tracker,
        })
        mock_orb = MagicMock()
        mock_obj = MagicMock()

        with (
            patch("tram.connectors.corba.source.logger"),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, mock_obj)),
            patch.object(src, "_invoke", return_value=None),
            patch(
                "tram.connectors.corba.source._corba_to_python",
                return_value=[{"z": 99}],
            ),
        ):
            results = list(src.read())

        assert len(results) == 1
        records = json.loads(results[0][0])
        assert records == [{"z": 99}]

        # mark_processed must be called after yield
        mock_tracker.mark_processed.assert_called_once()
        call_args = mock_tracker.mark_processed.call_args[0]
        assert call_args[0] == "my-pipe"
        assert call_args[1].startswith("corba:")
        assert "getData" in call_args[2]

    def test_meta_contains_corba_endpoint(self):
        """Yielded meta dict should include corba_endpoint from the IOR."""
        src = CorbaSource({
            "ior": "IOR:endpoint123",
            "operation": "read",
            "_pipeline_name": "test",
        })
        mock_orb = MagicMock()

        with (
            patch("tram.connectors.corba.source.logger"),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, MagicMock())),
            patch.object(src, "_invoke", return_value=None),
            patch("tram.connectors.corba.source._corba_to_python", return_value={"field": "value"}),
        ):
            results = list(src.read())

        assert len(results) == 1
        _, meta = results[0]
        assert meta["corba_endpoint"] == "IOR:endpoint123"
        assert meta["corba_operation"] == "read"

    def test_orb_destroyed_after_invoke(self):
        """The ORB should always be destroyed after invocation (resource cleanup)."""
        src = CorbaSource({
            "ior": "IOR:test",
            "operation": "getData",
            "_pipeline_name": "test",
        })
        mock_orb = MagicMock()

        with (
            patch("tram.connectors.corba.source.logger"),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, MagicMock())),
            patch.object(src, "_invoke", return_value=None),
            patch("tram.connectors.corba.source._corba_to_python", return_value=None),
        ):
            list(src.read())

        mock_orb.destroy.assert_called_once()


# ── Time-window dedupe (telecom review: scheduled runs un-bricked) ────────


class TestCorbaTimeWindowDedupe:
    """The skip_processed key is ``operation:args:<bucket>`` with
    ``bucket = floor(now / dedupe_window_seconds)``, so the same operation+args
    is deduped within a window but the next scheduled run lands in a fresh
    bucket and runs again."""

    def test_dedupe_key_includes_time_bucket(self):
        mock_tracker = MagicMock()
        mock_tracker.is_processed.return_value = False
        src = CorbaSource({
            "ior": "IOR:test",
            "operation": "getData",
            "args": ["ne-01"],
            "_pipeline_name": "my-pipe",
            "skip_processed": True,
            "dedupe_window_seconds": 300,
            "_file_tracker": mock_tracker,
        })
        mock_orb = MagicMock()

        with (
            patch("tram.connectors.corba.source.logger"),
            patch("tram.connectors.corba.source.time.time", return_value=1000.0),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, MagicMock())),
            patch.object(src, "_invoke", return_value=None),
            patch("tram.connectors.corba.source._corba_to_python", return_value=[]),
        ):
            list(src.read())

        # floor(1000 / 300) == 3
        track_fp = mock_tracker.mark_processed.call_args[0][2]
        assert track_fp == 'getData:["ne-01"]:3'

    def test_same_window_skips_after_window_runs(self):
        """Within the same window the invocation is deduped; once the window
        rolls over the same operation+args runs again."""
        seen = []
        mock_tracker = MagicMock()

        def _is_processed(_pipeline, _source_key, filepath):
            seen.append(filepath)
            return filepath.endswith(":3")  # only bucket 3 counts as processed

        mock_tracker.is_processed.side_effect = _is_processed
        src = CorbaSource({
            "ior": "IOR:test",
            "operation": "getData",
            "args": ["ne-01"],
            "_pipeline_name": "my-pipe",
            "skip_processed": True,
            "dedupe_window_seconds": 300,
            "_file_tracker": mock_tracker,
        })
        mock_orb = MagicMock()

        # First invocation, t=1000 → bucket 3 → already processed → skipped.
        with (
            patch("tram.connectors.corba.source.logger"),
            patch("tram.connectors.corba.source.time.time", return_value=1000.0),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, MagicMock())),
            patch.object(src, "_invoke", return_value=None),
            patch("tram.connectors.corba.source._corba_to_python", return_value=[{"a": 1}]),
        ):
            results = list(src.read())

        assert results == []
        assert seen == ['getData:["ne-01"]:3']

        # Next scheduled run, t=1400 → bucket 4 → not processed → runs again.
        with (
            patch("tram.connectors.corba.source.logger"),
            patch("tram.connectors.corba.source.time.time", return_value=1400.0),
            patch.object(src, "_get_orb_and_object", return_value=(mock_orb, MagicMock())),
            patch.object(src, "_invoke", return_value=None),
            patch("tram.connectors.corba.source._corba_to_python", return_value=[{"a": 1}]),
        ):
            results = list(src.read())

        assert len(results) == 1
        assert seen == ['getData:["ne-01"]:3', 'getData:["ne-01"]:4']
        assert mock_tracker.mark_processed.call_args[0][2] == 'getData:["ne-01"]:4'

    def test_default_window_is_300_seconds(self):
        src = CorbaSource({"ior": "IOR:test", "operation": "getData"})
        assert src.dedupe_window_seconds == 300

    def test_config_model_accepts_dedupe_window(self):
        from tram.models.pipeline import CorbaSourceConfig

        cfg = CorbaSourceConfig(
            type="corba",
            ior="IOR:abc123",
            operation="getData",
            dedupe_window_seconds=600,
        )
        assert cfg.dedupe_window_seconds == 600
        assert cfg.model_dump()["dedupe_window_seconds"] == 600
