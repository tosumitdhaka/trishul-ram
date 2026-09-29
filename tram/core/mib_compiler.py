"""Shared SNMP MIB source-store and compilation helpers.

v1.5.0 (GH #72): the compile path is stack-aware. With the default
``TRAM_SNMP_STACK=legacy`` the pysmi pipeline compiles to ``.py`` exactly as
before. With ``TRAM_SNMP_STACK=trishul`` the tsmi compiler (trishul-smi)
produces its JSON IR bundles (``<MODULE>.json`` plus ``manifest.json`` /
``oid_index.json`` sidecars) into the same ``TRAM_MIB_DIR`` — a dual-format
corpus where ``IF-MIB.py`` and ``IF-MIB.json`` coexist.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from tram.core.config import snmp_stack

MIB_HTTP_SOURCE_URL = "https://mibs.pysnmp.com/asn1/@mib@"
MIB_SOURCE_EXTENSIONS = ("", ".txt", ".mib", ".my")
SUPPORTED_MIB_SOURCE_FILE_HINT = "extensionless, .mib, .my, or .txt"
_MIB_SOURCE_SUFFIXES = {ext for ext in MIB_SOURCE_EXTENSIONS if ext}


class MibSupportUnavailable(RuntimeError):
    """Raised when the compile backend for the active stack is not installed."""


class MibCompileFailure(RuntimeError):
    """Raised when the active compile backend fails to compile one or more MIBs."""


@dataclass(frozen=True)
class MibCompileResult:
    """Normalized compile result shared by API and CLI callers."""

    results: dict[str, str]
    compiled: list[str]
    builtin_names: set[str]


@dataclass(frozen=True)
class MibDeleteResult:
    """Deleted compiled/raw artifacts for a MIB module."""

    compiled_files: list[str]
    source_files: list[str]


def mib_source_module_name(filename: str) -> str | None:
    """Return the MIB module name encoded by a supported source filename."""
    name = Path(filename).name
    if not name or name.startswith("."):
        return None

    suffix = Path(name).suffix
    if suffix and suffix.lower() not in _MIB_SOURCE_SUFFIXES:
        return None

    module_name = Path(name).stem if suffix else name
    return module_name or None


def is_supported_mib_source_filename(filename: str) -> bool:
    """Return whether the filename matches a supported ASN.1 MIB source pattern."""
    return mib_source_module_name(filename) is not None


def list_mib_source_files(source_dir: str | Path, *, recursive: bool = False) -> list[Path]:
    """List supported MIB source files from a directory."""
    base = Path(source_dir)
    if not base.is_dir():
        return []

    iterator = base.rglob("*") if recursive else base.iterdir()
    return sorted(
        (
            path
            for path in iterator
            if path.is_file() and is_supported_mib_source_filename(path.name)
            and not any(part.startswith(".") for part in path.relative_to(base).parts)
        ),
        key=lambda path: str(path.relative_to(base)).lower(),
    )


def mib_candidates(mib_name: str) -> list[str]:
    """Return possible dash/underscore filename stems for a MIB name."""
    return [mib_name, mib_name.replace("-", "_"), mib_name.replace("_", "-")]


def normalize_name_set(names: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for name in names:
        normalized.update(mib_candidates(name))
    return normalized


def mib_source_dir(mib_dir: str | None = None) -> str:
    """Return the raw ASN.1 MIB source store directory."""
    configured = os.environ.get("TRAM_MIB_SOURCE_DIR")
    if configured:
        return configured

    resolved_mib_dir = os.path.normpath(mib_dir or os.environ.get("TRAM_MIB_DIR", "/mibs"))
    parent = os.path.dirname(resolved_mib_dir)
    if not parent or parent == resolved_mib_dir:
        return "mib-sources"
    if parent == os.sep:
        return os.path.join(parent, "mib-sources")
    return os.path.join(parent, "mib-sources")


def bundled_mib_source_dirs() -> list[str]:
    """Return bundled readonly ASN.1 MIB source directories, if present."""
    configured = os.environ.get("TRAM_MIB_BUNDLED_SOURCE_DIR", "/mib-sources")
    candidates = [os.path.normpath(path) for path in configured.split(os.pathsep) if path]
    return list(dict.fromkeys(path for path in candidates if os.path.isdir(path)))


def is_mib_bundle_json(path: str | Path) -> bool:
    """Return whether *path* holds a tsmi JSON IR bundle (not a sidecar).

    The tsmi compile path writes one ``<MODULE>.json`` per module plus the
    ``manifest.json`` / ``oid_index.json`` sidecars into the same directory.
    A bundle is a JSON object carrying a ``module`` name and ``objects`` key;
    the sidecars do not, so this cheap content check keeps the directory
    scans (list / available / delete) from treating them as modules.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and isinstance(data.get("module"), str)
        and bool(data.get("module"))
        and isinstance(data.get("objects"), dict)
    )


