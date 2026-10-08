"""Unit tests for internal worker-to-manager callbacks."""
from __future__ import annotations

import types
from datetime import datetime
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from tram.api.routers.internal import router
from tram.persistence.db import TramDB


def _make_app():
    app = FastAPI()
    app.include_router(router)

    mock_controller = MagicMock()
    mock_stats_store = MagicMock()
    app.state.controller = mock_controller
    app.state.stats_store = mock_stats_store
    return app, mock_controller, mock_stats_store


class TestRunCompleteEndpoint:
    def test_calls_on_worker_run_complete(self):
        app, ctrl, _ = _make_app()
        client = TestClient(app)
        started_at = "2026-04-16T09:00:00+00:00"
        finished_at = "2026-04-16T09:05:00+00:00"

        resp = client.post("/api/internal/run-complete", json={
            "run_id": "abc123",
            "pipeline_name": "my-pipe",
            "worker_id": "worker-2",
            "status": "success",
            "records_in": 100,
            "records_out": 95,
            "bytes_in": 1024,
            "bytes_out": 768,
            "error": None,
            "started_at": started_at,
            "finished_at": finished_at,
        })

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctrl.on_worker_run_complete.assert_called_once_with(
            run_id="abc123",
            pipeline_name="my-pipe",
            worker_id="worker-2",
            status="success",
            records_in=100,
            records_out=95,
            records_skipped=0,
            bytes_in=1024,
            bytes_out=768,
            error=None,
            errors=[],
            started_at=datetime.fromisoformat(started_at),
            finished_at=datetime.fromisoformat(finished_at),
        )

    def test_passes_error_string(self):
        app, ctrl, _ = _make_app()
        client = TestClient(app)

        client.post("/api/internal/run-complete", json={
            "run_id": "r2",
            "pipeline_name": "p",
            "worker_id": "w0",
            "status": "error",
            "records_in": 0,
            "records_out": 0,
            "error": "something broke",
        })

        _, kwargs = ctrl.on_worker_run_complete.call_args
        assert kwargs["error"] == "something broke"
        assert kwargs["status"] == "error"

    def test_defaults_records_to_zero(self):
        app, ctrl, _ = _make_app()
        client = TestClient(app)

        # records_in / records_out are optional (default 0)
        client.post("/api/internal/run-complete", json={
            "run_id": "r3",
            "pipeline_name": "p",
            "worker_id": "w0",
            "status": "success",
        })

        _, kwargs = ctrl.on_worker_run_complete.call_args
        assert kwargs["records_in"] == 0
        assert kwargs["records_out"] == 0
        assert kwargs["bytes_in"] == 0
        assert kwargs["bytes_out"] == 0

    def test_defaults_timestamps_to_none(self):
        app, ctrl, _ = _make_app()
        client = TestClient(app)

        client.post("/api/internal/run-complete", json={
            "run_id": "r4",
            "pipeline_name": "p",
            "worker_id": "w0",
            "status": "success",
        })

        _, kwargs = ctrl.on_worker_run_complete.call_args
        assert kwargs["worker_id"] == "w0"
        assert kwargs["started_at"] is None
        assert kwargs["finished_at"] is None

    def test_not_in_openapi_schema(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        schema = client.get("/openapi.json").json()
        paths = schema.get("paths", {})
        assert "/api/internal/run-complete" not in paths
        assert "/api/internal/pipeline-stats" not in paths

    def test_attempt_payload_uses_identity_checked_path(self):
        """V18-04 §3: a payload carrying attempt_id/generation goes through
        the identity-checked controller path (the ledger commit precedes the
        200)."""
        app, ctrl, _ = _make_app()
        ctrl.on_attempt_run_complete.return_value = {"ok": True}
        client = TestClient(app)

        resp = client.post("/api/internal/run-complete", json={
            "run_id": "r1",
            "pipeline_name": "p",
            "worker_id": "w0",
            "status": "success",
            "records_in": 1,
            "records_out": 1,
            "attempt_id": "r1-a1",
            "generation": 3,
        })

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctrl.on_attempt_run_complete.assert_called_once_with(
            attempt_id="r1-a1",
            generation=3,
            run_id="r1",
            pipeline_name="p",
            worker_id="w0",
            status="success",
            records_in=1,
            records_out=1,
            records_skipped=0,
            bytes_in=0,
            bytes_out=0,
            error=None,
            errors=[],
            started_at=None,
            finished_at=None,
        )
        ctrl.on_worker_run_complete.assert_not_called()

    def test_legacy_payload_keeps_legacy_path(self):
        """A payload without attempt_id keeps today's path — the controller's
        legacy completion method is the only call."""
        app, ctrl, _ = _make_app()
        client = TestClient(app)

        client.post("/api/internal/run-complete", json={
            "run_id": "r2",
            "pipeline_name": "p",
            "worker_id": "w0",
            "status": "success",
        })

        ctrl.on_worker_run_complete.assert_called_once()
        ctrl.on_attempt_run_complete.assert_not_called()

    def test_attempt_payload_propagates_ignored_ack(self):
        """A mismatched/unknown attempt is acked with an 'ignored' marker (the
        worker stops retrying) without any ledger commit."""
        app, ctrl, _ = _make_app()
        ctrl.on_attempt_run_complete.return_value = {
            "ok": True, "ignored": "identity_mismatch",
        }
        client = TestClient(app)

        resp = client.post("/api/internal/run-complete", json={
            "run_id": "r3",
            "pipeline_name": "p",
            "status": "success",
            "attempt_id": "other-a1",
            "generation": 1,
        })

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "ignored": "identity_mismatch"}
        ctrl.on_worker_run_complete.assert_not_called()


