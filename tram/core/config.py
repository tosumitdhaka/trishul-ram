"""AppConfig — all values from environment variables (12-factor)."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """Read an integer env var; raise ValueError with the variable name on bad input."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {name}={raw!r} is not a valid integer"
        ) from None


def _env_snmp_stack() -> str:
    """``TRAM_SNMP_STACK`` (v1.5.0, GH #72) — ``legacy`` (pysnmp) | ``trishul`` (tsmi/tsnmp).

    Default ``legacy``. Invalid values fail loud (a ``ValueError`` naming the
    variable) instead of silently picking a stack — the strictest pattern in
    this module (``_env_int``), since silently flipping a deployment's SNMP
    stack would be worse than a startup error. Manager and every worker must
    agree on the value; mismatch handling ships with the flag reader in
    v1.5.0 layer 3.
    """
    raw = os.environ.get("TRAM_SNMP_STACK", "legacy").lower()
    if raw not in ("legacy", "trishul"):
        raise ValueError(
            f"Environment variable TRAM_SNMP_STACK={raw!r} must be 'legacy' or 'trishul'"
        )
    return raw


def snmp_stack() -> str:
    """Active SNMP stack — ``legacy`` (pysnmp/pysmi) | ``trishul`` (tsmi/tsnmp).

    v1.5.0 layer 3 flag reader (GH #72). Reads ``TRAM_SNMP_STACK`` each call
    so a re-exec'd worker picks up the value the process was started with;
    invalid values fail loud via ``_env_snmp_stack``.
    """
    return _env_snmp_stack()


def stateful_transforms_enabled() -> bool:
    """``TRAM_STATEFUL_TRANSFORMS`` feature flag (F.1 §9) — default ON, fails open.

    "0" disables stateful transforms: pipelines using them fail validation
    with "stateful transforms disabled" and the internal transform-state
    endpoints return 404. Any unrecognized value is logged at WARNING and
    treated as enabled, so a typo'd value never silently flips a deployment's
    transform semantics (the D.2/E.2 flag convention).
    """
    raw = os.environ.get("TRAM_STATEFUL_TRANSFORMS", "1")
    if raw not in ("0", "1"):
        logger.warning(
            "Unrecognized TRAM_STATEFUL_TRANSFORMS value — treating as enabled (\"1\")",
            extra={"value": raw},
        )
    return raw != "0"


def ai_audit_enabled() -> bool:
    """``TRAM_AI_AUDIT`` feature flag (A10) — default ON, fails open.

    "0" stops the ``ai_usage`` append-only audit rows; the per-call
    "tram.ai" log line is always emitted regardless. Any unrecognized value
    is logged at WARNING and treated as enabled (the D.2/E.2 flag
    convention — a typo'd value never silently disables auditing).
    """
    raw = os.environ.get("TRAM_AI_AUDIT", "1")
    if raw not in ("0", "1"):
        logger.warning(
            "Unrecognized TRAM_AI_AUDIT value — treating as enabled (\"1\")",
            extra={"value": raw},
        )
    return raw != "0"


def http_accelerated() -> bool:
    """``TRAM_HTTP_ACCELERATED`` (v1.7.0 Pilot A) — default OFF, fails closed.

    "1" selects the accelerated uvicorn runtime (uvloop event loop +
    httptools HTTP parser) at every in-process HTTP server start site
    (daemon API, worker agent, webhook ingress). The flag only takes effect
    where both packages are importable — a missing package logs a WARNING
    and the server starts on the default asyncio/h11 runtime, never crashing.
    Any unrecognized value is logged at WARNING and treated as disabled, so
    a typo'd value never silently flips a deployment onto an untested
    runtime (the D.2/E.2 flag convention, inverted for a default-off flag).
    """
    raw = os.environ.get("TRAM_HTTP_ACCELERATED", "0")
    if raw not in ("0", "1"):
        logger.warning(
            'Unrecognized TRAM_HTTP_ACCELERATED value — treating as disabled ("0")',
            extra={"value": raw},
        )
    return raw == "1"