def available_compiled_mibs(mib_dir: str) -> set[str]:
    names: set[str] = set()
    if not os.path.isdir(mib_dir):
        return names
    for fname in os.listdir(mib_dir):
        if fname.startswith("_"):
            continue
        if fname.endswith(".py"):
            names.update(mib_candidates(fname[:-3]))
        elif fname.endswith(".json") and is_mib_bundle_json(os.path.join(mib_dir, fname)):
            names.update(mib_candidates(fname[:-5]))
    return names


def available_source_mibs(source_dir: str) -> set[str]:
    names: set[str] = set()
    for path in list_mib_source_files(source_dir, recursive=True):
        mib_name = mib_source_module_name(path.name)
        if mib_name:
            names.update(mib_candidates(mib_name))

    return names


def persist_mib_source(source_dir: str, filename: str, content: bytes | str) -> Path:
    """Persist a raw ASN.1 MIB source file into the source store."""
    target = Path(source_dir) / Path(filename).name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return target


def delete_mib_artifacts(
    mib_name: str,
    compiled_dir: str,
    source_dir: str | None = None,
) -> MibDeleteResult:
    """Delete compiled artifacts (``.py`` and tsmi ``.json`` bundles) plus
    matching raw sources."""
    deleted_compiled: list[str] = []
    deleted_sources: list[str] = []
    seen_paths: set[str] = set()

    for candidate in mib_candidates(mib_name):
        for suffix in (".py", ".json"):
            compiled_path = Path(compiled_dir) / f"{candidate}{suffix}"
            if compiled_path.is_file() and (
                suffix == ".py" or is_mib_bundle_json(compiled_path)
            ):
                compiled_path.unlink()
                deleted_compiled.append(compiled_path.name)
                seen_paths.add(str(compiled_path))

    if source_dir and Path(source_dir).is_dir():
        candidate_names = set(mib_candidates(mib_name))
        for path in list_mib_source_files(source_dir, recursive=True):
            source_name = mib_source_module_name(path.name)
            if source_name not in candidate_names:
                continue
            if str(path) in seen_paths:
                continue

            path.unlink()
            deleted_sources.append(str(path.relative_to(source_dir)))
            seen_paths.add(str(path))

    return MibDeleteResult(
        compiled_files=sorted(deleted_compiled),
        source_files=sorted(deleted_sources),
    )


def compile_mibs(
    mib_names: Iterable[str],
    compiled_dir: str,
    *,
    source_dirs: Iterable[str] = (),
    resolve_missing: bool = False,
    remote_cache_dir: str | None = None,
    stack: str | None = None,
) -> MibCompileResult:
    """Compile one or more MIBs using local raw sources plus optional remote fallback.

    The backend follows the active ``TRAM_SNMP_STACK`` flag: ``legacy``
    compiles via pysmi to ``.py`` (byte-identical behavior), ``trishul``
    compiles via trishul-smi to JSON IR bundles in the same directory. An
    explicit *stack* overrides the flag (used by tests and by callers that
    must pin a backend regardless of the process environment).
    """
    requested = [name for name in mib_names if name]
    if not requested:
        return MibCompileResult(results={}, compiled=[], builtin_names=set())

    active = stack if stack is not None else snmp_stack()
    if active == "trishul":
        return _compile_mibs_trishul(
            requested,
            compiled_dir,
            source_dirs=source_dirs,
            resolve_missing=resolve_missing,
        )

    try:
        from pysmi.codegen.pysnmp import PySnmpCodeGen
        from pysmi.compiler import MibCompiler
        from pysmi.parser.smi import parserFactory
        from pysmi.reader import FileReader
        from pysmi.searcher import PyFileSearcher, StubSearcher
        from pysmi.writer import PyFileWriter

        HttpReader = None
        if resolve_missing:
            from pysmi.reader import HttpReader
    except ImportError as exc:
        raise MibSupportUnavailable(
            "MIB compilation requires pysmi — install with: pip install tram[mib]"
        ) from exc

    os.makedirs(compiled_dir, exist_ok=True)
    if remote_cache_dir:
        os.makedirs(remote_cache_dir, exist_ok=True)

    parser = parserFactory()()
    codegen = PySnmpCodeGen()
    writer = PyFileWriter(compiled_dir)

    compiler = MibCompiler(parser, codegen, writer)

    for source_dir in dict.fromkeys(str(path) for path in source_dirs if path):
        compiler.addSources(FileReader(source_dir))

    if resolve_missing and HttpReader is not None:
        remote_reader: object = HttpReader(MIB_HTTP_SOURCE_URL)
        if remote_cache_dir:
            remote_reader = _CachingReader(remote_reader, remote_cache_dir)
        compiler.addSources(remote_reader)

    compiler.addSearchers(PyFileSearcher(compiled_dir))
    compiler.addSearchers(StubSearcher(*(PySnmpCodeGen.baseMibs + PySnmpCodeGen.fakeMibs)))

    try:
        results = dict(compiler.compile(*requested))
    except Exception as exc:
        raise MibCompileFailure(str(exc)) from exc

    return MibCompileResult(
        results=results,
        compiled=[name for name, status in results.items() if status == "compiled"],
        builtin_names=normalize_name_set(set(PySnmpCodeGen.baseMibs + PySnmpCodeGen.fakeMibs)),
    )


