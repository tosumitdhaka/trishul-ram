# Dockerfile — TRAM standalone image
#
# Single-node deployment: combines manager and worker roles in one container.
# Use this for development, testing, or small single-node production deployments.
# For distributed deployments use Dockerfile.manager + Dockerfile.worker instead.
#
# Includes all tested extras from both manager and worker images plus AI features.
# Not yet tested (disabled; re-enable per image when validated):
#   mqtt, amqp, nats, gnmi, influxdb, redis, elasticsearch, opensearch
#
# Stage 0  ui-builder  — builds tram-ui static assets (Node 20)
# Stage 1  builder     — builds the Python wheel
# Stage 2  mib-builder — downloads + compiles standard SNMP MIBs + caches raw sources
# Stage 3  runtime     — final image (wheel + compiled MIBs + UI assets)
#
# NOTE: Stage 2 requires internet access during `docker build`.
#       If your build environment is air-gapped, place pre-compiled MIB .py
#       files in a local mibs/ directory and COPY them directly instead.

# ── Stage 0: build web UI ────────────────────────────────────────────────────
FROM node:20-alpine AS ui-builder

WORKDIR /ui-src
COPY tram/ui/package.json tram/ui/package-lock.json ./
RUN npm ci
COPY tram/ui/ ./
RUN npm run build

# ── Stage 1: build wheel ─────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY tram/ ./tram/

RUN pip install --no-cache-dir build && python -m build --wheel

# ── Stage 2: download + compile standard SNMP MIBs + cache raw sources ──────
FROM python:3.13-slim AS mib-builder

RUN pip install --no-cache-dir "pysmi-lextudio>=1.1"

# Download and compile the most commonly needed SNMP MIBs from mibs.pysnmp.com.
# Failures are non-fatal: the stage succeeds even if some MIBs can't be fetched
# (e.g. in air-gapped build environments).
RUN python - <<'EOF'
import os, sys
from pathlib import Path

os.makedirs("/mibs", exist_ok=True)
os.makedirs("/mib-sources", exist_ok=True)
try:
    from pysmi.reader import HttpReader
    from pysmi.searcher import PyFileSearcher, StubSearcher
    from pysmi.writer import PyFileWriter
    from pysmi.parser.smi import parserFactory
    from pysmi.codegen.pysnmp import PySnmpCodeGen
    from pysmi.compiler import MibCompiler

    STANDARD_MIBS = [
        "IANAifType-MIB",    # dependency of IF-MIB
        "IF-MIB",            # interface statistics (ifDescr, ifOperStatus, ...)
        "IP-MIB",            # IP routing / address table
        "TCP-MIB",           # TCP connection table
        "UDP-MIB",           # UDP listener table
        "ENTITY-MIB",        # physical / logical entity inventory
        "HOST-RESOURCES-MIB",# system resources (CPU, memory, storage, processes)
    ]

    class CachingReader:
        def __init__(self, reader, cache_dir):
            self._reader = reader
            self._cache_dir = Path(cache_dir)

        def __str__(self):
            return str(self._reader)

        def _fetch(self, mibname, **options):
            getter = getattr(self._reader, "getData", None) or getattr(self._reader, "get_data", None)
            if getter is None:
                raise AttributeError(f"{type(self._reader).__name__} reader has no getData/get_data method")
            mib_info, mib_text = getter(mibname, **options)
            target = self._cache_dir / Path(getattr(mib_info, "file", mibname)).name
            target.write_text(mib_text, encoding="utf-8")
            return mib_info, mib_text

        def getData(self, mibname, **options):
            return self._fetch(mibname, **options)

        def get_data(self, mibname, **options):
            return self._fetch(mibname, **options)

    parser  = parserFactory()()
    codegen = PySnmpCodeGen()
    writer  = PyFileWriter("/mibs")

    compiler = MibCompiler(parser, codegen, writer)
    compiler.addSources(CachingReader(HttpReader("https://mibs.pysnmp.com/asn1/@mib@"), "/mib-sources"))
    compiler.addSearchers(PyFileSearcher("/mibs"))
    compiler.addSearchers(StubSearcher(*(PySnmpCodeGen.baseMibs + PySnmpCodeGen.fakeMibs)))

    results = compiler.compile(*STANDARD_MIBS)
    for name, status in results.items():
        print(f"  {status}: {name}", flush=True)
    compiled = sum(1 for s in results.values() if s == "compiled")
    print(f"Compiled {compiled} MIB module(s) → /mibs; cached raw sources → /mib-sources", flush=True)
except Exception as exc:
    print(f"WARNING: MIB download failed ({exc}); continuing with empty /mibs and /mib-sources", flush=True)
EOF

# ── Stage 3: runtime image ────────────────────────────────────────────────────
FROM python:3.13-slim

# Non-root user
RUN useradd -m -u 1000 -s /bin/bash tram

WORKDIR /app

# omniORBpy (CORBA) requires the omniORB shared runtime libraries
# Package was renamed libomniorb4-2 → libomniorb4-3t64 in newer Debian
RUN apt-get update && \
    ( apt-get install -y --no-install-recommends libomniorb4-3t64 libomnithread4 || \
      apt-get install -y --no-install-recommends libomniorb4-2 libomnithread4 ) && \
    rm -rf /var/lib/apt/lists/*

# Standalone extras: union of manager + worker images plus AI features.
# Excluded to keep the image lean (install individually as needed):
#   parquet      — pyarrow (~150 MB):  pip install tram[parquet]
#   s3           — boto3/botocore (~60 MB): pip install tram[s3]
#   gcs          — google-cloud-storage (~50 MB): pip install tram[gcs]
#   azure        — azure-storage-blob (~30 MB): pip install tram[azure]
#   otel         — opentelemetry-sdk + OTLP exporter (~15 MB): pip install tram[otel]
# Not yet tested (re-enable when validated):
#   mqtt, amqp, nats, gnmi, influxdb, redis, elasticsearch, opensearch
# corba (omniORBpy) excluded: not on PyPI; install python3-omniorb via apt in a custom layer.
COPY --from=builder /build/dist/*.whl .
RUN whl=$(ls *.whl) && \
    pip install --no-cache-dir \
        "${whl}[manager,worker,k8s,metrics,watch,mib,protobuf_ser,protobuf,asn1,kafka,snmp,avro,jmespath,sql,websocket,prometheus_rw,ai-anthropic,ai-openai]" && \
    rm *.whl

# Copy compiled MIBs from mib-builder stage
COPY --from=mib-builder /mibs /mibs
COPY --from=mib-builder /mib-sources /mib-sources

# Copy web UI static assets from ui-builder stage
COPY --from=ui-builder /ui-src/dist /ui

# Bundle pipeline templates (read-only reference examples, always available)
COPY pipelines/ /tram-templates/

# Create default pipeline, data, MIB, and schema directories; set ownership
RUN mkdir -p /pipelines /data /data/mibs /data/mib-sources /mibs /mib-sources /schemas && \
    mkdir -p /home/tram/.tram && \
    chown -R tram:tram /pipelines /data /mibs /mib-sources /schemas /home/tram /app /ui

USER tram

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')" || exit 1

EXPOSE 8765

ENV TRAM_MIB_DIR=/mibs
ENV TRAM_MIB_BUNDLED_SOURCE_DIR=/mib-sources
ENV TRAM_SCHEMA_DIR=/schemas
ENV TRAM_UI_DIR=/ui
ENV TRAM_TEMPLATES_DIR=/tram-templates

ENTRYPOINT ["tram"]
CMD ["daemon"]