# TRAM_STATE_MAX_BYTES default: 20 MiB. Justification — the design F.1 §3.2a
# blob bound is ~2.5 MB at 50k counter keys; 20 MiB is ~8× that, so a
# ``window_aggregate`` group/window blob has room to grow between polls
# without letting a runaway blob inflate the ``transform_state`` DB row
# unbounded.
_STATE_MAX_BYTES_DEFAULT = 20 * 1024 * 1024


def state_max_bytes() -> int:
    """``TRAM_STATE_MAX_BYTES`` cap on the transform-state PUT body (F.1 §3.2b).

    Oversized blobs are rejected with 413. Invalid values are logged at
    WARNING and fall back to the default (the webhook body-cap convention).
    """
    raw = os.environ.get("TRAM_STATE_MAX_BYTES")
    if raw is None:
        return _STATE_MAX_BYTES_DEFAULT
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning(
            "Invalid TRAM_STATE_MAX_BYTES=%r — using default",
            raw,
        )
        return _STATE_MAX_BYTES_DEFAULT


# GH #78 defaults: the stream flush-record threshold mirrors kafka
# ``max_poll_records`` (500); the flush interval is the bounded end-to-end
# latency budget for buffered records (1s). The capacity study measured the
# per-message sink write as the dominant stream cost (~0.5-1 ms/record vs
# 5-12 µs/record on the batch path), so 500 records / 1s is the measured
# capacity window (kafka ~2x+, webhook ~1.2-1.5x) with a bounded latency trade.
_STREAM_FLUSH_RECORDS_DEFAULT = 500
_STREAM_FLUSH_INTERVAL_DEFAULT = 1.0


def stream_flush_records() -> int:
    """``TRAM_STREAM_FLUSH_RECORDS`` default record threshold for the stream
    micro-batch sink flush (GH #78).

    Stream pipelines buffer records and flush to sinks per batch instead of
    one serialized sink write per message; this is the record-count trigger
    (mirrors kafka ``max_poll_records``). ``1`` restores the pre-v1.6.0
    per-message flush. Per-pipeline ``stream_flush_records`` overrides it.
    Invalid values — non-integers and values < 1 (the model field is ``ge=1``,
    so ``0`` is invalid there too) — are logged at WARNING and fall back to
    the default (the webhook body-cap convention), consistent with the model
    rejecting ``0`` at validation.
    """
    raw = os.environ.get("TRAM_STREAM_FLUSH_RECORDS")
    if raw is None:
        return _STREAM_FLUSH_RECORDS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid TRAM_STREAM_FLUSH_RECORDS=%r — using default",
            raw,
        )
        return _STREAM_FLUSH_RECORDS_DEFAULT
    if value < 1:
        logger.warning(
            "Invalid TRAM_STREAM_FLUSH_RECORDS=%r (must be >= 1) — using default",
            raw,
        )
        return _STREAM_FLUSH_RECORDS_DEFAULT
    return value


def stream_flush_interval_seconds() -> float:
    """``TRAM_STREAM_FLUSH_INTERVAL_SECONDS`` flush interval for the stream
    micro-batch sink flush (GH #78).

    Bounded end-to-end latency budget: buffered records are flushed when the
    oldest record in the buffer has waited this long, even if the record
    threshold has not been reached. ``0`` disables the interval trigger
    (records then flush on the record threshold or the source batch end).
    Per-pipeline ``stream_flush_interval_s`` overrides it.
    """
    raw = os.environ.get("TRAM_STREAM_FLUSH_INTERVAL_SECONDS")
    if raw is None:
        return _STREAM_FLUSH_INTERVAL_DEFAULT
    try:
        return max(float(raw), 0.0)
    except ValueError:
        logger.warning(
            "Invalid TRAM_STREAM_FLUSH_INTERVAL_SECONDS=%r — using default",
            raw,
        )
        return _STREAM_FLUSH_INTERVAL_DEFAULT