def _compile_mibs_trishul(
    requested: list[str],
    compiled_dir: str,
    *,
    source_dirs: Iterable[str],
    resolve_missing: bool,
) -> MibCompileResult:
    """Compile MIBs via trishul-smi to its native JSON IR bundle format.

    This is the ``TRAM_SNMP_STACK=trishul`` backend. Output layout matches
    the snmp-wire-harness reference (scripts/snmp-wire-harness/scripts/
    02_compile_mibs.py): one ``<MODULE>.json`` per compiled module plus the
    ``manifest.json`` and ``oid_index.json`` sidecars, written alongside the
    pysmi ``.py`` corpus in *compiled_dir* (dual-format corpus). The JSON
    bundle format is tsmi's native output — the exact format trishul-snmp's
    ``load_bundle`` consumes.
    """
    try:
        from trishul_smi import CompilerConfig, FileReader, MibCompiler
    except ImportError as exc:
        raise MibSupportUnavailable(
            "MIB compilation requires trishul-smi — install with: pip install tram[snmp]"
        ) from exc

    os.makedirs(compiled_dir, exist_ok=True)

    config = CompilerConfig(
        output_dir=Path(compiled_dir),
        formats=["json"],
        emit_manifest=True,
        emit_oid_index=True,
        reproducible=True,
        cache_dir=None,
    )
    compiler = MibCompiler(config)
    for source_dir in dict.fromkeys(str(path) for path in source_dirs if path):
        compiler.add_reader(FileReader(source_dir))

    async def _run() -> list:
        if resolve_missing:
            from trishul_smi import HttpReader

            async with HttpReader(MIB_HTTP_SOURCE_URL) as http_reader:
                compiler.add_reader(http_reader)
                return await compiler.compile(*requested)
        return await compiler.compile(*requested)

    try:
        results = _run_async(_run)
    except Exception as exc:
        raise MibCompileFailure(str(exc)) from exc

    # tsmi CompileResult.status ∈ {"compiled", "cached", "failed", "missing"}; the
    # routers' classification understands "compiled"/"failed", matching pysmi.
    try:
        from trishul_smi.parser._constants import BASE_MIBS as _TSMI_BASE_MIBS
    except ImportError:  # pragma: no cover - pinned trishul-smi==0.5.2 always ships it
        _TSMI_BASE_MIBS = frozenset()

    return MibCompileResult(
        results={r.name: r.status for r in results},
        compiled=[r.name for r in results if r.status == "compiled"],
        builtin_names=normalize_name_set(_TSMI_BASE_MIBS),
    )


def _run_async[T](coro_factory: Callable[[], Awaitable[T]]) -> T:
    """Run an async coroutine from sync code, safe inside a running loop.

    tsmi's ``MibCompiler.compile()`` is async. ``asyncio.run`` raises when an
    event loop is already running (FastAPI endpoints), so the coroutine runs
    in a fresh loop on a worker thread in that case. This mirrors the async
    bridge pattern used by the trishul-snmp-suite reference consumer.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro_factory()).result()


class _CachingReader:
    """Wrap a remote reader and persist fetched raw ASN.1 source locally.

    Some pysmi variants use camelCase ``getData`` while others call snake_case
    ``get_data``. Expose both and delegate to whichever method the wrapped
    reader actually provides.
    """

    def __init__(self, reader: object, cache_dir: str):
        self._reader = reader
        self._cache_dir = cache_dir

    def __str__(self) -> str:  # pragma: no cover - trivial passthrough
        return str(self._reader)

    def getData(self, mibname: str, **options):
        mib_info, mib_text = _reader_get(self._reader, mibname, **options)
        persist_mib_source(self._cache_dir, getattr(mib_info, "file", mibname), mib_text)
        return mib_info, mib_text

    def get_data(self, mibname: str, **options):
        return self.getData(mibname, **options)

    def __getattr__(self, name: str):
        return getattr(self._reader, name)


def _reader_get(reader: object, mibname: str, **options):
    """Call a reader across pysmi API variants."""
    getter = getattr(reader, "getData", None)
    if getter is None:
        getter = getattr(reader, "get_data", None)
    if getter is None:
        raise AttributeError(f"{type(reader).__name__!s} reader has no getData/get_data method")
    return getter(mibname, **options)