class TestPipelineStatsEndpoint:
    def test_updates_stats_store_for_periodic_report(self):
        app, _, store = _make_app()
        client = TestClient(app)

        resp = client.post("/api/internal/pipeline-stats", json={
            "worker_id": "w0",
            "pipeline_name": "pipe-a",
            "run_id": "run-1",
            "schedule_type": "stream",
            "uptime_seconds": 10.5,
            "timestamp": "2026-04-17T12:00:00+00:00",
            "records_in": 5,
            "records_out": 4,
            "bytes_in": 100,
            "bytes_out": 80,
        })

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        store.update.assert_called_once()
        store.remove.assert_not_called()
        app.state.controller.on_pipeline_stats.assert_called_once()

    def test_removes_stats_store_entry_for_final_report(self):
        app, _, store = _make_app()
        client = TestClient(app)

        resp = client.post("/api/internal/pipeline-stats", json={
            "worker_id": "w0",
            "pipeline_name": "pipe-a",
            "run_id": "run-1",
            "schedule_type": "batch",
            "uptime_seconds": 3.0,
            "timestamp": "2026-04-17T12:00:00+00:00",
            "is_final": True,
        })

        assert resp.status_code == 200
        store.remove.assert_called_once_with("run-1")
        store.update.assert_not_called()
        app.state.controller.on_pipeline_stats.assert_not_called()


# ── Transform-state endpoints (F.1 §3.2b) ───────────────────────────────────