# V18-01 §9 (frozen): bridge/buffer budgets for the internal source→executor
# queues. Defaults frozen at 16 MiB / 10000; overflow pauses intake (blocking
# put) or is rejected explicitly (webhook router 503) — never a silent drop
# and never an early acknowledgement (plan F).
_SOURCE_BRIDGE_MAX_BYTES_DEFAULT = 16 * 1024 * 1024
_SOURCE_BRIDGE_MAX_COUNT_DEFAULT = 10000


def source_bridge_max_bytes() -> int:
    """``TRAM_SOURCE_BRIDGE_MAX_BYTES`` (V18-01 §9) — byte budget of the
    internal source bridge queues (mqtt, websocket, nats, prometheus_rw,
    syslog TCP; AMQP is bounded by broker prefetch instead).

    ``0`` disables the byte bound. Invalid values are logged at WARNING and
    fall back to the default (the webhook body-cap convention).
    """
    raw = os.environ.get("TRAM_SOURCE_BRIDGE_MAX_BYTES")
    if raw is None:
        return _SOURCE_BRIDGE_MAX_BYTES_DEFAULT
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning(
            "Invalid TRAM_SOURCE_BRIDGE_MAX_BYTES=%r — using default",
            raw,
        )
        return _SOURCE_BRIDGE_MAX_BYTES_DEFAULT


def source_bridge_max_count() -> int:
    """``TRAM_SOURCE_BRIDGE_MAX_COUNT`` (V18-01 §9) — item-count budget of the
    internal source bridge queues (same connectors as ``source_bridge_max_bytes``).

    ``0`` disables the count bound. Invalid values are logged at WARNING and
    fall back to the default (the webhook body-cap convention).
    """
    raw = os.environ.get("TRAM_SOURCE_BRIDGE_MAX_COUNT")
    if raw is None:
        return _SOURCE_BRIDGE_MAX_COUNT_DEFAULT
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning(
            "Invalid TRAM_SOURCE_BRIDGE_MAX_COUNT=%r — using default",
            raw,
        )
        return _SOURCE_BRIDGE_MAX_COUNT_DEFAULT


# V18-01 §4 (frozen): worker journal — one stdlib-sqlite journal per worker
# at the frozen path, bounded by quota with admission headroom (admission
# fails closed once size >= quota - headroom so in-flight completions and
# outbox writes still commit).
_WORKER_JOURNAL_PATH_DEFAULT = "/var/lib/tram/worker/journal.db"


def worker_journal_path() -> str:
    """``TRAM_WORKER_JOURNAL_PATH`` (V18-01 §4) — frozen worker journal path.

    Dedicated PVC mount (``/var/lib/tram/worker``), separate from the
    ``/data`` asset ``emptyDir``. Read each call so a re-exec'd worker picks
    up the value the process was started with.
    """
    return os.environ.get("TRAM_WORKER_JOURNAL_PATH", _WORKER_JOURNAL_PATH_DEFAULT)


def worker_journal_quota_mb() -> int:
    """``TRAM_WORKER_JOURNAL_QUOTA_MB`` (V18-01 §4) — journal quota in MiB.

    ``0`` disables the quota bound (tests and dev). Invalid values fail loud
    via ``_env_int`` (the strictest pattern in this module).
    """
    return _env_int("TRAM_WORKER_JOURNAL_QUOTA_MB", 512)


def worker_journal_headroom_mb() -> int:
    """``TRAM_WORKER_JOURNAL_HEADROOM_MB`` (V18-01 §4) — admission headroom in MiB.

    Admission fails closed once the journal size reaches ``quota - headroom``;
    the headroom is the budget left for in-flight completions and outbox
    writes to commit before the hard quota.
    """
    return _env_int("TRAM_WORKER_JOURNAL_HEADROOM_MB", 64)


