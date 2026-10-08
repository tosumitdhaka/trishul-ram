"""V18-01 §4/§5 worker-journal admission wiring tests (tram/agent/server.py).

Covers the authorized /agent/run admission lane (validate_and_admit → 202,
idempotent repeat → 200, conflicting identity → 409, revoked → 410,
refused auth → 401/403, admission closed → 503), the legacy-admit rollback
bridge (auto marker / off → 400), journal-first completion recording, boot
interrupt marking, and the /agent/status surface (worker_session + journal
health + watermark). Uses real temp-file journals with a fake clock, following
the tests/unit/test_worker_journal.py conventions.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import tram.core.config as cfg_mod
from tram.agent.auth_tokens import mint_start_authorization
from tram.agent.journal import WorkerJournal
from tram.agent.server import create_worker_app

_MINIMAL_YAML = """\
name: test-pipe
schedule:
  type: manual
source:
  type: local
  path: /tmp/in
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""

SECRET = "test-secret"

# The FakeClock start; tokens are minted against it so validate_and_admit
# (which uses the journal clock) sees issued_at == now.
_T0_DT = datetime(2026, 1, 1, tzinfo=UTC)
T0 = int(_T0_DT.timestamp())


class FakeClock:
    """Deterministic clock for time-dependent journal behaviour."""

    def __init__(self, start: datetime | None = None) -> None:
        self._t = start or _T0_DT

    def __call__(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)


@pytest.fixture
def journal(tmp_path):
    j = WorkerJournal(tmp_path / "journal.db", clock=FakeClock())
    yield j
    j.close()


@pytest.fixture
def auth_env(monkeypatch):
    """Pre-shared session secret so authorized dispatches validate."""
    monkeypatch.setenv("TRAM_AUTH_SESSION_SECRET", SECRET)


def _make_client(journal=None, worker_id="w0", manager_url=""):
    app = create_worker_app(
        worker_id=worker_id, manager_url=manager_url, journal=journal
    )
    return app, TestClient(app, raise_server_exceptions=True)


def _mint(
    app,
    *,
    attempt_id="a1",
    run_id="r1",
    generation=1,
    slot_id="s1",
    ttl_s=300,
    secret=SECRET,
    issued_at_unix=T0,
    worker_session=None,
) -> str:
    return mint_start_authorization(
        attempt_id=attempt_id,
        run_id=run_id,
        generation=generation,
        slot_id=slot_id,
        worker_session=worker_session or app.state.worker_session,
        ttl_s=ttl_s,
        secret=secret,
        issued_at_unix=issued_at_unix,
    )


def _dispatch(
    client,
    *,
    run_id="r1",
    yaml_text=_MINIMAL_YAML,
    pipeline_name="test-pipe",
    schedule_type="batch",
    authorization=None,
    attempt_id="",
    generation=None,
    slot_id="",
):
    body = {
        "pipeline_name": pipeline_name,
        "yaml_text": yaml_text,
        "run_id": run_id,
        "schedule_type": schedule_type,
    }
    if authorization is not None:
        body["authorization"] = authorization
    if attempt_id:
        body["attempt_id"] = attempt_id
    if generation is not None:
        body["generation"] = generation
    if slot_id:
        body["slot_id"] = slot_id
    return client.post("/agent/run", json=body)


def _mock_result(run_id="r1", status="success", records_in=2, records_out=2):
    from tram.core.context import RunResult, RunStatus

    return RunResult(
        run_id=run_id,
        pipeline_name="test-pipe",
        status=RunStatus(status),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        records_in=records_in,
        records_out=records_out,
        records_skipped=0,
        error=None,
    )


# ── Authorized admission ────────────────────────────────────────────────────


