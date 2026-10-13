"""Isolated ten-IP SNMP pilot with real SFTP JSONL readback.

Run with --sftp-url pointing to a disposable fixture, never a deployed service.
"""

import argparse
import asyncio
import json
import os
import platform
import resource
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from importlib.metadata import version
from pathlib import Path
from urllib.parse import unquote, urlsplit

import paramiko
from trishul_snmp import OctetStringValue, TimeTicksValue, V2cResponder

import tram.serializers  # noqa: F401
import tram.transforms  # noqa: F401
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--sftp-url", default=os.environ.get("TRAM_TEST_SFTP_URL"))
parser.add_argument("--evidence", required=True)
args = parser.parse_args()
if not args.sftp_url:
    parser.error("--sftp-url or TRAM_TEST_SFTP_URL required")
parts = urlsplit(args.sftp_url)
address = (parts.hostname, parts.port or 22)
username = unquote(parts.username or "")
password = unquote(parts.password or "")
hosts = [f"127.0.0.{i}" for i in range(11, 21)]
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]


def cycle(kind: str, iteration: int) -> dict:
    stop = threading.Event()
    ready = threading.Event()

    async def serve() -> None:
        async with AsyncExitStack() as stack:
            responders = []
            for host in hosts if kind == "healthy" else hosts[:-1]:
                responder = await stack.enter_async_context(
                    V2cResponder(
                        host=host,
                        port=port,
                        communities=["public"],
                        objects=[
                            ((1, 3, 6, 1, 2, 1, 1, 1, 0), OctetStringValue(host.encode())),
                            ((1, 3, 6, 1, 2, 1, 1, 3, 0), TimeTicksValue(12345)),
                        ],
                    )
                )
                responders.append(asyncio.create_task(responder.serve_forever()))
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.02)
            for task in responders:
                task.cancel()
            await asyncio.gather(*responders, return_exceptions=True)

    thread = threading.Thread(target=lambda: asyncio.run(serve()))
    remote = f"{parts.path.rstrip(chr(47))}/v183-snmp-pilot/{int(time.time())}-{kind}-{iteration}"
    transport = paramiko.Transport(address)
    try:
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        for path in [f"{parts.path.rstrip(chr(47))}/v183-snmp-pilot", remote]:
            try:
                sftp.mkdir(path)
            except OSError:
                pass
    finally:
        transport.close()
    config = load_pipeline_from_yaml(
        json.dumps(
            {
                "pipeline": {
                    "name": "ten-target-pilot",
                    "source": {
                        "type": "snmp_poll",
                        "targets": hosts,
                        "port": port,
                        "max_concurrency": 4,
                        "timeout": 0.15,
                        "retries": 0,
                        "target_timeout": 1,
                        "oids": ["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.3.0"],
                        "resolve_oids": False,
                        "classify": True,
                        "index_depth": 1,
                    },
                    "serializer_in": {"type": "json"},
                    "transforms": [
                        {
                            "type": "add_field",
                            "fields": {
                                "category": "'even' if _source_host in "
                                "['127.0.0.12','127.0.0.14','127.0.0.16','127.0.0.18','127.0.0.20'] "
                                "else 'odd'",
                            },
                        }
                    ],
                    "serializer_out": {"type": "ndjson"},
                    "sinks": [
                        {
                            "type": "sftp",
                            "host": parts.hostname,
                            "port": parts.port or 22,
                            "username": username,
                            "password": password,
                            "remote_path": remote,
                            "filename_template": "{field.category}_{run_id}.jsonl",
                            "file_mode": "append",
                        }
                    ],
                    "thread_workers": 1,
                    "on_error": "abort",
                }
            }
        )
    )
    started = time.monotonic()
    executor = PipelineExecutor()
    source = executor._build_source(config)
    executor._build_source = lambda config: source
    thread.start()
    try:
        assert ready.wait(3), "SNMP fixtures failed to start"
        result = executor.batch_run(config)
    finally:
        stop.set()
        thread.join(3)
        assert not thread.is_alive()
    elapsed = time.monotonic() - started
    transport = paramiko.Transport(address)
    transport.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    rows = []
    files = sftp.listdir(remote)
    for name in files:
        with sftp.open(remote + "/" + name) as f:
            for line in f:
                rows.append(json.loads(line))
    sftp.close()
    transport.close()
    expected = set(hosts if kind == "healthy" else hosts[:-1])
    assert {r["_source_host"] for r in rows} == expected, (kind, result, rows)
    assert len(rows) == len(expected)
    for row in rows:
        assert row["_labels"]["1.3.6.1.2.1.1.1"] == row["_source_host"]
        assert row["_metrics"]["1.3.6.1.2.1.1.3"] == 12345
    assert elapsed < 300
    assert str(result.status) == ("success" if kind == "healthy" else "failed"), result
    return dict(
        kind=kind,
        iteration=iteration,
        elapsed_seconds=round(elapsed, 3),
        status=str(result.status),
        error=result.error,
        records=len(rows),
        files=files,
        remote_path=remote,
        poll_results=source.poll_results,
    )


def stop_cycle(iteration: int) -> dict:
    """Hold real UDP requests unanswered, then cancel the owning batch run."""
    sockets = []
    with tempfile.TemporaryDirectory() as output:
        try:
            for host in hosts:
                blackhole = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                blackhole.bind((host, port))
                sockets.append(blackhole)
            config = load_pipeline_from_yaml(
                json.dumps(
                    {
                        "pipeline": {
                            "name": "pilot-stop",
                            "source": {
                                "type": "snmp_poll",
                                "targets": hosts,
                                "port": port,
                                "oids": ["1.3.6.1.2.1.1.3.0"],
                                "timeout": 10,
                                "target_timeout": 30,
                                "resolve_oids": False,
                            },
                            "serializer_in": {"type": "json"},
                            "serializer_out": {"type": "ndjson"},
                            "sink": {"type": "local", "path": output},
                        }
                    }
                )
            )
            stop = threading.Event()
            executor = PipelineExecutor()
            source = executor._build_source(config)
            executor._build_source = lambda config: source
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(executor.batch_run, config, stop_event=stop)
                sockets[0].settimeout(2)
                sockets[0].recvfrom(65535)
                stop.set()
                result = future.result(timeout=3)
            assert str(result.status) == "aborted", result
            assert len(source.poll_results) == 10
            assert all(r["status"] == "cancelled" for r in source.poll_results)
            assert list(Path(output).iterdir()) == []
            return {
                "kind": "stop",
                "iteration": iteration,
                "status": str(result.status),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "poll_results": source.poll_results,
            }
        finally:
            for blackhole in sockets:
                blackhole.close()


results = []
for i in range(1, 4):
    results.append(stop_cycle(i))
    results.extend(cycle(kind, i) for kind in ["healthy", "mixed"])
Path(args.evidence).write_text(
    json.dumps(
        dict(
            python=__import__("sys").version,
            network="isolated local loopback 127.0.0.11..20",
            port=port,
            oids=["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.3.0"],
            max_concurrency=4,
            request_timeout=0.15,
            retries=0,
            target_timeout=1,
            snmp_library=version("trishul-snmp"),
            platform=platform.platform(),
            visible_cpus=os.cpu_count(),
            max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            results=results,
        ),
        indent=2,
    )
)
print(
    json.dumps(
        [{k: r[k] for k in ("kind", "status", "elapsed_seconds")} for r in results], indent=2
    )
)