class TestTransformStateEndpoints:
    def _make_app(self, tmp_path, enabled=True):
        app = FastAPI()
        app.include_router(router)
        app.state.controller = MagicMock()
        app.state.stats_store = MagicMock()
        app.state.db = TramDB(url=f"sqlite:///{tmp_path}/internal.db")
        app.state.config = types.SimpleNamespace(stateful_transforms=enabled)
        return TestClient(app)

    def test_put_then_get_roundtrip(self, tmp_path):
        client = self._make_app(tmp_path)
        resp = client.put("/api/internal/transform-state/pipe-a", json={
            "state": {"counter_delta:0": {"k": {"v": 42}}},
            "config_sha256": "abc123",
            "run_id": "r1",
        })
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        resp = client.get("/api/internal/transform-state/pipe-a")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == {"counter_delta:0": {"k": {"v": 42}}}
        assert body["config_sha256"] == "abc123"
        # V18-06: the GET exposes the frozen §7 CAS identity additively — a
        # plain PUT row sits at revision 0 with a NULL generation.
        assert body["revision"] == 0
        assert body["generation"] is None
        # run_id lands in the audit column
        assert client.app.state.db.load_transform_state("pipe-a")["updated_by"] == "r1"

    def test_get_exposes_revision_advanced_by_checkpoint(self, tmp_path):
        """A row advanced by the checkpoint CAS is read back with the stored
        revision/generation — a worker hydrating it sends the advanced base
        instead of 0, so the fence no longer rejects a legitimate writer."""
        client = self._make_app(tmp_path)
        cp = client.post("/api/internal/checkpoint", json={
            "pipeline_name": "pipe-a",
            "generation": 3,
            "attempt_id": "run-1-a1",
            "run_id": "run-1",
            "source_unit": "local:/in/f.json:<fp>:0",
            "frontier": {"offset": 5},
            "frontier_seq": 5,
            "sink_receipts": [],
            "state": {"counter_delta:0": {"k": {"v": 1}}},
            "config_sha256": "abc123",
            "state_base_revision": 0,
        })
        assert cp.status_code == 200
        assert cp.json()["state_revision"] == 1

        body = client.get("/api/internal/transform-state/pipe-a").json()
        assert body["revision"] == 1
        assert body["generation"] == 3
        assert body["state"] == {"counter_delta:0": {"k": {"v": 1}}}

    def test_get_missing_returns_404(self, tmp_path):
        client = self._make_app(tmp_path)
        assert client.get("/api/internal/transform-state/nope").status_code == 404

    def test_flag_off_404s_both_endpoints(self, tmp_path):
        client = self._make_app(tmp_path, enabled=False)
        assert client.get("/api/internal/transform-state/pipe-a").status_code == 404
        assert client.put("/api/internal/transform-state/pipe-a", json={
            "state": {}, "config_sha256": "",
        }).status_code == 404

    def test_put_oversized_state_413(self, tmp_path, monkeypatch):
        """A state blob larger than TRAM_STATE_MAX_BYTES is rejected with 413."""
        monkeypatch.setenv("TRAM_STATE_MAX_BYTES", "100")
        client = self._make_app(tmp_path)
        resp = client.put("/api/internal/transform-state/pipe-a", json={
            "state": {"counter_delta:0": {"k" * 200: {"v": "x" * 500}}},
            "config_sha256": "abc",
        })
        assert resp.status_code == 413
        # Nothing was saved.
        assert client.get("/api/internal/transform-state/pipe-a").status_code == 404

    def test_put_oversized_state_413_config_source(self, tmp_path):
        """The cap also reads from app.state.config.state_max_bytes (the real app)."""
        client = self._make_app(tmp_path)
        client.app.state.config = types.SimpleNamespace(
            stateful_transforms=True, state_max_bytes=64
        )
        resp = client.put("/api/internal/transform-state/pipe-a", json={
            "state": {"counter_delta:0": {"k" * 100: {"v": "x" * 100}}},
        })
        assert resp.status_code == 413

    def test_put_missing_body_422(self, tmp_path):
        """A PUT with no JSON body is rejected at validation (422)."""
        client = self._make_app(tmp_path)
        assert client.put("/api/internal/transform-state/pipe-a", content=b"").status_code == 422

    def test_put_empty_state_ok(self, tmp_path):
        """An empty-but-present state dict passes the cap."""
        client = self._make_app(tmp_path)
        resp = client.put("/api/internal/transform-state/pipe-a", json={"state": {}})
        assert resp.status_code == 200

    def test_not_in_openapi_schema(self, tmp_path):
        client = self._make_app(tmp_path)
        paths = client.get("/openapi.json").json().get("paths", {})
        assert "/api/internal/transform-state/{pipeline}" not in paths


# ── Processed-files endpoints (GH #54) ──────────────────────────────────────