# V18-01 §4 (frozen): start-authorization token lifetimes for the
# manager→worker /agent/run channel. Defaults frozen at 300 / 600 / 5;
# the key-rotation overlap window is max TTL + skew (605 s at defaults).
_AUTH_TOKEN_TTL_S_DEFAULT = 300
_AUTH_MAX_TTL_S_DEFAULT = 600
_AUTH_CLOCK_SKEW_S_DEFAULT = 5


def auth_token_ttl_s() -> int:
    """``TRAM_AUTH_TOKEN_TTL_S`` (V18-01 §4) — minted start-authorization TTL.

    The manager stamps every start-authorization token with this lifetime.
    Invalid values fail loud via ``_env_int`` (the strictest pattern in this
    module).
    """
    return _env_int("TRAM_AUTH_TOKEN_TTL_S", _AUTH_TOKEN_TTL_S_DEFAULT)


def auth_max_ttl_s() -> int:
    """``TRAM_AUTH_MAX_TTL_S`` (V18-01 §4) — worker-side TTL acceptance cap.

    The worker rejects any token whose TTL exceeds this, and keeps the
    previous session secret valid for ``max_ttl + clock_skew`` after rotation.
    """
    return _env_int("TRAM_AUTH_MAX_TTL_S", _AUTH_MAX_TTL_S_DEFAULT)


def auth_clock_skew_s() -> int:
    """``TRAM_AUTH_CLOCK_SKEW_S`` (V18-01 §4) — manager/worker clock skew budget.

    ``issued_at`` up to this many seconds in the future is accepted; beyond it
    the token is rejected as future-issued.
    """
    return _env_int("TRAM_AUTH_CLOCK_SKEW_S", _AUTH_CLOCK_SKEW_S_DEFAULT)


# V18-01 §9 (frozen): worker journal retention.  Audit retention (7 d) bounds
# acked completion/outbox history; replay retention (1 d) bounds resolved
# revocation tombstones.  Authorization validity is max TTL + skew (~10 min),
# so ``gc_expired`` always outruns both — retention is the backstop sweep.
_WORKER_JOURNAL_AUDIT_RETENTION_S_DEFAULT = 604800
_WORKER_JOURNAL_REPLAY_RETENTION_S_DEFAULT = 86400


def worker_journal_audit_retention_s() -> int:
    """``TRAM_WORKER_JOURNAL_AUDIT_RETENTION_S`` (V18-01 §9) — audit retention
    for acked completion/outbox rows, in seconds.

    Invalid values fail loud via ``_env_int`` (the strictest pattern in this
    module).
    """
    return _env_int("TRAM_WORKER_JOURNAL_AUDIT_RETENTION_S", _WORKER_JOURNAL_AUDIT_RETENTION_S_DEFAULT)


def worker_journal_replay_retention_s() -> int:
    """``TRAM_WORKER_JOURNAL_REPLAY_RETENTION_S`` (V18-01 §9) — replay
    retention for resolved revocation tombstones, in seconds.

    Invalid values fail loud via ``_env_int`` (the strictest pattern in this
    module).
    """
    return _env_int("TRAM_WORKER_JOURNAL_REPLAY_RETENTION_S", _WORKER_JOURNAL_REPLAY_RETENTION_S_DEFAULT)


