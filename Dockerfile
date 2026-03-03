# Multi-stage Dockerfile for TRAM
# Stage 1: Build wheel
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY tram/ ./tram/

RUN pip install --no-cache-dir build && python -m build --wheel

# Stage 2: Runtime image
FROM python:3.11-slim

# Non-root user
RUN useradd -m -u 1000 -s /bin/bash tram

WORKDIR /app

# Install wheel
COPY --from=builder /build/dist/*.whl .
RUN pip install --no-cache-dir *.whl && rm *.whl

# Create default pipeline and state directories
RUN mkdir -p /pipelines /data/state && chown -R tram:tram /pipelines /data/state /app

USER tram

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')" || exit 1

EXPOSE 8765

ENTRYPOINT ["tram"]
CMD ["daemon"]