class TestProcessedFilesEndpoints:
    def _make_app(self, tmp_path):
        app = FastAPI()
        app.include_router(router)
        app.state.controller = MagicMock()
        app.state.stats_store = MagicMock()
        app.state.db = TramDB(url=f"sqlite:///{tmp_path}/internal.db")
        return TestClient(app)

    @staticmethod
    def _payload(pipeline_name, files):
        return {
            "pipeline_name": pipeline_name,
            "files": [{"source_key": sk, "filepath": fp} for sk, fp in files],
        }

    def test_mark_then_check_roundtrip(self, tmp_path):
        """mark → check round-trip through the manager-side tracker DB."""
        client = self._make_app(tmp_path)
        body = self._payload("pipe-a", [("local:/in", "/in/a.json")])

        resp = client.post("/api/internal/processed-files/check", json=body)
        assert resp.status_code == 200
        assert resp.json() == {"processed": [False]}

        resp = client.post("/api/internal/processed-files/mark", json=body)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        resp = client.post("/api/internal/processed-files/check", json=body)
        assert resp.status_code == 200
        assert resp.json() == {"processed": [True]}

    def test_batch_check_list_in_list_out(self, tmp_path):
        """A multi-file check returns one bool per file, aligned with input order."""
        client = self._make_app(tmp_path)
        mark_body = self._payload(
            "pipe-a", [("local:/in", "/in/a.json"), ("local:/in", "/in/b.json")]
        )
        assert client.post("/api/internal/processed-files/mark", json=mark_body).status_code == 200

        check_body = self._payload(
            "pipe-a",
            [
                ("local:/in", "/in/a.json"),
                ("local:/in", "/in/unseen.json"),
                ("local:/in", "/in/b.json"),
            ],
        )
        resp = client.post("/api/internal/processed-files/check", json=check_body)
        assert resp.status_code == 200
        assert resp.json() == {"processed": [True, False, True]}

    def test_per_pipeline_isolation(self, tmp_path):
        """Files are namespaced by pipeline_name — another pipeline sees nothing."""
        client = self._make_app(tmp_path)
        body = self._payload("pipe-a", [("local:/in", "/in/a.json")])
        assert client.post("/api/internal/processed-files/mark", json=body).status_code == 200

        other = self._payload("pipe-b", [("local:/in", "/in/a.json")])
        resp = client.post("/api/internal/processed-files/check", json=other)
        assert resp.json() == {"processed": [False]}

    def test_source_key_is_part_of_the_key(self, tmp_path):
        """The same filepath under a different source_key is a different file."""
        client = self._make_app(tmp_path)
        body = self._payload("pipe-a", [("local:/in", "/in/a.json")])
        assert client.post("/api/internal/processed-files/mark", json=body).status_code == 200

        resp = client.post(
            "/api/internal/processed-files/check",
            json=self._payload("pipe-a", [("s3:bucket/key", "/in/a.json")]),
        )
        assert resp.json() == {"processed": [False]}

    def test_db_unavailable_503(self, tmp_path):
        app = FastAPI()
        app.include_router(router)
        app.state.controller = MagicMock()
        app.state.stats_store = MagicMock()
        app.state.db = None
        client = TestClient(app)

        resp = client.post(
            "/api/internal/processed-files/check",
            json=self._payload("pipe-a", [("local:/in", "/in/a.json")]),
        )
        assert resp.status_code == 503
        resp = client.post(
            "/api/internal/processed-files/mark",
            json=self._payload("pipe-a", [("local:/in", "/in/a.json")]),
        )
        assert resp.status_code == 503

    def test_missing_body_422(self, tmp_path):
        client = self._make_app(tmp_path)
        assert client.post("/api/internal/processed-files/check", content=b"").status_code == 422
        assert client.post("/api/internal/processed-files/mark", content=b"").status_code == 422

    def test_oversized_batch_rejected_400(self, tmp_path):
        """C5 (v1.4.7): a processed-files request above the per-request bound
        is rejected with 400 — batch-friendly but bounded; the bound itself is
        still accepted."""
        from tram.api.routers.internal import _MAX_PROCESSED_FILES_PER_REQUEST

        client = self._make_app(tmp_path)
        too_big = self._payload(
            "pipe-a",
            [("local:/in", f"/in/f{i}.json") for i in range(_MAX_PROCESSED_FILES_PER_REQUEST + 1)],
        )
        assert client.post("/api/internal/processed-files/check", json=too_big).status_code == 400
        assert client.post("/api/internal/processed-files/mark", json=too_big).status_code == 400

        at_bound = self._payload(
            "pipe-a",
            [("local:/in", f"/in/f{i}.json") for i in range(_MAX_PROCESSED_FILES_PER_REQUEST)],
        )
        assert client.post("/api/internal/processed-files/check", json=at_bound).status_code == 200

    def test_not_in_openapi_schema(self, tmp_path):
        client = self._make_app(tmp_path)
        paths = client.get("/openapi.json").json().get("paths", {})
        assert "/api/internal/processed-files/check" not in paths
        assert "/api/internal/processed-files/mark" not in paths


# ── Delivery-checkpoint endpoint (V18-06 / frozen V18-01 §7) ────────────────