class TestAuthorizedAdmission:
    def test_202_and_thread_started_only_after_admission(self, journal, auth_env):
        """Admission succeeds → reservation exists → thread starts → running.

        The reservation must exist BEFORE the thread: while the run is blocked
        inside batch_run the journal row is already 'running' (thread_started),
        and a refused admission below never creates a thread at all.
        """
        gate = threading.Event()

        def _blocking_batch(config, run_id=None, stats=None, config_sha256="", flush=False):
            gate.wait(timeout=30)
            return _mock_result(run_id=run_id or "r1")

        from unittest.mock import patch

        app, client = _make_client(journal=journal)
        token = _mint(app, attempt_id="a1", run_id="r1")
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=_blocking_batch,
        ):
            resp = _dispatch(
                client,
                run_id="r1",
                authorization=token,
                attempt_id="a1",
                generation=1,
                slot_id="s1",
            )
            assert resp.status_code == 202
            body = resp.json()
            assert body["accepted"] is True
            assert body["run_id"] == "r1"
            assert body["attempt_id"] == "a1"
            assert body["worker_id"] == "w0"
            assert body["state"] == "running"
            # Journal reservation exists with the worker's session identity.
            rec = journal.get_attempt("a1")
            assert rec is not None and rec.kind == "active"
            assert rec.state == "running"
            assert rec.thread_started is True
            assert rec.worker_session == app.state.worker_session
            assert rec.pipeline_name == "test-pipe"
            assert rec.generation == 1
            assert rec.slot_id == "s1"
            # Status item carries the attempt identity.
            status = client.get("/agent/status").json()
            items = [i for i in status["running"] if i["run_id"] == "r1"]
            assert len(items) == 1
            assert items[0]["attempt_id"] == "a1"
            assert items[0]["generation"] == 1
            assert items[0]["slot_id"] == "s1"
            assert items[0]["legacy"] is False
            gate.set()

    def test_refused_admission_never_starts_a_thread(self, journal, auth_env):
        """401 refusal → no thread, no reservation, no ActiveRun."""
        from unittest.mock import patch

        app, client = _make_client(journal=journal)
        token = _mint(app, attempt_id="a1", run_id="r1", secret="wrong-secret")
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=AssertionError("thread must not start"),
        ):
            resp = _dispatch(client, run_id="r1", authorization=token)
        assert resp.status_code == 401
        assert resp.json()["detail"]["reason"] == "bad signature"
        assert app.state.worker.snapshot() == []
        assert journal.get_attempt("a1") is None

    def test_repeat_is_200_no_second_thread(self, journal, auth_env):
        """A repeat of the same attempt returns 200 already_admitted and never
        starts a second thread."""
        from unittest.mock import patch

        gate = threading.Event()
        started = []

        def _blocking_batch(config, run_id=None, stats=None, config_sha256="", flush=False):
            started.append(run_id)
            gate.wait(timeout=30)
            return _mock_result(run_id=run_id or "r1")

        app, client = _make_client(journal=journal)
        token = _mint(app, attempt_id="a1", run_id="r1")
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=_blocking_batch,
        ):
            resp1 = _dispatch(client, run_id="r1", authorization=token)
            assert resp1.status_code == 202
            resp2 = _dispatch(client, run_id="r1", authorization=token)
            assert resp2.status_code == 200
            body = resp2.json()
            assert body["accepted"] is True
            assert body["attempt_id"] == "a1"
            assert body["already_admitted"] is True
            assert body["state"] == "running"
            assert len(started) == 1          # no second thread
            assert len(app.state.worker.snapshot()) == 1
            gate.set()
        deadline = time.time() + 5
        while time.time() < deadline and journal.get_attempt("a1") is not None \
                and journal.get_attempt("a1").kind != "completion":
            time.sleep(0.02)
        rec = journal.get_attempt("a1")
        assert rec is not None and rec.kind == "completion"
        assert len(journal.list_unacked_completions()) == 1

    def test_conflicting_identity_409(self, journal, auth_env):
        """Same attempt_id with a different identity → 409, no second thread."""
        from unittest.mock import patch

        gate = threading.Event()
        started = []

        def _blocking_batch(config, run_id=None, stats=None, config_sha256="", flush=False):
            started.append(run_id)
            gate.wait(timeout=30)
            return _mock_result(run_id=run_id or "r1")

        app, client = _make_client(journal=journal)
        token1 = _mint(app, attempt_id="a1", run_id="r1", generation=1)
        token2 = _mint(app, attempt_id="a1", run_id="r1", generation=2)
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=_blocking_batch,
        ):
            resp1 = _dispatch(client, run_id="r1", authorization=token1)
            assert resp1.status_code == 202
            resp2 = _dispatch(client, run_id="r1", authorization=token2)
            assert resp2.status_code == 409
            assert resp2.json()["detail"]["attempt_id"] == "a1"
            assert "different" in resp2.json()["detail"]["reason"]
            assert len(started) == 1
            assert len(app.state.worker.snapshot()) == 1
            gate.set()

    def test_revoked_410_with_tombstone_echo(self, journal, auth_env):
        """A revocation tombstone blocks admission with 410 + the echo."""
        from unittest.mock import patch

        app, client = _make_client(journal=journal)
        journal.record_tombstone("a1", "r1", "superseded")
        token = _mint(app, attempt_id="a1", run_id="r1")
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=AssertionError("thread must not start"),
        ):
            resp = _dispatch(client, run_id="r1", authorization=token)
        assert resp.status_code == 410
        detail = resp.json()["detail"]
        assert detail["attempt_id"] == "a1"
        assert detail["reason"] == "superseded"
        assert detail["revoked_at"]
        assert app.state.worker.snapshot() == []

    def test_invalid_token_401_with_reason(self, journal, auth_env):
        from unittest.mock import patch

        app, client = _make_client(journal=journal)
        token = _mint(app, attempt_id="a1", run_id="r1", secret="other-secret")
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=AssertionError("thread must not start"),
        ):
            resp = _dispatch(client, run_id="r1", authorization=token)
        assert resp.status_code == 401
        assert resp.json()["detail"]["reason"] == "bad signature"

    def test_old_session_epoch_403(self, journal, auth_env):
        """A token whose authorization row carries a retired session epoch is
        refused 403 (superseded session), never 401."""
        from unittest.mock import patch

        app, client = _make_client(journal=journal)
        token = _mint(app, attempt_id="a1", run_id="r1")
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=_mock_result,
        ):
            resp1 = _dispatch(client, run_id="r1", authorization=token)
            assert resp1.status_code == 202
        journal.advance_session_epoch()  # invalidates epoch-1 authorizations
        resp2 = _dispatch(client, run_id="r1", authorization=token)
        assert resp2.status_code == 403
        assert resp2.json()["detail"]["reason"] == "old session epoch"

    def test_quota_full_503_admission_closed(self, tmp_path, auth_env):
        """A journal at quota fails admission closed → 503, no thread."""
        from unittest.mock import patch

        j = WorkerJournal(
            tmp_path / "journal.db", quota_mb=0.001, headroom_mb=0, clock=FakeClock()
        )
        try:
            app, client = _make_client(journal=j)
            token = _mint(app, attempt_id="a1", run_id="r1")
            with patch(
                "tram.pipeline.executor.PipelineExecutor.batch_run",
                side_effect=AssertionError("thread must not start"),
            ):
                resp = _dispatch(client, run_id="r1", authorization=token)
            assert resp.status_code == 503
            assert "admission closed" in resp.json()["detail"]["reason"]
            assert app.state.worker.snapshot() == []
        finally:
            j.close()


