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
# Stage 2  runtime     — final image (wheel + bundled MIB assets + UI assets)

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

# ── Stage 2: runtime image ────────────────────────────────────────────────────
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

# Copy bundled SNMP MIB assets from the repo
COPY files/mibs_compiled/ /mibs/
COPY files/mibs/ /mib-sources/

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
