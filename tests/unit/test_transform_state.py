"""Tests for the transform-state foundation (design F.1 §3.2, §8, §10 subsets).

Covers the durable state-store plumbing: DbTransformStateStore round-trips,
executor hydration/save points (batch success-only, retry re-hydration,
config-sha discard), stream periodic persistence, the mode-gating validators
(§6), and the TRAM_STATEFUL_TRANSFORMS flag.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
import time
from unittest.mock import MagicMock, patch

import pytest

from tram.core.exceptions import ConfigError
from tram.persistence.db import TramDB
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml
from tram.pipeline.state_store import (
    DbTransformStateStore,
    HttpTransformStateStore,
    TransformState,
)

_PIPELINE_YAML = """\
pipeline:
  name: state-test
  source:
    type: local
    path: /tmp/in
  serializer_in:
    type: json
  sinks:
    - type: local
      path: /tmp/out
  transforms:
    - type: counter_delta
      fields: [_metrics.ifInOctets]
{extra}
"""


def _pipeline_yaml(extra: str = "") -> str:
    return textwrap.dedent(_PIPELINE_YAML).format(extra=extra)


def _sha(yaml_text: str) -> str:
    return hashlib.sha256(yaml_text.encode()).hexdigest()[:16]


def _record(v: int, t: str = "2026-09-16T09:00:00+00:00") -> dict:
    return {"_metrics": {"ifInOctets": v}, "_index": "1", "_polled_at": t}


def _stream_yaml(persist_interval: float) -> str:
    return _pipeline_yaml(
        f"  state_persist_interval_s: {persist_interval}\n"
    )


class _ExecHarness:
    """A batch/stream runner with mocked I/O and a REAL transform build."""

    def __init__(self, store, yaml_text: str):
        self.yaml_text = yaml_text
        self.config = load_pipeline_from_yaml(yaml_text)
        self.config_sha = _sha(yaml_text)
        self.executor = PipelineExecutor(state_store=store)

    def _mocks(self):
        sink = MagicMock()
        ser_in = MagicMock()
        ser_out = MagicMock()
        ser_out.serialize.side_effect = lambda recs: json.dumps(recs).encode()
        return sink, ser_in, ser_out

    def batch(self, records, run_id="r1", config_sha=None, source=None):
        sink, ser_in, ser_out = self._mocks()
        ser_in.parse.return_value = records
        mock_source = source or MagicMock()
        if source is None:
            mock_source.read.return_value = iter(
                [(json.dumps(records).encode(), {"source_host": "h1"})]
            )
        with patch.object(self.executor, "_build_source", return_value=mock_source), \
             patch.object(self.executor, "_build_sinks", return_value=[(sink, None, [])]), \
             patch.object(self.executor, "_build_serializer_in", return_value=ser_in), \
             patch.object(self.executor, "_build_serializer_out", return_value=ser_out):
            result = self.executor.batch_run(
                self.config, run_id=run_id,
                config_sha256=config_sha if config_sha is not None else self.config_sha,
            )
        return result, ser_out

    def stream(self, records, run_id="r-s1", config_sha=None, delay=0.0):
        import threading

        sink, ser_in, ser_out = self._mocks()
        ser_in.parse.side_effect = lambda raw: json.loads(raw)

        def _gen():
            for rec in records:
                yield json.dumps([rec]).encode(), {"source_host": "h1"}
                if delay:
                    time.sleep(delay)

        mock_source = MagicMock()
        mock_source.read.return_value = iter(_gen())
        with patch.object(self.executor, "_build_source", return_value=mock_source), \
             patch.object(self.executor, "_build_sinks", return_value=[(sink, None, [])]), \
             patch.object(self.executor, "_build_serializer_in", return_value=ser_in), \
             patch.object(self.executor, "_build_serializer_out", return_value=ser_out):
            self.executor.stream_run(
                self.config, threading.Event(),
                config_sha256=config_sha if config_sha is not None else self.config_sha,
            )
        return ser_out


def _sink_records(mock_ser_out) -> list[dict]:
    """Records delivered to the sink on the last run."""
    if mock_ser_out.serialize.call_args is None:
        return []
    return mock_ser_out.serialize.call_args[0][0]


# ── Db store round-trips ────────────────────────────────────────────────────


class TestDbStateStore:
    def test_save_and_load_roundtrip(self, tmp_path):
        db = TramDB(url=f"sqlite:///{tmp_path}/state.db")
        store = DbTransformStateStore(db)
        store.put("p1", {"counter_delta:0": {"k": {"v": 5}}}, "abc123", run_id="r9")
        loaded = store.get("p1")
        assert isinstance(loaded, TransformState)
        assert loaded.state == {"counter_delta:0": {"k": {"v": 5}}}
        assert loaded.config_sha256 == "abc123"
        row = db.load_transform_state("p1")
        assert row["updated_by"] == "r9"

    def test_get_missing_returns_none(self, tmp_path):
        db = TramDB(url=f"sqlite:///{tmp_path}/state2.db")
        store = DbTransformStateStore(db)
        assert store.get("nope") is None


# ── Executor batch hydration/save points (§3.2c) ────────────────────────────


class TestBatchState:
    def test_state_roundtrip_two_runs(self, tmp_path):
        """Consecutive batch runs produce a correct second delta via the store."""
        db = TramDB(url=f"sqlite:///{tmp_path}/rt.db")
        store = DbTransformStateStore(db)
        yaml_text = _pipeline_yaml()

        h1 = _ExecHarness(store, yaml_text)
        result1, ser1 = h1.batch([_record(1000)])
        assert result1.status.value == "success"
        assert _sink_records(ser1)[0]["_metrics"]["ifInOctets_delta"] is None

        # Run 2 uses a fresh executor + transforms; hydration replays run 1's
        # saved state, so the first sample computes a real delta.
        h2 = _ExecHarness(store, yaml_text)
        result2, ser2 = h2.batch([_record(1100, t="2026-09-16T09:05:00+00:00")])
        assert result2.status.value == "success"
        assert _sink_records(ser2)[0]["_metrics"]["ifInOctets_delta"] == 100

    def test_failed_run_persists_nothing(self, tmp_path):
        """A failed batch run must not PUT state (retry re-hydrates the snapshot)."""
        db = TramDB(url=f"sqlite:///{tmp_path}/fail.db")
        store = DbTransformStateStore(db)
        yaml_text = _pipeline_yaml("  on_error: retry\n  retry_count: 1\n  retry_delay_seconds: 0\n")

        # Pre-populate a snapshot (v=1000) the retry must re-hydrate from.
        h0 = _ExecHarness(store, yaml_text)
        result0, _ = h0.batch([_record(1000)])
        assert result0.status.value == "success"

        h = _ExecHarness(store, yaml_text)

        class _Src:
            def __init__(self, gen):
                self._gen = gen

            def read(self):
                return self._gen

        def _failing_gen():
            yield json.dumps([_record(1500)]).encode(), {"source_host": "h1"}
            from tram.core.exceptions import TramError
            raise TramError("source exploded")

        def _good_gen():
            yield json.dumps([_record(1100, t="2026-09-16T09:10:00+00:00")]).encode(), \
                {"source_host": "h1"}

        ser_in = MagicMock()
        ser_out = MagicMock()
        ser_in.parse.side_effect = lambda raw: json.loads(raw)
        ser_out.serialize.side_effect = lambda recs: json.dumps(recs).encode()
        with patch.object(h.executor, "_build_source", side_effect=[_Src(_failing_gen()), _Src(_good_gen())]), \
             patch.object(h.executor, "_build_sinks", return_value=[(MagicMock(), None, [])]), \
             patch.object(h.executor, "_build_serializer_in", return_value=ser_in), \
             patch.object(h.executor, "_build_serializer_out", return_value=ser_out):
            with patch.object(db, "save_transform_state", wraps=db.save_transform_state) as spy:
                result = h.executor.batch_run(h.config, run_id="r-retry", config_sha256=h.config_sha)

        assert result.status.value == "success"
        # The failed attempt mutated transform state to v=1500, but the rebuild
        # re-hydrated from the in-run snapshot (v=1000) → delta 100, not a
        # reset/skip. Exactly one PUT, after the successful attempt.
        spy.assert_called_once()
        # save_transform_state takes (pipeline, state dict, sha, updated_by)
        state_arg = spy.call_args[0][1]
        identity = next(iter(state_arg["counter_delta:0"]))
        assert state_arg["counter_delta:0"][identity]["v"] == 1100

    def test_config_sha_mismatch_discards_state(self, tmp_path):
        """Hydration discards the blob on config-sha mismatch → first sight."""
        db = TramDB(url=f"sqlite:///{tmp_path}/sha.db")
        store = DbTransformStateStore(db)
        store.put("state-test", {"counter_delta:0": {"k": {"v": 1000}}}, "oldsha")

        h = _ExecHarness(store, _pipeline_yaml())  # config_sha differs from oldsha
        assert h.config_sha != "oldsha"
        result, ser = h.batch([_record(1100)])
        assert result.status.value == "success"
        # State discarded → first sight → null delta.
        assert _sink_records(ser)[0]["_metrics"]["ifInOctets_delta"] is None
        # The run re-primes the row under the current config sha.
        assert db.load_transform_state("state-test")["config_sha256"] == h.config_sha

    def test_state_not_persisted_without_stateful_transforms(self, tmp_path):
        """Pipelines without stateful transforms never touch the state store."""
        db = TramDB(url=f"sqlite:///{tmp_path}/nostate.db")
        store = DbTransformStateStore(db)
        yaml_text = textwrap.dedent("""
            pipeline:
              name: plain-test
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              sinks:
                - type: local
                  path: /tmp/out
        """)
        h = _ExecHarness(store, yaml_text)
        result, _ = h.batch([{"x": 1}])
        assert result.status.value == "success"
        assert db.load_transform_state("plain-test") is None


# ── Stream periodic persistence (§3.2c) ─────────────────────────────────────


class TestStreamState:
    def test_stream_persist_interval_hydrates_redispatch(self, tmp_path):
        """Snapshot at t, redispatch at t+Δ → the first delta is correct from it."""
        db = TramDB(url=f"sqlite:///{tmp_path}/stream.db")
        store = DbTransformStateStore(db)
        yaml_text = _stream_yaml(0.05)

        # Run 1: three samples ~60ms apart → periodic PUTs fire mid-run.
        h1 = _ExecHarness(store, yaml_text)
        records1 = [
            _record(1000, "2026-09-16T09:00:00+00:00"),
            _record(1050, "2026-09-16T09:00:05+00:00"),
            _record(1080, "2026-09-16T09:00:10+00:00"),
        ]
        with patch.object(db, "save_transform_state", wraps=db.save_transform_state) as spy:
            h1.stream(records1, delay=0.06)
        # Periodic + final save: at least one persist happened mid-run.
        assert spy.call_count >= 2

        # Run 2: a redispatch hydrates from run 1's final snapshot (v=1080).
        h2 = _ExecHarness(store, yaml_text)
        ser2 = h2.stream([_record(1100, "2026-09-16T09:00:15+00:00")])
        assert _sink_records(ser2)[0]["_metrics"]["ifInOctets_delta"] == 20

    def test_stream_default_no_periodic_persist(self, tmp_path):
        """persist_interval_s default (0) → only the run-end save."""
        db = TramDB(url=f"sqlite:///{tmp_path}/stream2.db")
        store = DbTransformStateStore(db)
        h = _ExecHarness(store, _pipeline_yaml())
        with patch.object(db, "save_transform_state", wraps=db.save_transform_state) as spy:
            h.stream([_record(1000)])
        assert spy.call_count == 1


# ── Controller: update/delete delete the state row (§3.2d) ──────────────────


class TestControllerStateRow:
    def _make_controller(self, tmp_path):
        from tram.pipeline.controller import PipelineController
        db = TramDB(url=f"sqlite:///{tmp_path}/ctrl.db")
        ctrl = PipelineController(db=db, node_id="n0")
        return ctrl, db

    def test_update_deletes_state_row(self, tmp_path):
        ctrl, db = self._make_controller(tmp_path)
        yaml_text = _pipeline_yaml()
        config = load_pipeline_from_yaml(yaml_text)
        ctrl.register(config, yaml_text, source="api")
        db.save_transform_state("state-test", {"counter_delta:0": {}}, _sha(yaml_text))
        assert db.load_transform_state("state-test") is not None

        new_yaml = _pipeline_yaml("  description: changed\n")
        ctrl.update("state-test", new_yaml)
        assert db.load_transform_state("state-test") is None

    def test_delete_deletes_state_row(self, tmp_path):
        ctrl, db = self._make_controller(tmp_path)
        yaml_text = _pipeline_yaml()
        config = load_pipeline_from_yaml(yaml_text)
        ctrl.register(config, yaml_text, source="api")
        db.save_transform_state("state-test", {"counter_delta:0": {}}, _sha(yaml_text))
        ctrl.delete("state-test")
        assert db.load_transform_state("state-test") is None


# ── HTTP store (worker mode) ────────────────────────────────────────────────


class TestHttpStateStore:
    def test_get_uses_api_key_and_parses_payload(self):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-API-Key"] == "secret"
            return httpx.Response(200, json={
                "state": {"counter_delta:0": {"k": {"v": 1}}},
                "config_sha256": "abc",
            })

        store = HttpTransformStateStore(
            "http://mgr:8765", "secret", transport=httpx.MockTransport(handler)
        )
        loaded = store.get("pipe")
        assert loaded == TransformState({"counter_delta:0": {"k": {"v": 1}}}, "abc")

    def test_put_failure_logged_and_swallowed(self):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("manager unreachable", request=request)

        store = HttpTransformStateStore(
            "http://mgr:8765", "secret", transport=httpx.MockTransport(handler)
        )
        store.put("pipe", {"k": 1}, "sha")  # must not raise
        assert store.get("pipe") is None  # GET failure degrades to no state

    def test_put_failure_does_not_fail_the_run(self, tmp_path):
        """A PUT failure at run end is logged and swallowed — run unaffected."""
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("manager unreachable", request=request)

        store = HttpTransformStateStore(
            "http://mgr:8765", "", transport=httpx.MockTransport(handler)
        )
        h = _ExecHarness(store, _pipeline_yaml())
        result, _ = h.batch([_record(1000)])
        assert result.status.value == "success"

    def test_get_404_is_no_state(self):
        import httpx
        store = HttpTransformStateStore(
            "http://mgr:8765", "", transport=httpx.MockTransport(lambda req: httpx.Response(404))
        )
        assert store.get("pipe") is None


# ── Mode gating (§6) and the feature flag (§9) ──────────────────────────────


class TestModeGating:
    def test_validation_rejects_thread_workers_gt_1(self):
        with pytest.raises(ConfigError, match="thread_workers"):
            load_pipeline_from_yaml(_pipeline_yaml("  thread_workers: 2\n"))

    def test_validation_rejects_stateful_in_sink_transforms(self):
        yaml_text = textwrap.dedent("""
            pipeline:
              name: state-test
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              sinks:
                - type: local
                  path: /tmp/out
                  transforms:
                    - type: counter_delta
                      fields: [x]
        """)
        with pytest.raises(ConfigError, match="sink-level transforms"):
            load_pipeline_from_yaml(yaml_text)

    def test_flag_off_disables_transforms(self, monkeypatch):
        monkeypatch.setenv("TRAM_STATEFUL_TRANSFORMS", "0")
        with pytest.raises(ConfigError, match="disabled"):
            load_pipeline_from_yaml(_pipeline_yaml())

    def test_flag_fails_open_on_unrecognized_value(self, monkeypatch):
        monkeypatch.setenv("TRAM_STATEFUL_TRANSFORMS", "banana")
        config = load_pipeline_from_yaml(_pipeline_yaml())
        assert config.transforms[0].type == "counter_delta"

    def test_flag_default_on(self, monkeypatch):
        monkeypatch.delenv("TRAM_STATEFUL_TRANSFORMS", raising=False)
        config = load_pipeline_from_yaml(_pipeline_yaml())
        assert config.transforms[0].type == "counter_delta"