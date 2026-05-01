"""MIB management API — list, upload, download, and delete compiled MIB modules."""

from __future__ import annotations

import logging
import os
import re
import tempfile

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from tram.core.mib_compiler import (
    SUPPORTED_MIB_SOURCE_FILE_HINT,
    MibCompileFailure,
    MibSupportUnavailable,
    available_compiled_mibs,
    available_source_mibs,
    bundled_mib_source_dirs,
    compile_mibs,
    delete_mib_artifacts,
    is_supported_mib_source_filename,
    list_mib_source_files,
    mib_candidates,
    mib_source_dir,
    mib_source_module_name,
    persist_mib_source,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mibs", tags=["mibs"])

_IMPORTS_BLOCK_RE = re.compile(r"\bIMPORTS\b(?P<body>.*?);", re.IGNORECASE | re.DOTALL)
_FROM_MIB_RE = re.compile(r"\bFROM\s+([A-Za-z0-9][A-Za-z0-9_-]*)\b")
_LINE_COMMENT_RE = re.compile(r"--.*?$", re.MULTILINE)


def _mib_dir() -> str:
    return os.environ.get("TRAM_MIB_DIR", "/mibs")


def _extract_imported_mibs(text: str) -> list[str]:
    text = _LINE_COMMENT_RE.sub("", text)
    found: list[str] = []
    for match in _IMPORTS_BLOCK_RE.finditer(text):
        for mib_name in _FROM_MIB_RE.findall(match.group("body")):
            if mib_name not in found:
                found.append(mib_name)
    return found


def _classify_imports(
    mib_name: str,
    imported: list[str],
    builtin_names: set[str],
    local_available_before: set[str],
    compiled_before: set[str],
    available_after: set[str],
    compiled_after: set[str],
    results: dict,
) -> dict:
    builtin_imports = sorted(name for name in imported if name in builtin_names)
    local_imports = sorted(
        name for name in imported if name not in builtin_names and name in local_available_before
    )
    unresolved_imports = sorted(
        name
        for name in imported
        if name not in builtin_names and name not in available_after
    )
    resolved_imports = sorted(
        name
        for name in imported
        if name not in builtin_names
        and name not in local_available_before
        and name not in unresolved_imports
    )

    if mib_name in compiled_after or results.get(mib_name) == "compiled":
        target_status = "compiled"
    elif mib_name in builtin_names:
        target_status = "builtin_available"
    elif mib_name in compiled_before:
        target_status = "already_available"
    elif unresolved_imports:
        target_status = "unresolved_dependencies"
    elif results.get(mib_name) == "failed":
        target_status = "compile_failed"
    else:
        target_status = "no_change"

    return {
        "imports": imported,
        "builtin_imports": builtin_imports,
        "local_imports": local_imports,
        "resolved_imports": resolved_imports,
        "unresolved_imports": unresolved_imports,
        "target_status": target_status,
    }


def _mib_key(name: str) -> str:
    return min(mib_candidates(name))


def _scan_compiled_entries(mib_dir: str) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    if not os.path.isdir(mib_dir):
        return entries

    for fname in sorted(os.listdir(mib_dir)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        stem = fname[:-3]
        key = _mib_key(stem)
        path = os.path.join(mib_dir, fname)
        entries[key] = {
            "name": stem,
            "file": fname,
            "size_bytes": os.path.getsize(path),
        }
    return entries


def _scan_source_entries(*, include_bundled: bool, mib_dir: str) -> dict[str, dict]:
    source_dir = mib_source_dir(mib_dir)
    source_roots: list[tuple[str, str]] = [("local", source_dir)]
    if include_bundled:
        source_roots.extend(("bundled", path) for path in bundled_mib_source_dirs())

    entries: dict[str, dict] = {}
    for origin, root in source_roots:
        if not os.path.isdir(root):
            continue
        for path in list_mib_source_files(root, recursive=True):
            mib_name = mib_source_module_name(path.name)
            if mib_name is None:
                continue
            key = _mib_key(mib_name)
            current = entries.get(key)
            if current and current["origin"] == "local":
                continue
            entries[key] = {
                "name": mib_name,
                "file": str(path.relative_to(root)),
                "size_bytes": path.stat().st_size,
                "origin": origin,
                "path": path,
            }
    return entries


# ── GET /api/mibs ────────────────────────────────────────────────────────────


@router.get("")
def list_mibs() -> list[dict]:
    """List locally managed MIB modules with raw/compiled artifact visibility.

    The page intentionally focuses on the local managed set:
    - raw ASN.1 sources in the local source store
    - compiled Python modules in TRAM_MIB_DIR

    Bundled readonly base MIBs are not listed on their own to avoid flooding
    the UI, but their raw source is surfaced as a fallback when a listed local
    module also exists in the bundled source tree.
    """
    mib_dir = _mib_dir()
    compiled_entries = _scan_compiled_entries(mib_dir)
    local_source_entries = _scan_source_entries(include_bundled=False, mib_dir=mib_dir)
    source_entries_with_fallback = _scan_source_entries(include_bundled=True, mib_dir=mib_dir)

    keys = sorted(set(compiled_entries) | set(local_source_entries))
    rows: list[dict] = []
    for key in keys:
        compiled = compiled_entries.get(key)
        raw = source_entries_with_fallback.get(key)
        display_name = (
            (local_source_entries.get(key) or raw or compiled or {}).get("name")
            or key
        )
        rows.append({
            "name": display_name,
            "raw_available": raw is not None,
            "raw_file": raw["file"] if raw else None,
            "raw_size_bytes": raw["size_bytes"] if raw else None,
            "raw_origin": raw["origin"] if raw else None,
            "compiled_available": compiled is not None,
            "compiled_file": compiled["file"] if compiled else None,
            "compiled_size_bytes": compiled["size_bytes"] if compiled else None,
        })
    return rows


# ── POST /api/mibs/upload ────────────────────────────────────────────────────


@router.post("/upload")
async def upload_mib(
    file: UploadFile = File(...),
    resolve_missing: bool = Query(
        False,
        description="When true, also try mibs.pysnmp.com for missing imported MIBs.",
    ),
) -> dict:
    """Upload a raw ASN.1 MIB source file and compile it to TRAM_MIB_DIR.

    Supported source filenames match pysmi's reader behavior: extensionless,
    ``.mib``, ``.my``, or ``.txt`` (case-insensitive). Uploads compile against
    the persisted local ASN.1 source store plus compiled artifacts in
    ``TRAM_MIB_DIR``. When ``resolve_missing=true``, missing imports are also
    fetched from ``mibs.pysnmp.com`` and cached locally.

    Requires ``tram[mib]``.
    """
    filename = file.filename or ""
    if not is_supported_mib_source_filename(filename):
        raise HTTPException(
            status_code=400,
            detail=(
                "Uploaded file must be a supported ASN.1 MIB source filename "
                f"({SUPPORTED_MIB_SOURCE_FILE_HINT})"
            ),
        )

    mib_dir = _mib_dir()
    source_dir = mib_source_dir(mib_dir)
    bundled_source_dirs = bundled_mib_source_dirs()
    os.makedirs(mib_dir, exist_ok=True)

    content = await file.read()
    mib_name = mib_source_module_name(filename)
    assert mib_name is not None
    imported_mibs = _extract_imported_mibs(content.decode("utf-8", errors="ignore"))
    compiled_before = available_compiled_mibs(mib_dir)
    local_available_before = compiled_before | available_source_mibs(source_dir)
    for bundled_dir in bundled_source_dirs:
        local_available_before |= available_source_mibs(bundled_dir)
    persist_mib_source(source_dir, filename, content)

    with tempfile.TemporaryDirectory(prefix="tram_mib_upload_") as tmpdir:
        persist_mib_source(tmpdir, filename, content)
        try:
            compile_result = compile_mibs(
                [mib_name],
                mib_dir,
                source_dirs=[tmpdir, source_dir, *bundled_source_dirs],
                resolve_missing=resolve_missing,
                remote_cache_dir=source_dir if resolve_missing else None,
            )
        except MibSupportUnavailable as exc:
            raise HTTPException(status_code=501, detail=str(exc))
        except MibCompileFailure as exc:
            logger.error("MIB compilation failed for %s: %s", mib_name, exc)
            raise HTTPException(status_code=500, detail=f"Compilation failed: {exc}")

    compiled_after = available_compiled_mibs(mib_dir)
    available_after = compiled_after | available_source_mibs(source_dir)
    for bundled_dir in bundled_source_dirs:
        available_after |= available_source_mibs(bundled_dir)
    classification = _classify_imports(
        mib_name=mib_name,
        imported=imported_mibs,
        builtin_names=compile_result.builtin_names,
        local_available_before=local_available_before,
        compiled_before=compiled_before,
        available_after=available_after,
        compiled_after=compiled_after,
        results=compile_result.results,
    )
    logger.info(
        "MIB uploaded and compiled",
        extra={
            "mib": mib_name,
            "compiled": compile_result.compiled,
            "resolve_missing": resolve_missing,
            "target_status": classification["target_status"],
            "unresolved_imports": classification["unresolved_imports"],
        },
    )
    return {
        "compiled": compile_result.compiled,
        "mib_dir": mib_dir,
        "mib_source_dir": source_dir,
        "results": compile_result.results,
        "resolve_missing": resolve_missing,
        **classification,
    }


# ── POST /api/mibs/download ──────────────────────────────────────────────────


class MibDownloadRequest(BaseModel):
    names: list[str]


@router.post("/download")
def download_mibs(body: MibDownloadRequest) -> dict:
    """Download and compile MIB modules by name from mibs.pysnmp.com.

    Downloads the named modules plus their dependencies and compiles them to
    Python format in ``TRAM_MIB_DIR`` while also caching raw ASN.1 sources in
    the local source-store directory. Requires internet access at the time of
    the call and ``tram[mib]``.

    Example request body::

        {"names": ["IF-MIB", "ENTITY-MIB", "HOST-RESOURCES-MIB"]}
    """
    if not body.names:
        raise HTTPException(status_code=400, detail="'names' must be a non-empty list")

    mib_dir = _mib_dir()
    source_dir = mib_source_dir(mib_dir)
    bundled_source_dirs = bundled_mib_source_dirs()
    os.makedirs(mib_dir, exist_ok=True)

    try:
        compile_result = compile_mibs(
            body.names,
            mib_dir,
            source_dirs=[source_dir, *bundled_source_dirs],
            resolve_missing=True,
            remote_cache_dir=source_dir,
        )
    except MibSupportUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except MibCompileFailure as exc:
        logger.error("MIB download/compilation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Download/compilation failed: {exc}")

    logger.info("MIBs downloaded", extra={"requested": body.names, "compiled": compile_result.compiled})
    return {
        "compiled": compile_result.compiled,
        "mib_dir": mib_dir,
        "mib_source_dir": source_dir,
        "results": compile_result.results,
    }


# ── GET /api/mibs/{mib_name}/source ──────────────────────────────────────────


@router.get("/{mib_name}/source")
def get_mib_source(mib_name: str):
    """Return raw ASN.1 source for a MIB module when available."""
    from fastapi.responses import PlainTextResponse

    mib_dir = _mib_dir()
    source_entries = _scan_source_entries(include_bundled=True, mib_dir=mib_dir)
    entry = source_entries.get(_mib_key(mib_name))
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Raw MIB source '{mib_name}' not found")

    path = entry["path"]
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


# ── GET /api/mibs/{mib_name} ─────────────────────────────────────────────────


@router.get("/{mib_name}")
def get_mib(mib_name: str):
    """Return the compiled Python source of a MIB module.

    Used by worker agents to pull custom MIB files from the manager on
    demand before executing a pipeline.  Standard MIBs baked into the
    image (IF-MIB, ENTITY-MIB, …) are never stored here so a 404 for
    those is expected and handled gracefully by the worker.

    Accepts both dash and underscore naming (e.g. ``IF-MIB`` or ``IF_MIB``).
    """
    from fastapi.responses import PlainTextResponse

    mib_dir = _mib_dir()
    # pysnmp compiles MIB names with dashes replaced by underscores in filenames
    for candidate in mib_candidates(mib_name):
        fpath = os.path.join(mib_dir, f"{candidate}.py")
        if os.path.isfile(fpath):
            with open(fpath, "rb") as fh:
                return PlainTextResponse(content=fh.read().decode("utf-8", errors="replace"))
    raise HTTPException(status_code=404, detail=f"MIB '{mib_name}' not found")


# ── DELETE /api/mibs/{mib_name} ──────────────────────────────────────────────


@router.delete("/{mib_name}")
def delete_mib(mib_name: str) -> dict:
    """Delete compiled and raw-source artifacts for a MIB module."""
    mib_dir = _mib_dir()
    source_dir = mib_source_dir(mib_dir)
    deleted = delete_mib_artifacts(mib_name, mib_dir, source_dir)
    if deleted.compiled_files or deleted.source_files:
        logger.info(
            "MIB deleted",
            extra={
                "mib": mib_name,
                "compiled_files": deleted.compiled_files,
                "source_files": deleted.source_files,
            },
        )
        return {
            "deleted": mib_name,
            "compiled_files": deleted.compiled_files,
            "source_files": deleted.source_files,
            "mib_dir": mib_dir,
            "mib_source_dir": source_dir,
        }
    raise HTTPException(status_code=404, detail=f"MIB '{mib_name}' not found in {mib_dir}")
