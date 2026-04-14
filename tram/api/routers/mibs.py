"""MIB management API — list, upload, download, and delete compiled MIB modules."""

from __future__ import annotations

import logging
import os
import tempfile

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mibs", tags=["mibs"])


def _mib_dir() -> str:
    return os.environ.get("TRAM_MIB_DIR", "/mibs")


# ── GET /api/mibs ────────────────────────────────────────────────────────────


@router.get("")
def list_mibs() -> list[dict]:
    """List all compiled MIB modules available in TRAM_MIB_DIR."""
    mib_dir = _mib_dir()
    if not os.path.isdir(mib_dir):
        return []
    entries = []
    for fname in sorted(os.listdir(mib_dir)):
        if fname.endswith(".py") and not fname.startswith("_"):
            fpath = os.path.join(mib_dir, fname)
            entries.append({
                "name": fname[:-3],   # strip .py
                "file": fname,
                "size_bytes": os.path.getsize(fpath),
            })
    return entries


# ── POST /api/mibs/upload ────────────────────────────────────────────────────


@router.post("/upload")
async def upload_mib(file: UploadFile = File(...)) -> dict:
    """Upload a raw .mib text file and compile it to TRAM_MIB_DIR.

    The file must have a ``.mib`` extension.  Any MIBs that the uploaded file
    imports that are not already present in TRAM_MIB_DIR must be downloaded
    separately first (see ``POST /api/mibs/download``).

    Requires ``pysmi-lextudio`` (``pip install tram[mib]``).
    """
    try:
        from pysmi.codegen.pysnmp import PySnmpCodeGen
        from pysmi.compiler import MibCompiler
        from pysmi.parser.smi import parserFactory
        from pysmi.reader import FileReader
        from pysmi.searcher import PyFileSearcher, StubSearcher
        from pysmi.writer import PyFileWriter
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail=(
                "MIB compilation requires pysmi-lextudio — "
                "install with: pip install tram[mib]"
            ),
        )

    if not (file.filename or "").endswith(".mib"):
        raise HTTPException(status_code=400, detail="Uploaded file must have a .mib extension")

    mib_dir = _mib_dir()
    os.makedirs(mib_dir, exist_ok=True)

    content = await file.read()
    mib_name = os.path.splitext(file.filename)[0]

    with tempfile.TemporaryDirectory(prefix="tram_mib_upload_") as tmpdir:
        mib_path = os.path.join(tmpdir, file.filename)
        with open(mib_path, "wb") as fh:
            fh.write(content)

        parser = parserFactory()()
        codegen = PySnmpCodeGen()
        writer = PyFileWriter(mib_dir)

        compiler = MibCompiler(parser, codegen, writer)
        compiler.addSources(FileReader(tmpdir))
        compiler.addSearchers(PyFileSearcher(mib_dir))
        compiler.addSearchers(StubSearcher(*(PySnmpCodeGen.baseMibs + PySnmpCodeGen.fakeMibs)))

        try:
            results = compiler.compile(mib_name)
        except Exception as exc:
            logger.error("MIB compilation failed for %s: %s", mib_name, exc)
            raise HTTPException(status_code=500, detail=f"Compilation failed: {exc}")

    compiled = [name for name, status in results.items() if status == "compiled"]
    logger.info("MIB uploaded and compiled", extra={"mib": mib_name, "compiled": compiled})
    return {"compiled": compiled, "mib_dir": mib_dir, "results": dict(results)}


# ── POST /api/mibs/download ──────────────────────────────────────────────────


class MibDownloadRequest(BaseModel):
    names: list[str]


@router.post("/download")
def download_mibs(body: MibDownloadRequest) -> dict:
    """Download and compile MIB modules by name from mibs.pysnmp.com.

    Downloads the named modules plus their dependencies and compiles them to
    Python format in TRAM_MIB_DIR.  Requires internet access at the time of
    the call and ``pysmi-lextudio`` (``pip install tram[mib]``).

    Example request body::

        {"names": ["IF-MIB", "ENTITY-MIB", "HOST-RESOURCES-MIB"]}
    """
    if not body.names:
        raise HTTPException(status_code=400, detail="'names' must be a non-empty list")

    try:
        from pysmi.codegen.pysnmp import PySnmpCodeGen
        from pysmi.compiler import MibCompiler
        from pysmi.parser.smi import parserFactory
        from pysmi.reader import HttpReader
        from pysmi.searcher import PyFileSearcher, StubSearcher
        from pysmi.writer import PyFileWriter
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail=(
                "MIB download requires pysmi-lextudio — "
                "install with: pip install tram[mib]"
            ),
        )

    mib_dir = _mib_dir()
    os.makedirs(mib_dir, exist_ok=True)

    parser = parserFactory()()
    codegen = PySnmpCodeGen()
    writer = PyFileWriter(mib_dir)

    compiler = MibCompiler(parser, codegen, writer)
    compiler.addSources(HttpReader("https://mibs.pysnmp.com/asn1/@mib@"))
    compiler.addSearchers(PyFileSearcher(mib_dir))
    compiler.addSearchers(StubSearcher(*(PySnmpCodeGen.baseMibs + PySnmpCodeGen.fakeMibs)))

    try:
        results = compiler.compile(*body.names)
    except Exception as exc:
        logger.error("MIB download/compilation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Download/compilation failed: {exc}")

    compiled = [name for name, status in results.items() if status == "compiled"]
    logger.info("MIBs downloaded", extra={"requested": body.names, "compiled": compiled})
    return {"compiled": compiled, "mib_dir": mib_dir, "results": dict(results)}


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
    for candidate in (mib_name, mib_name.replace("-", "_"), mib_name.replace("_", "-")):
        fpath = os.path.join(mib_dir, f"{candidate}.py")
        if os.path.isfile(fpath):
            with open(fpath, "rb") as fh:
                return PlainTextResponse(content=fh.read().decode("utf-8", errors="replace"))
    raise HTTPException(status_code=404, detail=f"MIB '{mib_name}' not found")


# ── DELETE /api/mibs/{mib_name} ──────────────────────────────────────────────


@router.delete("/{mib_name}")
def delete_mib(mib_name: str) -> dict:
    """Delete a compiled MIB module (.py file) from TRAM_MIB_DIR."""
    mib_dir = _mib_dir()
    mib_file = os.path.join(mib_dir, f"{mib_name}.py")
    if not os.path.isfile(mib_file):
        raise HTTPException(status_code=404, detail=f"MIB '{mib_name}' not found in {mib_dir}")
    os.remove(mib_file)
    logger.info("MIB deleted", extra={"mib": mib_name})
    return {"deleted": mib_name, "mib_dir": mib_dir}