class TestCheckpointEndpoint:
    """POST /api/internal/checkpoint — the atomic delivery checkpoint.

    One manager-DB transaction writes the delivery_checkpoints row (monotonic
    frontier upsert) AND the generation-/revision-fenced transform_state CAS.
    """

    def _make_app(self, tmp_path):
        app = FastAPI()
        app.include_router(router)
        app.state.controller = MagicMock()
        app.state.stats_store = MagicMock()
        app.state.db = TramDB(url=f"sqlite:///{tmp_path}/internal.db")
        return TestClient(app)

    @staticmethod
    def _payload(
        pipeline="pipe-a",
        unit="local:/in/f.json:<fp>:0",
        seq=5,
        base_rev=0,
        gen=1,
        attempt="run-1-a1",
        run_id="run-1",
        state=None,
        frontier=None,
    ):
        return {
            "pipeline_name": pipeline,
            "generation": gen,
            "attempt_id": attempt,
            "run_id": run_id,
            "source_unit": unit,
            "frontier": frontier if frontier is not None else {"offset": seq},
            "frontier_seq": seq,
            "sink_receipts": [
                {"sink_key": "sftp", "tier": "fsynced_local", "confirmed": True},
            ],
            "state": state if state is not None else {"t:0": {"k": "v"}},
            "config_sha256": "abc123",
            "state_base_revision": base_rev,
        }

    @staticmethod
    def _fetch(db, sql, params=None):
        with db._engine.connect() as conn:
            row = conn.execute(text(sql), params or {}).mappings().fetchone()
        return dict(row) if row is not None else None

    def test_first_commit_writes_checkpoint_and_state_together(self, tmp_path):
        """A fresh pipeline's first checkpoint commits BOTH the
        delivery_checkpoints row and the transform_state row (revision 1,
        generation adopted) in one place."""
        client = self._make_app(tmp_path)
        resp = client.post("/api/internal/checkpoint", json=self._payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["already_committed"] is False
        assert body["state_revision"] == 1
        assert body["checkpoint_id"]

        db = client.app.state.db
        cp = self._fetch(
            db,
            "SELECT checkpoint_id, pipeline_name, generation, source_unit, "
            "frontier_seq, state_revision FROM delivery_checkpoints "
            "WHERE pipeline_name = 'pipe-a' AND source_unit = :source_unit",
            {"source_unit": "local:/in/f.json:<fp>:0"},
        )
        assert cp is not None
        assert cp["checkpoint_id"] == body["checkpoint_id"]
        assert cp["generation"] == 1
        assert cp["frontier_seq"] == 5
        assert cp["state_revision"] == 1
        ts = self._fetch(
            db,
            "SELECT generation, revision, config_sha256 FROM transform_state "
            "WHERE pipeline_name = 'pipe-a'",
        )
        assert ts == {"generation": 1, "revision": 1, "config_sha256": "abc123"}

    def test_repeat_returns_already_committed_with_committed_revision(self, tmp_path):
        """A repeat for the same (pipeline_name, source_unit) with an
        older/equal frontier returns the stored id, already_committed: true,
        and the committed state revision — without advancing the state."""
        client = self._make_app(tmp_path)
        first = client.post("/api/internal/checkpoint", json=self._payload()).json()
        second = client.post("/api/internal/checkpoint", json=self._payload())

        assert second.status_code == 200
        body = second.json()
        assert body["already_committed"] is True
        assert body["checkpoint_id"] == first["checkpoint_id"]
        assert body["state_revision"] == 1

        db = client.app.state.db
        ts = self._fetch(db, "SELECT revision FROM transform_state WHERE pipeline_name = 'pipe-a'")
        assert ts == {"revision": 1}  # a duplicate never advances the revision

    def test_advancing_frontier_keeps_identity_and_bumps_revision(self, tmp_path):
        """A higher frontier_seq upserts in place: the first-minted
        checkpoint_id survives and the state revision advances (base 1 → 2)."""
        client = self._make_app(tmp_path)
        first = client.post("/api/internal/checkpoint", json=self._payload()).json()
        # An older frontier is rejected as already-committed.
        stale = client.post(
            "/api/internal/checkpoint",
            json=self._payload(seq=3, frontier={"offset": 3}),
        ).json()
        assert stale["already_committed"] is True
        assert stale["checkpoint_id"] == first["checkpoint_id"]
        # The writer knows revision 1 now; advancing the frontier commits.
        advanced = client.post(
            "/api/internal/checkpoint",
            json=self._payload(seq=9, frontier={"offset": 9}, base_rev=1),
        )
        assert advanced.status_code == 200
        body = advanced.json()
        assert body["already_committed"] is False
        assert body["checkpoint_id"] == first["checkpoint_id"]
        assert body["state_revision"] == 2

        db = client.app.state.db
        cp = self._fetch(
            db,
            "SELECT frontier_seq, state_revision FROM delivery_checkpoints "
            "WHERE pipeline_name = 'pipe-a' AND source_unit = :source_unit",
            {"source_unit": "local:/in/f.json:<fp>:0"},
        )
        assert cp == {"frontier_seq": 9, "state_revision": 2}

    def test_stale_revision_writer_rejected_atomically(self, tmp_path):
        """A writer whose state fence fails is rejected with 409 and BOTH the
        checkpoint row and the state advance roll back (atomicity)."""
        client = self._make_app(tmp_path)
        assert client.post("/api/internal/checkpoint", json=self._payload()).status_code == 200
        # A NEW unit with a stale base revision: the checkpoint upsert would
        # insert, but the CAS fence (revision 1 != base 0) rejects it.
        resp = client.post(
            "/api/internal/checkpoint",
            json=self._payload(unit="local:/in/g.json:<fp2>:0", seq=7, base_rev=0),
        )
        assert resp.status_code == 409

        db = client.app.state.db
        # The new unit's checkpoint row was rolled back.
        assert self._fetch(
            db,
            "SELECT 1 AS x FROM delivery_checkpoints "
            "WHERE pipeline_name = 'pipe-a' AND source_unit = :source_unit",
            {"source_unit": "local:/in/g.json:<fp2>:0"},
        ) is None
        # The committed unit's row is untouched.
        cp = self._fetch(
            db,
            "SELECT frontier_seq, state_revision FROM delivery_checkpoints "
            "WHERE pipeline_name = 'pipe-a' AND source_unit = :source_unit",
            {"source_unit": "local:/in/f.json:<fp>:0"},
        )
        assert cp == {"frontier_seq": 5, "state_revision": 1}
        # The transform_state revision did not advance.
        ts = self._fetch(db, "SELECT revision FROM transform_state WHERE pipeline_name = 'pipe-a'")
        assert ts == {"revision": 1}

    def test_foreign_generation_writer_rejected(self, tmp_path):
        """A writer under a different generation is rejected by the CAS fence
        ((generation IS NULL OR generation = :gen) fails) and nothing is
        written."""
        client = self._make_app(tmp_path)
        assert client.post("/api/internal/checkpoint", json=self._payload()).status_code == 200
        resp = client.post(
            "/api/internal/checkpoint",
            json=self._payload(unit="local:/in/h.json:<fp3>:0", gen=2, base_rev=1),
        )
        assert resp.status_code == 409

        db = client.app.state.db
        assert self._fetch(
            db,
            "SELECT 1 AS x FROM delivery_checkpoints "
            "WHERE pipeline_name = 'pipe-a' AND source_unit = :source_unit",
            {"source_unit": "local:/in/h.json:<fp3>:0"},
        ) is None
        ts = self._fetch(db, "SELECT generation, revision FROM transform_state WHERE pipeline_name = 'pipe-a'")
        assert ts == {"generation": 1, "revision": 1}

    def test_legacy_generation_null_row_adopted_by_first_checkpoint(self, tmp_path):
        """M5 legacy transform_state rows (generation NULL, revision 0) are
        adopted by the first generation-identified checkpoint writer."""
        client = self._make_app(tmp_path)
        db = client.app.state.db
        db.save_transform_state("pipe-a", {"old": "blob"}, "legacy-sha", updated_by="r0")

        resp = client.post("/api/internal/checkpoint", json=self._payload())
        assert resp.status_code == 200
        assert resp.json()["state_revision"] == 1

        ts = self._fetch(db, "SELECT generation, revision FROM transform_state WHERE pipeline_name = 'pipe-a'")
        assert ts == {"generation": 1, "revision": 1}

    def test_db_unavailable_503(self, tmp_path):
        app = FastAPI()
        app.include_router(router)
        app.state.controller = MagicMock()
        app.state.stats_store = MagicMock()
        app.state.db = None
        client = TestClient(app)
        assert client.post("/api/internal/checkpoint", json=self._payload()).status_code == 503

    def test_missing_body_422(self, tmp_path):
        client = self._make_app(tmp_path)
        assert client.post("/api/internal/checkpoint", content=b"").status_code == 422

    def test_not_in_openapi_schema(self, tmp_path):
        client = self._make_app(tmp_path)
        paths = client.get("/openapi.json").json().get("paths", {})
        assert "/api/internal/checkpoint" not in paths
