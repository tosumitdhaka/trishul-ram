"""Shared process-lifetime ``httpx.Client`` pool for stateless HTTP sinks.

V18-08 (plan F): REST/VES sinks previously constructed a fresh
``httpx.Client`` per ``write()`` — one TCP+TLS handshake per record/chunk.
These sinks are stateless per request (synchronous request/response, the
response fully received before ``write()`` returns — that IS the delivery
barrier) and hold no run-scoped resources, so their connections can be
pooled process-wide without touching per-sink commit/close semantics.

Why this is safe for these sinks specifically (and NOT for e.g. ClickHouse,
Kafka, AMQP, SFTP):

- ``write()`` returns only after the response arrives, so the per-sink commit
  barrier (write-return = delivery) is unchanged.
- The sinks do not override ``close()`` — the executor's per-sink close call
  stays a no-op exactly as before, so close ordering is unchanged.
- ``httpx.Client`` is thread-safe for concurrent requests, so
  ``parallel_sinks`` fan-out over the shared client is safe.
- Connections (keep-alive) survive across runs in the same process, so a
  second run reuses the first run's pooled connections.

The pool is keyed by ``verify_ssl`` (the only client-level setting that
differs between sink configs); per-request settings (headers, auth, timeout)
are passed per request and override the client's base timeouts. The base
connect/read deadlines come from the frozen ``TRAM_RPC_CONNECT_TIMEOUT_S`` /
``TRAM_RPC_READ_TIMEOUT_S`` (V18-01 §9).
"""

from __future__ import annotations

import threading

import httpx

from tram.core.config import rpc_connect_timeout_s, rpc_read_timeout_s

_POOL: dict[bool, httpx.Client] = {}
_POOL_LOCK = threading.Lock()


def shared_http_client(verify_ssl: bool = True) -> httpx.Client:
    """Return the process-lifetime shared ``httpx.Client`` for *verify_ssl*.

    Lazily created on first use and kept for the process lifetime (never
    closed per run — there is no run-scoped resource). Thread-safe; the
    double-checked lock guarantees a single client per ``verify_ssl`` value.
    """
    client = _POOL.get(verify_ssl)
    if client is None:
        with _POOL_LOCK:
            client = _POOL.get(verify_ssl)
            if client is None:
                client = httpx.Client(
                    verify=verify_ssl,
                    timeout=httpx.Timeout(
                        connect=rpc_connect_timeout_s(),
                        read=rpc_read_timeout_s(),
                        write=rpc_read_timeout_s(),
                        pool=rpc_read_timeout_s(),
                    ),
                )
                _POOL[verify_ssl] = client
    return client


def reset_pool_for_tests() -> None:
    """Drop cached clients (tests only).

    Unit tests that patch the construction seam need a fresh pool between
    tests so a client built under one test's patch is never reused by a
    later test.
    """
    with _POOL_LOCK:
        _POOL.clear()