# Multi-stage Dockerfile for TRAM
#
# Stage 1  builder     — builds the Python wheel
# Stage 2  mib-builder — downloads + compiles standard SNMP MIBs
# Stage 3  runtime     — final image (wheel + compiled MIBs)
#
# NOTE: Stage 2 requires internet access during `docker build`.
#       If your build environment is air-gapped, place pre-compiled MIB .py
#       files in a local mibs/ directory and COPY them directly instead.

# ── Stage 1: build wheel ─────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY tram/ ./tram/

RUN pip install --no-cache-dir build && python -m build --wheel

# ── Stage 2: download + compile standard SNMP MIBs ───────────────────────────
FROM python:3.11-slim AS mib-builder

RUN pip install --no-cache-dir "pysmi-lextudio>=1.1"

# Download and compile the most commonly needed SNMP MIBs from mibs.pysnmp.com.
# Failures are non-fatal: the stage succeeds even if some MIBs can't be fetched
# (e.g. in air-gapped build environments).
RUN python - <<'EOF'
import os, sys
os.makedirs("/mibs", exist_ok=True)
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

    parser  = parserFactory()()
    codegen = PySnmpCodeGen()
    writer  = PyFileWriter("/mibs")

    compiler = MibCompiler(parser, codegen, writer)
    compiler.addSources(HttpReader("https://mibs.pysnmp.com/asn1/", ("",)))
    compiler.addSearchers(PyFileSearcher("/mibs"))
    compiler.addSearchers(StubSearcher(*PySnmpCodeGen.PYSNMP_STUBS))

    results = compiler.compile(*STANDARD_MIBS)
    for name, status in results.items():
        print(f"  {status}: {name}", flush=True)
    compiled = sum(1 for s in results.values() if s == "compiled")
    print(f"Compiled {compiled} MIB module(s) → /mibs", flush=True)
except Exception as exc:
    print(f"WARNING: MIB download failed ({exc}); continuing with empty /mibs", flush=True)
EOF

# ── Stage 3: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim

# Non-root user
RUN useradd -m -u 1000 -s /bin/bash tram

WORKDIR /app

# omniORBpy (CORBA) requires the omniORB shared runtime libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
        libomniorb4-2 libomnithread4 \
    && rm -rf /var/lib/apt/lists/*

# Install wheel with ALL connector/serializer/observability extras including corba.
COPY --from=builder /build/dist/*.whl .
RUN whl=$(ls *.whl) && \
    pip install --no-cache-dir \
        "${whl}[kafka,opensearch,s3,snmp,avro,protobuf_ser,parquet,msgpack_ser,mqtt,amqp,nats,gnmi,jmespath,sql,influxdb,redis,gcs,azure,websocket,elasticsearch,metrics,prometheus_rw,corba,mib,otel,watch,postgresql,mysql]" && \
    rm *.whl

# Copy compiled MIBs from mib-builder stage
COPY --from=mib-builder /mibs /mibs

# Create default pipeline, data, MIB, and schema directories; set ownership
RUN mkdir -p /pipelines /data /mibs /schemas && \
    mkdir -p /home/tram/.tram && \
    chown -R tram:tram /pipelines /data /mibs /schemas /home/tram /app

USER tram

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')" || exit 1

EXPOSE 8765

ENV TRAM_MIB_DIR=/mibs
ENV TRAM_SCHEMA_DIR=/schemas

ENTRYPOINT ["tram"]
CMD ["daemon"]
