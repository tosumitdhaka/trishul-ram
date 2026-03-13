"""Schema file management API — list, upload, retrieve, and delete schema files.

Also provides a transparent proxy to an external Confluent-compatible Schema Registry
(Confluent, Apicurio, Karapace …) via ``/api/schemas/registry/{path}``.

Endpoints:
    GET    /api/schemas                        list all local schema files (recursive)
    GET    /api/schemas/{filepath}             read a local schema file's content
    POST   /api/schemas/upload                 upload a local schema file
    DELETE /api/schemas/{filepath}             delete a local schema file

    ANY    /api/schemas/registry/{path}        transparent proxy to TRAM_SCHEMA_REGISTRY_URL
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Query
from fastapi.responses import PlainTextResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schemas", tags=["schemas"])

# Extensions accepted for upload
_ALLOWED_EXT = {".proto", ".avsc", ".json", ".xsd", ".yaml", ".yml"}

# Human-readable schema type by extension
_EXT_TO_TYPE = {
    ".proto": "protobuf",
    ".avsc":  "avro",
    ".json":  "json",
    ".xsd":   "xml",
    ".yaml":  "yaml",
    ".yml":   "yaml",
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _schema_dir() -> str:
    return os.environ.get("TRAM_SCHEMA_DIR", "/schemas")


def _safe_join(base: str, relative: str) -> str:
    """Return the absolute path of ``base/relative``, raising 400 on traversal.

    Uses ``os.path.normpath`` (no filesystem access) so it works for paths
    that do not yet exist.
    """
    # Normalise base to a canonical absolute path
    base_abs = os.path.normpath(os.path.abspath(base))
    # Normalise the candidate (resolves .., ., redundant seps)
    candidate = os.path.normpath(os.path.join(base_abs, relative))
    # The candidate must be the base itself OR strictly inside it
    if candidate != base_abs and not candidate.startswith(base_abs + os.sep):
        raise HTTPException(status_code=400, detail="Invalid path: outside schema directory")
    return candidate


def _schema_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return _EXT_TO_TYPE.get(ext, "other")


# ── Schema Registry proxy ─────────────────────────────────────────────────────
#
# MUST be registered before the /{filepath:path} catch-all so that requests
# to /registry/... are matched here first.


@router.api_route(
    "/registry/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    summary="Schema Registry proxy",
    description=(
        "Transparent HTTP proxy to the external Schema Registry configured via "
        "``TRAM_SCHEMA_REGISTRY_URL``.  All Confluent-compatible endpoints "
        "(subjects, schemas/ids, config …) are forwarded as-is, preserving "
        "method, query params, headers, and body.  "
        "Useful for UI integrations that want a single origin for both TRAM "
        "management and schema registry operations."
    ),
)
async def schema_registry_proxy(path: str, request: Request) -> Response:
    registry_url = os.environ.get("TRAM_SCHEMA_REGISTRY_URL", "").rstrip("/")
    if not registry_url:
        raise HTTPException(
            status_code=503,
            detail=(
                "Schema registry proxy not configured — "
                "set TRAM_SCHEMA_REGISTRY_URL (e.g. http://schema-registry:8081)"
            ),
        )

    target = f"{registry_url}/{path}"
    if request.query_params:
        target += f"?{request.query_params}"

    # Forward headers; skip hop-by-hop and host so httpx fills them correctly
    _skip_req = {"host", "accept-encoding", "content-length", "transfer-encoding", "connection"}
    forward_headers = {k: v for k, v in request.headers.items() if k.lower() not in _skip_req}

    body = await request.body()

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=target,
                headers=forward_headers,
                content=body or None,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Schema registry proxy error ({registry_url}): {exc}",
        )

    _skip_resp = {"transfer-encoding", "content-encoding", "connection"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _skip_resp}

    logger.debug(
        "Schema registry proxy",
        extra={"method": request.method, "path": path, "status": resp.status_code},
    )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
    )


# ── GET /api/schemas ─────────────────────────────────────────────────────────


@router.get("")
def list_schemas() -> list[dict]:
    """List all schema files under TRAM_SCHEMA_DIR (recursive).

    Returns one entry per file:

    * ``path``        — relative path from schema_dir (e.g. ``cisco/GenericRecord.proto``)
    * ``type``        — inferred schema type: ``protobuf``, ``avro``, ``json``, ``xml``, ``yaml``, ``other``
    * ``size_bytes``  — file size
    * ``schema_file`` — absolute path ready to paste into a pipeline ``schema_file:`` field
    """
    base = _schema_dir()
    if not os.path.isdir(base):
        return []

    entries: list[dict] = []
    for dirpath, _dirs, filenames in os.walk(base):
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, base)
            entries.append({
                "path":        rel_path,
                "type":        _schema_type(fname),
                "size_bytes":  os.path.getsize(abs_path),
                "schema_file": abs_path,
            })
    # Sort by relative path for stable output
    entries.sort(key=lambda e: e["path"])
    return entries


# ── GET /api/schemas/{filepath} ──────────────────────────────────────────────


@router.get("/{filepath:path}")
def get_schema(filepath: str) -> PlainTextResponse:
    """Return the raw text content of a schema file.

    ``filepath`` is relative to TRAM_SCHEMA_DIR, e.g. ``cisco/GenericRecord.proto``.
    """
    base = _schema_dir()
    full = _safe_join(base, filepath)

    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail=f"Schema '{filepath}' not found")

    try:
        content = open(full, "r", encoding="utf-8", errors="replace").read()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read schema: {exc}")

    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")


# ── POST /api/schemas/upload ─────────────────────────────────────────────────


@router.post("/upload")
async def upload_schema(
    file: UploadFile = File(...),
    subdir: str = Query(
        default="",
        description=(
            "Optional subdirectory within TRAM_SCHEMA_DIR "
            "(e.g. 'cisco' saves to TRAM_SCHEMA_DIR/cisco/).  "
            "Must not contain '..' components."
        ),
    ),
) -> dict:
    """Upload a schema file to TRAM_SCHEMA_DIR.

    Accepted extensions: ``.proto``, ``.avsc``, ``.json``, ``.xsd``,
    ``.yaml``, ``.yml``.

    When ``subdir`` is supplied the file is stored under that subdirectory.
    This is especially useful for Protobuf schemas that span multiple files:
    upload them all to the same subdir so that import paths resolve correctly.

    The response ``schema_file`` value is the absolute path to use in a
    pipeline YAML ``schema_file:`` field.

    Example — upload all Cisco EMS proto files::

        for f in *.proto; do
            curl -F "file=@$f" \\
                 "http://localhost:8765/api/schemas/upload?subdir=cisco"
        done

        # Then in pipeline YAML:
        serializer_in:
          type: protobuf
          schema_file: /schemas/cisco/GenericRecord.proto
          message_class: PerformanceMonitoringMessage
    """
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()

    if not filename:
        raise HTTPException(status_code=400, detail="Filename must not be empty")

    if ext not in _ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Extension '{ext}' is not allowed.  "
                f"Accepted: {', '.join(sorted(_ALLOWED_EXT))}"
            ),
        )

    # Validate and resolve target directory
    base = _schema_dir()
    if subdir:
        # Reject obvious traversal attempts before joining
        if ".." in subdir.split("/") or ".." in subdir.split(os.sep):
            raise HTTPException(status_code=400, detail="'subdir' must not contain '..' components")
        target_dir = _safe_join(base, subdir)
    else:
        target_dir = os.path.normpath(os.path.abspath(base))

    # Resolve final file path (guards against crafted filenames like "../../etc/passwd")
    dest_path = _safe_join(target_dir, os.path.basename(filename))

    os.makedirs(target_dir, exist_ok=True)

    content = await file.read()

    # Atomic write: write to a sibling .tmp file then rename
    tmp_path = dest_path + ".tmp"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(content)
        os.replace(tmp_path, dest_path)
    except Exception as exc:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}")

    rel_path = os.path.relpath(dest_path, base)
    logger.info(
        "Schema uploaded",
        extra={"path": rel_path, "size": len(content), "type": _schema_type(filename)},
    )
    return {
        "path":        rel_path,
        "type":        _schema_type(filename),
        "size_bytes":  len(content),
        "schema_file": dest_path,
        "schema_dir":  base,
    }


# ── DELETE /api/schemas/{filepath} ───────────────────────────────────────────


@router.delete("/{filepath:path}")
def delete_schema(filepath: str) -> dict:
    """Delete a schema file from TRAM_SCHEMA_DIR.

    ``filepath`` is relative to TRAM_SCHEMA_DIR, e.g. ``cisco/GenericRecord.proto``.
    Empty directories left behind after deletion are **not** removed automatically.
    """
    base = _schema_dir()
    full = _safe_join(base, filepath)

    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail=f"Schema '{filepath}' not found")

    os.remove(full)
    logger.info("Schema deleted", extra={"path": filepath})
    return {"deleted": filepath, "schema_dir": base}
