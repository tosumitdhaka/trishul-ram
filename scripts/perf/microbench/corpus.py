"""Canonical telecom-CDR corpus + variants for the TRAM capacity-study microbench.

Shared study-wide record schema (Phase 1c): flat telecom CDR, ~22 fields,
~500 B as JSON. All generators are deterministic (seeded) so every phase of
the capacity study can regenerate identical data.

Variants (documented deviations, constructed only where a transform's input
shape demands it):

* ``make_nested``       — session_start/end, duration_s, bytes_up/down moved
                          under "session_info" (for json_flatten / jmespath /
                          unnest).
* ``make_cast``        — charge_amount / cell_id / roaming stringified, so the
                          cast transform has real str → int/float/bool work.
* ``make_hex``         — sgsn_addr / ggsn_addr as packed-IPv4 hex strings, so
                          hex_decode has real bytes.fromhex + ipaddress work.
* ``make_explode``     — adds charge_breakdown: list of 2 billing components.
* ``make_melt``        — adds counters: dict of 3 cumulative usage metrics.
* ``make_select``      — adds locations: list of serving/previous cell dicts.
* ``make_dedup``       — flat corpus with ~10% re-delivered duplicate records.
* ``make_counter``     — SNMP-style cumulative counter polls (cell_id series,
                          monotonically increasing octet counters, _polled_at
                          timestamps) for counter_delta.

Timing methodology (shared): time.perf_counter_ns, warmup on 1,000 records,
median of 5 timed reps, single process, one thread. Host is WSL2 — numbers
are comparative, not absolute.
"""

from __future__ import annotations

import ipaddress
import random
import time
import uuid
from datetime import UTC, datetime

# Deterministic base epoch for all generated timestamps: 2025-10-09T00:00:00Z.
BASE_EPOCH = 1_759_968_000

EVENT_TYPES = ["VOICE", "SMS", "DATA", "ROAMING", "EVENT"]
DIRECTIONS = ["MO", "MT", "FWD"]
RATS = ["2G", "3G", "4G", "5G", "NR_SA"]
APNS = ["internet", "mms.apn", "enterprise.vpn.apn", "iot.telemetry.apn", ""]
N_CELLS = 500


