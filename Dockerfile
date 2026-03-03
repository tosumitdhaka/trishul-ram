# Multi-stage Dockerfile for TRAM
# Stage 1: Build wheel
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY tram/ ./tram/

RUN pip install --no-cache-dir build && python -m build --wheel

# Stage 2: Runtime image
FROM python:3.11-slim

# Non-root user
RUN useradd -m -u 1000 -s /bin/bash tram

WORKDIR /app

# Install wheel
COPY --from=builder /build/dist/*.whl .
RUN whl=$(ls *.whl) && pip install --no-cache-dir "${whl}[metrics]" && rm *.whl

# Create default pipeline and data directories; give tram user a home .tram dir
RUN mkdir -p /pipelines /data && \
    mkdir -p /home/tram/.tram && \
    chown -R tram:tram /pipelines /data /home/tram /app

USER tram

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')" || exit 1

EXPOSE 8765

ENTRYPOINT ["tram"]
CMD ["daemon"]
