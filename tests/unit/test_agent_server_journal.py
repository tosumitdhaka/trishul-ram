"""V18-01 §4/§5 worker-journal admission wiring tests (tram/agent/server.py).

Covers the authorized /agent/run admission lane (validate_and_admit → 202,
idempotent repeat → 200, conflicting identity → 409, revoked → 410,
refused auth → 401/403, admission closed → 503), the legacy-admit rollback
bridge (auto marker / off → 400), journal-first completion recording, boot
interrupt marking, and the /agent/status surface (worker_session + journal
health + watermark). Uses real temp-file journals with a fake clock, following
the tests/unit/test_worker_journal.py conventions.

V18-05 part 2 (this lane): the /agent/handshake registration exchange
(secret mint + rotation, truthful capability set), the /agent/attempts/
replay endpoint (all four journal states + 404), the outbox drain (deliver +
ack, backoff, restart redelivery, duplicate no-op), and attempt-aware
WorkerState tracking (a superseded attempt can never clobber a newer one).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import tram.core.config as cfg_mod
from tram.agent.auth_tokens import mint_start_authorization
from tram.agent.journal import WorkerJournal
from tram.agent.server import (
    _WORKER_CAPABILITIES,
    TRAM_PROTOCOL_VERSION,
    ActiveRun,
    WorkerState,
    _completion_result_json,
    _drain_outbox_once,
    create_worker_app,
)

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

        def _remove(run_id, attempt_id=""):
            rec = journal.get_attempt("a1")
            seen["kind_at_removal"] = rec.kind if rec is not None else None
            real_remove(run_id, attempt_id=attempt_id)

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


# ── Shared helpers for the V18-05 part-2 lanes ──────────────────────────────


def _admit_direct(
    journal,
    attempt_id="a1",
    run_id="r1",
    generation=1,
    slot_id="s1",
    worker_session="w0-sess",
):
    """Admit one attempt straight through the journal (no HTTP), returning
    the minted token — the direct setup path for the attempts/outbox tests."""
    token = mint_start_authorization(
        attempt_id=attempt_id,
        run_id=run_id,
        generation=generation,
        slot_id=slot_id,
        worker_session=worker_session,
        ttl_s=300,
        secret=SECRET,
        issued_at_unix=T0,
    )
    result = journal.validate_and_admit(
        token,
        "test-pipe",
        worker_session=worker_session,
        current_secret=SECRET,
    )
    assert result.outcome == "admitted"
    return token


def _completion_payload(*, attempt_id="a1", run_id="r1", status="success"):
    return _completion_result_json(
        run_id=run_id,
        pipeline_name="test-pipe",
        worker_id="w0",
        attempt_id=attempt_id,
        generation=1,
        status=status,
        records_in=2,
        records_out=2,
        records_skipped=0,
        bytes_in=8,
        bytes_out=8,
        errors=[],
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        legacy=False,
    )


def _seed_completion_outbox(journal, attempt_id="a1", run_id="r1"):
    """Journal-first completion + outbox backup row, exactly as the run
    threads record them — then a crash before the direct post."""
    _admit_direct(journal, attempt_id=attempt_id, run_id=run_id)
    payload = _completion_payload(attempt_id=attempt_id, run_id=run_id)
    journal.record_completion(attempt_id, payload)
    journal.enqueue_outbox(attempt_id, "run-complete", payload)


class FakeManager:
    """Mock manager run-complete endpoint: identity-checked and idempotent.

    ``fail_first`` requests fail with 500 (transient manager outage); every
    later request returns 200 after a simulated ledger commit. A repeated
    ``attempt_id`` is a no-op that still 200s (frozen §5: duplicate delivery
    is safe — the worker's outbox acks on the 200 either way).
    """

    def __init__(self, fail_first: int = 0) -> None:
        self.requests: list[dict] = []
        self.responses: list[dict] = []
        self.commits: set[str] = set()
        self.fail_first = fail_first

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        if len(self.requests) <= self.fail_first:
            return httpx.Response(500, json={"error": "transient outage"})
        attempt_id = payload.get("attempt_id")
        if attempt_id in self.commits:
            body = {"ok": True, "ignored": "duplicate"}
        else:
            self.commits.add(attempt_id)
            body = {"ok": True}
        self.responses.append(body)
        return httpx.Response(200, json=body)


def _patch_manager_client(handler):
    """Patch tram.agent.server.httpx.Client with a fresh MockTransport client
    per call — the drain constructs one client per delivery attempt, exactly
    like the real HTTP path. The real class is captured BEFORE the patch (the
    patch rewrites the attribute on the shared httpx module)."""
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    return patch("tram.agent.server.httpx.Client", side_effect=_factory)


def _is_acked(journal, attempt_id="a1") -> bool:
    rec = journal.get_attempt(attempt_id)
    return rec is not None and rec.kind == "completion" and rec.acked is True


# ── /agent/handshake ────────────────────────────────────────────────────────


class TestHandshake:
    def test_round_trip_mints_and_rotates_secret(self, journal, auth_env):
        """A handshake mints a fresh secret (stored as current), retains the
        pre-rotation secret, and reports the truthful capability set."""
        app, client = _make_client(journal=journal)
        resp = client.post("/agent/handshake", json={})
        assert resp.status_code == 200
        body = resp.json()
        secret = body["session_secret"]
        assert secret and len(secret) >= 32
        assert body["protocol_version"] == TRAM_PROTOCOL_VERSION == "1.8"
        # Truthful capability declaration — exactly what this branch honors.
        assert body["capabilities"] == list(_WORKER_CAPABILITIES)
        assert body["capabilities"] == [
            "fencing",
            "commit_receipts",
            "durable_completion",
            "query_replay",
        ]
        # Not claimed (not implemented): admission_limits, drain, status_snapshot.
        for absent in ("admission_limits", "drain", "status_snapshot"):
            assert absent not in body["capabilities"]
        assert body["session_id"] == app.state.worker_session
        assert body["session_id"].startswith("w0-")
        assert body["worker_id"] == "w0"
        assert body["slot_capacity"] == {"batch": 2, "stream": 4}
        assert body["journal_health"]["state"] == "ok"
        # Secret state: env bootstrap became the retained previous.
        assert app.state.auth_secret == secret
        assert app.state.auth_secret_previous == SECRET

        # A second handshake rotates: the first minted secret is retained.
        resp2 = client.post("/agent/handshake", json={})
        secret2 = resp2.json()["session_secret"]
        assert secret2 != secret
        assert app.state.auth_secret == secret2
        assert app.state.auth_secret_previous == secret

    def test_handshake_secret_authorizes_dispatch_and_overlap_holds(
        self, journal, auth_env
    ):
        """Tokens minted with the handshake secret validate; tokens minted
        with the retained pre-rotation secret still validate during the
        rotation overlap (max TTL + skew)."""
        from unittest.mock import patch

        app, client = _make_client(journal=journal)
        handshake_secret = client.post("/agent/handshake", json={}).json()[
            "session_secret"
        ]
        # Pre-rotation token (env bootstrap secret) stays valid.
        old_token = _mint(app, attempt_id="a1", run_id="r1", secret=SECRET)
        # Post-rotation token (handshake secret).
        new_token = _mint(
            app, attempt_id="a2", run_id="r2", secret=handshake_secret
        )
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            return_value=_mock_result(run_id="r1"),
        ):
            resp_old = _dispatch(client, run_id="r1", authorization=old_token)
            assert resp_old.status_code == 202
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            return_value=_mock_result(run_id="r2"),
        ):
            resp_new = _dispatch(client, run_id="r2", authorization=new_token)
            assert resp_new.status_code == 202

    def test_handshake_without_env_bootstrap(self, journal):
        """No TRAM_AUTH_SESSION_SECRET: the first handshake retains nothing
        (no bootstrap) and the minted secret becomes current."""
        app, client = _make_client(journal=journal)
        assert app.state.auth_secret == ""
        assert app.state.auth_secret_previous is None
        secret = client.post("/agent/handshake", json={}).json()["session_secret"]
        assert app.state.auth_secret == secret
        assert app.state.auth_secret_previous is None

    def test_handshake_reports_env_slot_capacity(self, journal, monkeypatch):
        monkeypatch.setenv("TRAM_WORKER_BATCH_SLOTS", "5")
        monkeypatch.setenv("TRAM_WORKER_STREAM_SLOTS", "8")
        app, client = _make_client(journal=journal)
        body = client.post("/agent/handshake", json={}).json()
        assert body["slot_capacity"] == {"batch": 5, "stream": 8}

    def test_handshake_survives_fatal_journal(self, tmp_path):
        """A fatal journal is still reported in the handshake payload — the
        endpoint never 500s (the worker stays registerable)."""
        d = tmp_path / "subdir"
        d.mkdir()
        j = WorkerJournal(d)
        try:
            app, client = _make_client(journal=j)
            resp = client.post("/agent/handshake", json={})
            assert resp.status_code == 200
            body = resp.json()
            assert body["journal_health"]["state"] == "unavailable"
            assert body["session_secret"]
            assert app.state.auth_secret == body["session_secret"]
        finally:
            j.close()


# ── GET /agent/attempts/{attempt_id} ────────────────────────────────────────


class TestAttemptsEndpoint:
    def test_404_unknown_attempt(self, journal, auth_env):
        """404 means NO journal row — neither revocation nor quiescence."""
        app, client = _make_client(journal=journal)
        resp = client.get("/agent/attempts/no-such-attempt")
        assert resp.status_code == 404
        assert resp.json()["detail"]["reason"] == "unknown attempt"

    def test_tombstone_kind(self, journal, auth_env):
        app, client = _make_client(journal=journal)
        journal.record_tombstone("a1", "r1", "superseded")
        resp = client.get("/agent/attempts/a1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "tombstone"
        assert body["attempt_id"] == "a1"
        assert body["run_id"] == "r1"
        assert body["reason"] == "superseded"
        assert body["revoked_at"]
        assert body["session_epoch"] == 1

    def test_active_kind(self, journal, auth_env):
        app, client = _make_client(journal=journal)
        _admit_direct(journal, worker_session=app.state.worker_session)
        journal.mark_running("a1")
        resp = client.get("/agent/attempts/a1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "active"
        assert body["state"] == "running"
        assert body["run_id"] == "r1"
        assert body["pipeline_name"] == "test-pipe"
        assert body["generation"] == 1
        assert body["slot_id"] == "s1"
        assert body["thread_started"] is True

    def test_interrupted_kind(self, journal, auth_env):
        app, client = _make_client(journal=journal)
        _admit_direct(journal, worker_session=app.state.worker_session)
        journal.mark_running("a1")
        journal.mark_interrupted_on_boot()  # a previous process boot died
        resp = client.get("/agent/attempts/a1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "interrupted"
        assert body["state"] == "interrupted"
        assert body["attempt_id"] == "a1"

    def test_completion_kind_with_result(self, journal, auth_env):
        app, client = _make_client(journal=journal)
        _admit_direct(journal, worker_session=app.state.worker_session)
        journal.record_completion("a1", _completion_payload())
        resp = client.get("/agent/attempts/a1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "completion"
        assert body["run_id"] == "r1"
        assert body["completed_at"]
        assert body["acked"] is False
        assert body["acked_at"] is None
        assert body["result"]["attempt_id"] == "a1"
        assert body["result"]["run_id"] == "r1"
        assert body["result"]["status"] == "success"
        assert body["result"]["generation"] == 1


# ── Outbox drain ────────────────────────────────────────────────────────────


class TestOutboxDrain:
    def test_delivers_and_acks(self, journal, auth_env):
        """fetch_due_outbox → POST run-complete with the attempt-identity
        payload → mark_acked on manager 200."""
        _seed_completion_outbox(journal)
        manager = FakeManager()
        with _patch_manager_client(manager):
            n = _drain_outbox_once(journal, "http://manager", api_key="k")
        assert n == 1
        assert len(manager.requests) == 1
        payload = manager.requests[0]
        assert payload["attempt_id"] == "a1"
        assert payload["generation"] == 1
        assert payload["run_id"] == "r1"
        assert payload["status"] == "success"
        assert "a1" in manager.commits
        assert _is_acked(journal, "a1")

    def test_backs_off_on_failure(self, journal, auth_env):
        """A non-200 delivery records exponential backoff and stays due —
        the completion is never acked and never lost."""
        clock = journal._clock
        _seed_completion_outbox(journal)
        manager = FakeManager(fail_first=1)  # first delivery 500s
        with _patch_manager_client(manager):
            _drain_outbox_once(journal, "http://manager", api_key="k")
        assert not _is_acked(journal, "a1")
        rec = journal.get_attempt("a1")
        assert rec is not None and rec.acked is False
        # The row backed off: not due at T0, due after the 1 s backoff.
        assert journal.fetch_due_outbox() == []
        clock.advance(2)
        due = journal.fetch_due_outbox()
        assert len(due) == 1
        assert due[0].attempts == 1
        assert due[0].last_error
        # Second pass (manager healthy again) delivers and acks.
        manager2 = FakeManager()
        with _patch_manager_client(manager2):
            _drain_outbox_once(journal, "http://manager", api_key="k")
        assert _is_acked(journal, "a1")
        assert len(manager2.requests) == 1

    def test_duplicate_delivery_is_noop_manager_side(self, journal, auth_env):
        """Both the direct post and the outbox may deliver the same attempt
        — the identity-checked manager commits once and 200s the duplicate
        (and the worker acks on the 200 either way)."""
        _seed_completion_outbox(journal)
        manager = FakeManager()
        payload = json.loads(_completion_payload())
        with _patch_manager_client(manager):
            # The direct post arrives first (the retained fast path).
            with httpx.Client(transport=httpx.MockTransport(manager)) as direct:
                direct.post(
                    "http://manager/api/internal/run-complete", json=payload
                )
            # The outbox drain delivers the duplicate.
            _drain_outbox_once(journal, "http://manager", api_key="k")
        # Two deliveries, ONE manager-side commit; the duplicate was a no-op.
        assert len(manager.requests) == 2
        assert len(manager.commits) == 1
        assert "a1" in manager.commits
        assert manager.responses[1].get("ignored") == "duplicate"
        assert _is_acked(journal, "a1")
        assert journal.fetch_due_outbox() == []

    def test_redelivers_after_restart(self, tmp_path):
        """The outbox row is durable: a failed delivery survives a journal
        reopen, and the restarted worker's drain redelivers and acks."""
        clock = FakeClock()
        path = tmp_path / "journal.db"
        j1 = WorkerJournal(path, clock=clock)
        try:
            _seed_completion_outbox(j1)
            manager = FakeManager(fail_first=1)
            with _patch_manager_client(manager):
                _drain_outbox_once(j1, "http://manager", api_key="k")
            assert not _is_acked(j1, "a1")
        finally:
            j1.close()
        # "Restart": a new journal over the same file, clock past the backoff.
        clock.advance(10)
        j2 = WorkerJournal(path, clock=clock)
        try:
            manager2 = FakeManager()
            with _patch_manager_client(manager2):
                _drain_outbox_once(j2, "http://manager", api_key="k")
            assert _is_acked(j2, "a1")
            assert manager2.requests[0]["attempt_id"] == "a1"
        finally:
            j2.close()

    def test_drain_thread_delivers_under_lifespan(self, tmp_path, auth_env):
        """The background drain loop (started by the app lifespan) picks up
        a pre-existing unacked completion and acks it without any direct
        post — the crash-recovery path."""
        clock = FakeClock()
        j = WorkerJournal(tmp_path / "journal.db", clock=clock)
        try:
            _seed_completion_outbox(j)
            manager = FakeManager()
            app = create_worker_app(
                worker_id="w0", manager_url="http://manager", journal=j
            )
            with _patch_manager_client(manager), TestClient(
                app, raise_server_exceptions=True
            ):
                deadline = time.time() + 5
                while time.time() < deadline and not _is_acked(j, "a1"):
                    time.sleep(0.05)
            assert _is_acked(j, "a1")
            assert manager.requests[0]["attempt_id"] == "a1"
        finally:
            j.close()


# ── Attempt-aware WorkerState tracking ──────────────────────────────────────


class TestWorkerStateAttemptAware:
    def _run(self, *, run_id="r1", attempt_id="", legacy=False):
        return ActiveRun(
            run_id=run_id,
            pipeline_name="p",
            schedule_type="batch",
            started_at="2026-01-01T00:00:00+00:00",
            attempt_id=attempt_id,
            legacy=legacy,
        )

    def test_two_attempts_of_same_run_id_coexist(self):
        """A newer attempt's add must not clobber the older one."""
        s = WorkerState(worker_id="w0", manager_url="")
        older = self._run(attempt_id="a1")
        newer = self._run(attempt_id="b2")
        s.add(older)
        s.add(newer)
        assert len(s.snapshot()) == 2
        assert s.get("r1") is newer  # index points at the newest attempt

    def test_superseded_attempt_remove_does_not_evict_newer(self):
        """The superseded attempt finishing must not evict the newer one."""
        s = WorkerState(worker_id="w0", manager_url="")
        older = self._run(attempt_id="a1")
        newer = self._run(attempt_id="b2")
        s.add(older)
        s.add(newer)
        s.remove("r1", attempt_id="a1")  # older attempt completes last
        assert s.get("r1") is newer
        assert len(s.snapshot()) == 1

    def test_newer_remove_falls_back_to_older(self):
        s = WorkerState(worker_id="w0", manager_url="")
        older = self._run(attempt_id="a1")
        newer = self._run(attempt_id="b2")
        s.add(older)
        s.add(newer)
        s.remove("r1", attempt_id="b2")  # newer completes first
        assert s.get("r1") is older
        assert len(s.snapshot()) == 1

    def test_legacy_path_unchanged(self):
        """Legacy runs (no attempt_id) keep today's run_id-keyed behavior."""
        s = WorkerState(worker_id="w0", manager_url="")
        run = self._run(legacy=True)
        s.add(run)
        assert s.get("r1") is run
        s.remove("r1")
        assert s.get("r1") is None
        assert s.snapshot() == []

    def test_legacy_remove_does_not_evict_newer_attempt(self):
        s = WorkerState(worker_id="w0", manager_url="")
        legacy = self._run(legacy=True)
        newer = self._run(attempt_id="b2")
        s.add(legacy)
        s.add(newer)
        s.remove("r1")  # legacy run finishes by run_id
        assert s.get("r1") is newer
        assert len(s.snapshot()) == 1

    def test_superseded_attempt_http_surface(self, journal, auth_env):
        """End-to-end: two attempts of the same run_id run concurrently; the
        superseded attempt completing does not drop the newer one from
        /agent/status."""
        gate_a = threading.Event()
        gate_b = threading.Event()
        started: list[int] = []
        started_lock = threading.Lock()

        def _blocking_batch(config, run_id=None, stats=None, config_sha256="", flush=False):
            with started_lock:
                started.append(1)
                idx = len(started)
            if idx == 1:
                gate_a.wait(timeout=30)
            else:
                gate_b.wait(timeout=30)
            return _mock_result(run_id=run_id or "r1")

        app, client = _make_client(journal=journal)
        token_a = _mint(app, attempt_id="a1", run_id="r1", generation=1)
        token_b = _mint(app, attempt_id="b2", run_id="r1", generation=2)
        with patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            side_effect=_blocking_batch,
        ):
            resp_a = _dispatch(
                client, run_id="r1", authorization=token_a,
                attempt_id="a1", generation=1, slot_id="s1",
            )
            assert resp_a.status_code == 202
            deadline = time.time() + 5
            while time.time() < deadline and len(started) < 1:
                time.sleep(0.02)
            resp_b = _dispatch(
                client, run_id="r1", authorization=token_b,
                attempt_id="b2", generation=2, slot_id="s1",
            )
            assert resp_b.status_code == 202
            deadline = time.time() + 5
            while time.time() < deadline and len(started) < 2:
                time.sleep(0.02)
            # Both attempts of run r1 are active and visible.
            status = client.get("/agent/status").json()
            items = [i for i in status["running"] if i["run_id"] == "r1"]
            assert {i["attempt_id"] for i in items} == {"a1", "b2"}

            # The superseded attempt (a1) completes — its thread finally must
            # not evict b2.
            gate_a.set()
            deadline = time.time() + 5
            while time.time() < deadline and (
                journal.get_attempt("a1") is None
                or journal.get_attempt("a1").kind != "completion"
            ):
                time.sleep(0.02)
            deadline = time.time() + 5
            while time.time() < deadline and len(app.state.worker.snapshot()) != 1:
                time.sleep(0.02)
            status = client.get("/agent/status").json()
            items = [i for i in status["running"] if i["run_id"] == "r1"]
            assert len(items) == 1
            assert items[0]["attempt_id"] == "b2"
            gate_b.set()