# ── Legacy-admit rollback bridge ────────────────────────────────────────────


class TestLegacyAdmit:
    def test_legacy_dispatch_accepted_with_legacy_marker(self, journal, auth_env, caplog):
        """No authorization → accepted exactly as today, with the legacy
        marker in the response, the status payload, and the logs — and no
        journal recording."""
        from unittest.mock import patch

        gate = threading.Event()

        def _blocking_batch(config, run_id=None, stats=None, config_sha256="", flush=False):
            gate.wait(timeout=30)
            return _mock_result(run_id=run_id or "r1")

        app, client = _make_client(journal=journal)
        with caplog.at_level(logging.INFO, logger="tram.agent.server"), patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=_blocking_batch,
        ):
            resp = _dispatch(client, run_id="r1")
            assert resp.status_code == 202
            body = resp.json()
            assert body["accepted"] is True
            assert body["legacy"] is True
            assert "attempt_id" not in body
            # Status payload carries the legacy marker on the run item.
            status = client.get("/agent/status").json()
            items = [i for i in status["running"] if i["run_id"] == "r1"]
            assert len(items) == 1
            assert items[0]["legacy"] is True
            gate.set()

        # Log carries the explicit legacy marker.
        legacy_logs = [
            r for r in caplog.records if getattr(r, "legacy", False) is True
        ]
        assert len(legacy_logs) == 1
        assert "no fencing claimed" in legacy_logs[0].getMessage()
        # Legacy runs are never recorded in the journal.
        assert journal.list_unacked_completions() == []

    def test_legacy_admit_off_rejects_400(self, journal, monkeypatch):
        monkeypatch.setenv("TRAM_WORKER_LEGACY_ADMIT", "off")
        app, client = _make_client(journal=journal)
        resp = _dispatch(client, run_id="r1")
        assert resp.status_code == 400
        assert "TRAM_WORKER_LEGACY_ADMIT=off" in resp.json()["detail"]["reason"]
        assert app.state.worker.snapshot() == []

    def test_legacy_admit_off_still_allows_authorized(self, journal, auth_env, monkeypatch):
        """TRAM_WORKER_LEGACY_ADMIT=off rejects only un-authorized dispatches."""
        from unittest.mock import patch

        monkeypatch.setenv("TRAM_WORKER_LEGACY_ADMIT", "off")
        app, client = _make_client(journal=journal)
        token = _mint(app, attempt_id="a1", run_id="r1")
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=_mock_result,
        ):
            resp = _dispatch(client, run_id="r1", authorization=token)
        assert resp.status_code == 202
        assert resp.json()["attempt_id"] == "a1"

    def test_legacy_duplicate_run_id_409(self, journal, auth_env):
        """Legacy dispatches keep today's run_id-keyed 409 guard."""
        from tram.agent.server import ActiveRun

        app, client = _make_client(journal=journal)
        # Pre-seed an active run (same convention as test_agent_server.py) —
        # the async thread would otherwise finish before the repeat arrives.
        app.state.worker.add(ActiveRun(
            run_id="dup",
            pipeline_name="p",
            schedule_type="batch",
            started_at="2026-01-01T00:00:00+00:00",
            legacy=True,
        ))
        resp = _dispatch(client, run_id="dup")
        assert resp.status_code == 409
        assert "already active" in resp.json()["detail"]