@dataclass(frozen=True)
class AppConfig:
    """Application-wide configuration loaded from environment variables."""

    host: str
    port: int
    pipeline_dir: str
    state_dir: str | None
    api_url: str
    log_level: str
    log_format: str
    workers: int
    reload_on_start: bool
    # v0.7.0 additions
    node_id: str
    db_url: str
    shutdown_timeout: int
    # v1.0.0 security additions
    api_key: str
    rate_limit: int
    rate_limit_window: int
    tls_certfile: str
    tls_keyfile: str
    # v1.0.0 observability additions
    otel_endpoint: str
    otel_service: str
    # v1.0.0 operations additions
    watch_pipelines: bool
    # v1.0.0 SNMP MIB directory
    mib_dir: str
    # v1.0.0 schema directory
    schema_dir: str
    # v1.0.4 schema registry (proxy + serializer fallback)
    schema_registry_url: str
    schema_registry_username: str
    schema_registry_password: str
    # v1.0.7 web UI static dir (empty = UI disabled)
    ui_dir: str
    # v1.0.8 browser user auth — comma-separated user:password pairs
    auth_users: str
    # v1.1.0 bundled pipeline templates directory
    templates_dir: str
    # v1.2.0 manager+worker mode
    tram_mode: str       # "standalone" | "manager" | "worker"
    manager_url: str     # worker → manager callback base URL (also used by manager itself)
    stats_interval: int  # worker stats reporting interval
    # Worker discovery (TRAM_MODE=manager)
    worker_urls: str     # explicit comma-separated worker agent URLs (TRAM_WORKER_URLS)
    worker_replicas: int # K8s headless DNS replica count (0 = disabled)
    worker_service: str  # K8s headless service name
    worker_namespace: str  # K8s namespace
    worker_port: int     # worker agent port
    worker_ingress_port: int  # worker public ingress port
    # v1.3.1 D.2 (GH #17): count=1 stream durable placement
    stream_single_placement: bool = True  # "1" (default) durable 1-slot placement / "0" legacy
    # v1.4.0 E.2 (GH #21): queued manual runs
    queue_manual_runs: bool = True    # "1" (default) queue manual runs on no-capacity / "0" fail-fast
    queue_ttl_seconds: int = 900      # how long a queued run waits for capacity before expiring
    # F.1 (GH #W-5.1): stateful transforms (counter_delta, later window_aggregate)
    stateful_transforms: bool = True  # "1" (default) enabled / "0" disabled (rollback)
    # F.1 (§3.2b): body-size cap for the internal transform-state PUT (20 MiB default)
    state_max_bytes: int = _STATE_MAX_BYTES_DEFAULT
    # GH #44: serve /docs, /redoc, /openapi.json (default on for dev; the
    # production recommendation is to disable via TRAM_DOCS_ENABLED=false)
    docs_enabled: bool = True
    # v1.5.0 (GH #72): SNMP library stack — "legacy" (pysnmp) | "trishul" (tsmi/tsnmp).
    # Definition only in v1.5.0 layer 2; the reader/consumer lands in layer 3.
    snmp_stack: str = "legacy"

    @classmethod
    def from_env(cls) -> AppConfig:
        node_id = os.environ.get("TRAM_NODE_ID", socket.gethostname())
        tram_mode = os.environ.get("TRAM_MODE", "standalone").lower()
        # v1.6.0 (GH #81): standalone mode defaults TRAM_MANAGER_URL to the
        # local daemon so the run-complete callback URL is never empty — an
        # empty URL silently drops every run-history row in single topology.
        # An explicitly-set value always wins (a hybrid standalone that reports
        # to a remote manager keeps working), and manager/worker modes are
        # deliberately NOT defaulted: their manager is remote, and a worker
        # defaulting to localhost would POST its own run-complete callbacks to
        # itself (tram/agent/server.py.create_worker_app reads the env raw).
        manager_url = os.environ.get("TRAM_MANAGER_URL", "")
        if tram_mode == "standalone" and not manager_url:
            manager_url = "http://localhost:8765"
        stream_single_placement_raw = os.environ.get("TRAM_STREAM_SINGLE_PLACEMENT", "1")
        if stream_single_placement_raw not in ("0", "1"):
            # Fail open: anything other than an explicit "0" enables the
            # durable-placement path. A typo'd value is loud here instead
            # of silently flipping a deployment's stream semantics.
            logger.warning(
                "Unrecognized TRAM_STREAM_SINGLE_PLACEMENT value — "
                'treating as enabled ("1")',
                extra={"value": stream_single_placement_raw},
            )
        queue_manual_runs_raw = os.environ.get("TRAM_QUEUE_MANUAL_RUNS", "1")
        if queue_manual_runs_raw not in ("0", "1"):
            # Fail open: anything other than an explicit "0" enables the
            # queue. A typo'd value is loud here instead of silently
            # flipping a deployment's manual-run semantics.
            logger.warning(
                "Unrecognized TRAM_QUEUE_MANUAL_RUNS value — "
                'treating as enabled ("1")',
                extra={"value": queue_manual_runs_raw},
            )
        return cls(
            host=os.environ.get("TRAM_HOST", "0.0.0.0"),
            port=_env_int("TRAM_PORT", 8765),
            pipeline_dir=os.environ.get("TRAM_PIPELINE_DIR", "./pipelines"),
            state_dir=os.environ.get("TRAM_STATE_DIR") or None,
            api_url=os.environ.get("TRAM_API_URL", "http://localhost:8765"),
            log_level=os.environ.get("TRAM_LOG_LEVEL", "INFO").upper(),
            log_format=os.environ.get("TRAM_LOG_FORMAT", "json"),
            workers=_env_int("TRAM_WORKERS", 1),
            reload_on_start=os.environ.get("TRAM_RELOAD_ON_START", "true").lower() == "true",
            node_id=node_id,
            db_url=os.environ.get("TRAM_DB_URL", ""),
            shutdown_timeout=_env_int("TRAM_SHUTDOWN_TIMEOUT_SECONDS", 30),
            api_key=os.environ.get("TRAM_API_KEY", ""),
            rate_limit=_env_int("TRAM_RATE_LIMIT", 50),
            rate_limit_window=_env_int("TRAM_RATE_LIMIT_WINDOW", 60),
            tls_certfile=os.environ.get("TRAM_TLS_CERTFILE", ""),
            tls_keyfile=os.environ.get("TRAM_TLS_KEYFILE", ""),
            otel_endpoint=os.environ.get("TRAM_OTEL_ENDPOINT", ""),
            otel_service=os.environ.get("TRAM_OTEL_SERVICE", "tram"),
            watch_pipelines=os.environ.get("TRAM_WATCH_PIPELINES", "false").lower() == "true",
            mib_dir=os.environ.get("TRAM_MIB_DIR", "/mibs"),
            schema_dir=os.environ.get("TRAM_SCHEMA_DIR", "/schemas"),
            schema_registry_url=os.environ.get("TRAM_SCHEMA_REGISTRY_URL", ""),
            schema_registry_username=os.environ.get("TRAM_SCHEMA_REGISTRY_USERNAME", ""),
            schema_registry_password=os.environ.get("TRAM_SCHEMA_REGISTRY_PASSWORD", ""),
            ui_dir=os.environ.get("TRAM_UI_DIR", "/ui"),
            auth_users=os.environ.get("TRAM_AUTH_USERS", ""),
            templates_dir=os.environ.get("TRAM_TEMPLATES_DIR", "/tram-templates"),
            tram_mode=tram_mode,
            manager_url=manager_url,
            stats_interval=_env_int("TRAM_STATS_INTERVAL", 30),
            stream_single_placement=stream_single_placement_raw != "0",
            queue_manual_runs=queue_manual_runs_raw != "0",
            queue_ttl_seconds=_env_int("TRAM_QUEUE_TTL_SECONDS", 900),
            stateful_transforms=stateful_transforms_enabled(),
            state_max_bytes=state_max_bytes(),
            docs_enabled=os.environ.get("TRAM_DOCS_ENABLED", "true").lower() == "true",
            snmp_stack=_env_snmp_stack(),
            worker_urls=os.environ.get("TRAM_WORKER_URLS", ""),
            worker_replicas=_env_int("TRAM_WORKER_REPLICAS", 0),
            worker_service=os.environ.get("TRAM_WORKER_SERVICE", "tram-worker"),
            worker_namespace=os.environ.get("TRAM_WORKER_NAMESPACE", "default"),
            worker_port=_env_int("TRAM_WORKER_PORT", 8766),
            worker_ingress_port=_env_int("TRAM_WORKER_INGRESS_PORT", 8767),
        )
