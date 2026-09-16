"""gNMI source connector — subscribes to gNMI telemetry stream."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

@register_source("gnmi")
class GnmiSource(BaseSource):
    """Subscribe to a gNMI telemetry stream.

    Config keys:
        host            (str, required)
        port            (int, default 57400)
        username        (str, default "")
        password        (str, default "")
        tls             (bool, default True)
        tls_ca          (str, optional)  Path to CA certificate file
        subscription_mode (str, default "stream")  "once" | "poll" | "stream"
        poll_interval_seconds (int, default 60)  seconds between re-gets in poll mode
        reconnect_delay_seconds (float, default 5.0)  backoff between reconnect attempts
        max_reconnect_attempts (int, default 0)  0 = infinite
        subscriptions   (list[dict])     Each: {path, mode, sample_interval}
            path            (str, required)   XPath e.g. "/interfaces/interface[name=*]/state"
            mode            (str, default "SAMPLE")  SAMPLE|ON_CHANGE|TARGET_DEFINED
            sample_interval (int, default 10000000000)  nanoseconds

    ``subscription_mode`` mirrors the gNMI spec's top-level subscription modes:

    * ``stream`` (default) — continuous telemetry stream.  A lost session is
      re-established with backoff instead of silently ending the pipeline
      (previously a router reload or transient TCP break killed the stream).
    * ``once`` — one snapshot subscription that ends after the initial data is
      received (gNMI end-of-stream semantics).  Pair with an interval schedule
      for repeated snapshots.
    * ``poll`` — gNMI POLL mapped to a periodic re-get: TRAM issues a fresh
      ONCE subscription every ``poll_interval_seconds``.  The gNMI SUBSCRIBE
      poll channel requires holding a live gRPC session open and issuing
      client-initiated poll calls, which does not fit TRAM's pull-based
      ``read()`` plus the reconnect loop; a periodic ONCE re-get delivers the
      same data with the same session lifecycle as ``stream`` mode.
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 57400))
        self.username: str = config.get("username", "")
        self.password: str = config.get("password", "")
        self.tls: bool = bool(config.get("tls", True))
        self.tls_ca: str | None = config.get("tls_ca")
        self.subscriptions: list[dict] = config.get("subscriptions", [])
        self.subscription_mode: str = str(config.get("subscription_mode", "stream")).lower()
        if self.subscription_mode not in ("once", "poll", "stream"):
            raise SourceError(
                f"gNMI: invalid subscription_mode '{self.subscription_mode}' "
                "(use once, poll, or stream)"
            )
        self.poll_interval_seconds: int = int(config.get("poll_interval_seconds", 60))
        self.reconnect_delay_seconds: float = float(config.get("reconnect_delay_seconds", 5.0))
        self.max_reconnect_attempts: int = int(config.get("max_reconnect_attempts", 0))
        self._stop_event: threading.Event = threading.Event()
        self._client = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Interrupt read(): unblock a blocked subscription and skip backoff.

        Called by the executor's stop-watcher thread when the pipeline stop
        event fires.  Sets the stop flag and closes the active gNMI session so
        a blocked ``subscribe_stream`` unblocks immediately.
        """
        self._stop_event.set()
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Sleep in slices so stop() interrupts the delay. Returns True if stopped."""
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._stop_event.wait(timeout=min(0.25, remaining))
        return True

    # ── Decoding ───────────────────────────────────────────────────────────

    @staticmethod
    def _decode_updates(response: dict) -> list[dict]:
        updates = []
        for update in response.get("update", {}).get("update", []):
            updates.append({
                "path": update.get("path", ""),
                "val": update.get("val", {}),
                "timestamp": response.get("update", {}).get("timestamp", 0),
            })
        return updates

    def _iter_updates(self, client, subscribe_request) -> Iterator[tuple[bytes, dict]]:
        """Decode each subscribe_stream response into (payload, meta) tuples."""
        for response in client.subscribe_stream(subscribe=subscribe_request):
            if self._stop_event.is_set():
                return
            try:
                updates = self._decode_updates(response)
            except Exception as exc:
                logger.warning("gNMI update decode error: %s", exc)
                continue
            if updates:
                yield json.dumps(updates).encode(), {
                    "gnmi_host": self.host,
                    "gnmi_port": self.port,
                }

    # ── Read ───────────────────────────────────────────────────────────────

    def read(self) -> Iterator[tuple[bytes, dict]]:
        try:
            from pygnmi.client import gNMIclient
        except ImportError as exc:
            raise SourceError(
                "gNMI source requires pygnmi — install with: pip install tram[gnmi]"
            ) from exc

        gnmi_kwargs = {
            "target": (self.host, self.port),
            "username": self.username,
            "password": self.password,
            "insecure": not self.tls,
        }
        if self.tls_ca:
            gnmi_kwargs["path_cert"] = self.tls_ca

        mode = self.subscription_mode
        subscribe_request = {
            "subscription": [
                {
                    "path": sub["path"],
                    "mode": sub.get("mode", "SAMPLE").upper(),
                    "sampleInterval": sub.get("sample_interval", 10_000_000_000),
                }
                for sub in self.subscriptions
            ],
            # gNMI spec top-level mode: STREAM for the continuous subscription;
            # ONCE for both once and poll (poll re-gets via fresh ONCE snapshots).
            "mode": "STREAM" if mode == "stream" else "ONCE",
            "encoding": "JSON_IETF",
        }

        if mode == "once":
            yield from self._run_snapshot(gNMIclient, gnmi_kwargs, subscribe_request)
            return
        if mode == "poll":
            yield from self._run_poll(gNMIclient, gnmi_kwargs, subscribe_request)
            return
        yield from self._run_stream(gNMIclient, gnmi_kwargs, subscribe_request)

    def _run_snapshot(self, client_cls, gnmi_kwargs, subscribe_request) -> Iterator[tuple[bytes, dict]]:
        """One gNMI ONCE snapshot, then end-of-stream."""
        try:
            with client_cls(**gnmi_kwargs) as client:
                self._client = client
                logger.info(
                    "gNMI source taking snapshot",
                    extra={"host": self.host, "port": self.port},
                )
                yield from self._iter_updates(client, subscribe_request)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"gNMI subscription failed: {exc}") from exc
        finally:
            self._client = None

    def _run_poll(self, client_cls, gnmi_kwargs, subscribe_request) -> Iterator[tuple[bytes, dict]]:
        """Periodic re-get: a fresh ONCE snapshot every poll_interval_seconds.

        ``max_reconnect_attempts`` bounds *consecutive failed polls*: a snapshot
        that completes without an exception (even with zero updates — the NE
        simply had no data) proves the connection is healthy and resets the
        counter, mirroring the stream mode's healthy-session reset. Exhaustion
        raises SourceError; ``0`` = retry forever.
        """
        attempt = 0
        max_attempts = self.max_reconnect_attempts  # 0 = infinite
        while not self._stop_event.is_set():
            try:
                with client_cls(**gnmi_kwargs) as client:
                    self._client = client
                    logger.info(
                        "gNMI source polling snapshot",
                        extra={"host": self.host, "port": self.port},
                    )
                    yield from self._iter_updates(client, subscribe_request)
            except SourceError:
                raise
            except Exception as exc:
                attempt += 1
                if max_attempts > 0 and attempt >= max_attempts:
                    raise SourceError(
                        f"gNMI poll failed after {attempt} failed poll(s): {exc}"
                    ) from exc
                logger.warning(
                    "gNMI poll failed — retrying next poll",
                    extra={
                        "host": self.host,
                        "port": self.port,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
            else:
                # The snapshot completed without an exception — healthy poll.
                attempt = 0
            finally:
                self._client = None
            if self._sleep_interruptible(self.poll_interval_seconds):
                return

    def _run_stream(self, client_cls, gnmi_kwargs, subscribe_request) -> Iterator[tuple[bytes, dict]]:
        """Continuous subscription with reconnect-on-loss (backoff, stop-aware)."""
        attempt = 0
        max_attempts = self.max_reconnect_attempts  # 0 = infinite
        while not self._stop_event.is_set():
            session_error: Exception | None = None
            produced = False
            try:
                with client_cls(**gnmi_kwargs) as client:
                    self._client = client
                    logger.info(
                        "gNMI source streaming",
                        extra={"host": self.host, "port": self.port},
                    )
                    for payload, meta in self._iter_updates(client, subscribe_request):
                        produced = True
                        yield payload, meta
                # The stream ended without an exception: the peer closed the
                # session (e.g. a target reload) — reconnect.
                if self._stop_event.is_set():
                    return
                session_error = ConnectionError("gNMI stream ended")
            except SourceError:
                raise
            except Exception as exc:
                session_error = exc
            finally:
                self._client = None
            if self._stop_event.is_set():
                return
            if produced:
                # A healthy session ran and delivered data — the target is up,
                # so the reconnect counter restarts (matches the Kafka source's
                # reset-on-connect semantics).
                attempt = 0
            else:
                attempt += 1
                if max_attempts > 0 and attempt >= max_attempts:
                    raise SourceError(
                        f"gNMI subscription failed after {attempt} failed session(s): {session_error}"
                    ) from session_error
            logger.warning(
                "gNMI session lost — reconnecting",
                extra={
                    "host": self.host,
                    "port": self.port,
                    "attempt": attempt,
                    "delay": self.reconnect_delay_seconds,
                    "error": str(session_error),
                },
            )
            if self._sleep_interruptible(self.reconnect_delay_seconds):
                return