def _iso(epoch_s: int) -> str:
    return datetime.fromtimestamp(epoch_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_records(n: int, seed: int = 42) -> list[dict]:
    """Canonical flat CDR corpus: n records, timestamps 1 s apart."""
    rng = random.Random(seed)
    records = []
    for i in range(n):
        ts = BASE_EPOCH + i
        duration = rng.randrange(0, 3601)
        bytes_up = rng.randrange(0, 5_000_001)
        bytes_down = rng.randrange(0, 50_000_001)
        cell = rng.randrange(1, N_CELLS + 1)
        records.append({
            "record_id": str(uuid.UUID(int=rng.getrandbits(128))),
            "timestamp": _iso(ts),
            "msisdn": f"0{rng.randrange(0, 1_000_000_000):09d}",
            "imsi": f"44010{rng.randrange(0, 10**10):010d}",
            "imei": f"{rng.randrange(0, 10**15):015d}",
            "cell_id": cell,
            "event_type": rng.choice(EVENT_TYPES),
            "direction": rng.choice(DIRECTIONS),
            "duration_s": duration,
            "bytes_up": bytes_up,
            "bytes_down": bytes_down,
            "rat": rng.choice(RATS),
            "roaming": rng.random() < 0.2,
            "charge_amount": round(rng.uniform(0, 500), 2),
            "currency": "JPY",
            "session_start": _iso(ts),
            "session_end": _iso(ts + max(duration, 1)),
            "apn": rng.choice(APNS),
            "sgsn_addr": f"10.{rng.randrange(0, 256)}.{rng.randrange(0, 256)}.{rng.randrange(1, 255)}",
            "ggsn_addr": f"10.{rng.randrange(0, 256)}.{rng.randrange(0, 256)}.{rng.randrange(1, 255)}",
        })
    return records


def make_nested(n: int, seed: int = 42) -> list[dict]:
    """Nested variant: session fields under "session_info"."""
    out = []
    for rec in make_records(n, seed):
        session = {
            "session_start": rec.pop("session_start"),
            "session_end": rec.pop("session_end"),
            "duration_s": rec.pop("duration_s"),
            "bytes_up": rec.pop("bytes_up"),
            "bytes_down": rec.pop("bytes_down"),
        }
        rec["session_info"] = session
        out.append(rec)
    return out


def make_cast(n: int, seed: int = 42) -> list[dict]:
    """Cast variant: charge_amount / cell_id / roaming pre-stringified."""
    out = []
    for rec in make_records(n, seed):
        rec["charge_amount"] = f"{rec['charge_amount']:.2f}"
        rec["cell_id"] = str(rec["cell_id"])
        rec["roaming"] = "true" if rec["roaming"] else "false"
        out.append(rec)
    return out


def make_hex(n: int, seed: int = 42) -> list[dict]:
    """hex_decode variant: sgsn_addr / ggsn_addr as packed-IPv4 hex strings."""
    out = []
    for rec in make_records(n, seed):
        rec["sgsn_addr"] = ipaddress.IPv4Address(rec["sgsn_addr"]).packed.hex()
        rec["ggsn_addr"] = ipaddress.IPv4Address(rec["ggsn_addr"]).packed.hex()
        out.append(rec)
    return out


def make_explode(n: int, seed: int = 42) -> list[dict]:
    """Explode variant: adds charge_breakdown (list of 2 billing components)."""
    out = []
    for rec in make_records(n, seed):
        charge = rec["charge_amount"]
        rec["charge_breakdown"] = [
            {"component": "base", "amount": round(charge * 0.8, 2)},
            {"component": "tax", "amount": round(charge * 0.2, 2)},
        ]
        out.append(rec)
    return out


def make_melt(n: int, seed: int = 42) -> list[dict]:
    """Melt variant: adds a counters dict (SNMP-style wide metrics block)."""
    out = []
    for rec in make_records(n, seed):
        rec["counters"] = {
            "bytes_up": rec["bytes_up"],
            "bytes_down": rec["bytes_down"],
            "duration_s": rec["duration_s"],
        }
        out.append(rec)
    return out


def make_select(n: int, seed: int = 42) -> list[dict]:
    """select_from_list variant: adds a locations list (serving/previous cell)."""
    out = []
    for rec in make_records(n, seed):
        rec["locations"] = [
            {"loc_type": "serving", "cell_id": rec["cell_id"], "rat": rec["rat"]},
            {"loc_type": "previous", "cell_id": (rec["cell_id"] % N_CELLS) + 1,
             "rat": RATS[(RATS.index(rec["rat"]) + 1) % len(RATS)]},
        ]
        out.append(rec)
    return out


def make_dedup(n: int, seed: int = 42) -> list[dict]:
    """Deduplicate variant: every 10th record is a re-delivered duplicate."""
    import copy

    base = make_records(n, seed)
    out = []
    for i, rec in enumerate(base):
        if i > 0 and i % 10 == 0:
            out.append(copy.deepcopy(out[-1]))
        else:
            out.append(rec)
    return out


def make_counter(n: int, seed: int = 42, n_cells: int = N_CELLS, poll_interval_s: int = 300) -> list[dict]:
    """counter_delta variant: cumulative per-cell octet counters over polls.

    Shape required by counter_delta: key_fields (cell_id), a parseable
    timestamp (_polled_at), and monotonically increasing integer counters.
    Records are emitted in poll-time order (interleaved across cells), the
    way an SNMP poll source would deliver them.
    """
    rng = random.Random(seed)
    polls_per_cell = max(1, n // n_cells)
    samples = []
    for cell in range(n_cells):
        in_oct = rng.randrange(1_000_000_000, 2_000_000_000)
        out_oct = rng.randrange(500_000_000, 1_000_000_000)
        for p in range(polls_per_cell):
            t = BASE_EPOCH + p * poll_interval_s + cell
            in_oct += rng.randrange(0, 1_000_000)
            out_oct += rng.randrange(0, 500_000)
            samples.append({
                "_polled_at": _iso(t),
                "cell_id": f"cell-{cell:04d}",
                "if_in_octets": in_oct,
                "if_out_octets": out_oct,
            })
    samples.sort(key=lambda r: r["_polled_at"])
    return samples[:n]


# ── Shared benchmark plumbing ──────────────────────────────────────────────


def timed_reps(fn, reps: int = 5) -> list[int]:
    """Run fn reps times, return ALL raw ns timings (caller takes the median)."""
    timings = []
    for _ in range(reps):
        t0 = time.perf_counter_ns()
        fn()
        timings.append(time.perf_counter_ns() - t0)
    return timings


def median_ns(timings: list[int]) -> int:
    ordered = sorted(timings)
    return ordered[len(ordered) // 2]


def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


WARMUP_RECORDS = 1_000
N_RECORDS = 10_000
REPS = 5