# ── Config reader ───────────────────────────────────────────────────────────


class TestWorkerLegacyAdmitConfig:
    def test_default_auto(self, monkeypatch):
        monkeypatch.delenv("TRAM_WORKER_LEGACY_ADMIT", raising=False)
        assert cfg_mod.worker_legacy_admit() == "auto"

    def test_off(self, monkeypatch):
        monkeypatch.setenv("TRAM_WORKER_LEGACY_ADMIT", "off")
        assert cfg_mod.worker_legacy_admit() == "off"

    def test_invalid_value_fails_loud(self, monkeypatch):
        monkeypatch.setenv("TRAM_WORKER_LEGACY_ADMIT", "maybe")
        with pytest.raises(ValueError, match="TRAM_WORKER_LEGACY_ADMIT"):
            cfg_mod.worker_legacy_admit()


# ── Completion recording ────────────────────────────────────────────────────


class TestCompletionRecording:
    def test_completion_recorded_before_workerstate_removal(self, journal, auth_env):
        """Journal-first: the completions row commits before the run leaves
        WorkerState."""
        from unittest.mock import patch

        app, client = _make_client(journal=journal)
        token = _mint(app, attempt_id="a1", run_id="r1")
        state = app.state.worker
        real_remove = state.remove
        seen = {}

        def _remove(run_id):
            rec = journal.get_attempt("a1")
            seen["kind_at_removal"] = rec.kind if rec is not None else None
            real_remove(run_id)

        state.remove = _remove
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            return_value=_mock_result(run_id="r1", records_in=3, records_out=3),
        ):
            resp = _dispatch(client, run_id="r1", authorization=token)
            assert resp.status_code == 202
            deadline = time.time() + 5
            while time.time() < deadline and "kind_at_removal" not in seen:
                time.sleep(0.02)

        assert seen.get("kind_at_removal") == "completion"
        rec = journal.get_attempt("a1")
        assert rec is not None and rec.kind == "completion"
        payload = json.loads(rec.result_json)
        assert payload["run_id"] == "r1"
        assert payload["attempt_id"] == "a1"
        assert payload["status"] == "success"
        assert payload["records_in"] == 3
        assert payload["legacy"] is False
        assert state.get("r1") is None


# ── Boot / status surface ───────────────────────────────────────────────────


class TestBootAndStatus:
    def test_boot_marks_interrupted(self, journal, auth_env):
        """A reserved/running row from a previous app boot is marked
        interrupted by the next app's lifespan startup."""
        from unittest.mock import patch

        gate = threading.Event()

        def _blocking_batch(config, run_id=None, stats=None, config_sha256="", flush=False):
            gate.wait(timeout=30)
            return _mock_result(run_id=run_id or "r1")

        app1, client1 = _make_client(journal=journal)
        token = _mint(app1, attempt_id="a1", run_id="r1")
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=_blocking_batch,
        ):
            resp = client1.post(
                "/agent/run",
                json={
                    "pipeline_name": "test-pipe",
                    "yaml_text": _MINIMAL_YAML,
                    "run_id": "r1",
                    "authorization": token,
                },
            )
            assert resp.status_code == 202
            assert journal.get_attempt("a1").state == "running"

        # "Restart": a new app on the same journal enters lifespan → boot
        # recovery marks the in-flight reservation interrupted.
        app2 = create_worker_app(worker_id="w0", manager_url="", journal=journal)
        with TestClient(app2, raise_server_exceptions=True) as client2:
            rec = journal.get_attempt("a1")
            assert rec is not None and rec.kind == "interrupted"
            assert client2.get("/agent/status").status_code == 200
        gate.set()  # release the old thread (its completion lands afterwards)

    def test_status_carries_worker_session_journal_and_watermark(self, journal, auth_env):
        app, client = _make_client(journal=journal)
        status = client.get("/agent/status").json()
        assert status["worker_session"] == app.state.worker_session
        assert status["worker_session"].startswith("w0-")
        assert status["worker_session"].count("-") == 1
        assert status["journal"]["state"] == "ok"
        assert status["journal"]["size_bytes"] is not None
        wm = status["watermark"]
        assert wm["session_epoch"] == 1
        assert wm["admitting"] is True
        assert wm["behind_ms"] <= 0

    def test_status_survives_fatal_journal(self, tmp_path):
        """A corrupt/unavailable journal is reported distinctly on status —
        the endpoint never 500s and admission reports closed."""
        d = tmp_path / "subdir"
        d.mkdir()
        j = WorkerJournal(d)  # journal path is a directory → fatal unavailable
        try:
            app, client = _make_client(journal=j)
            resp = client.get("/agent/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["journal"]["state"] == "unavailable"
            assert data["watermark"]["admitting"] is False
            assert data["worker_session"] == app.state.worker_session
        finally:
            j.close()