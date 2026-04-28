"""Shared SNMP MIB source-store and compilation helpers."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MIB_HTTP_SOURCE_URL = "https://mibs.pysnmp.com/asn1/@mib@"
MIB_SOURCE_EXTENSIONS = ("", ".txt", ".mib", ".my")
SUPPORTED_MIB_SOURCE_FILE_HINT = "extensionless, .mib, .my, or .txt"
_MIB_SOURCE_SUFFIXES = {ext for ext in MIB_SOURCE_EXTENSIONS if ext}


class MibSupportUnavailable(RuntimeError):
    """Raised when optional pysmi support is not installed."""


class MibCompileFailure(RuntimeError):
    """Raised when pysmi fails to compile one or more MIBs."""


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


def available_compiled_mibs(mib_dir: str) -> set[str]:
    names: set[str] = set()
    if not os.path.isdir(mib_dir):
        return names
    for fname in os.listdir(mib_dir):
        if fname.endswith(".py") and not fname.startswith("_"):
            names.update(mib_candidates(fname[:-3]))
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
    """Delete compiled `.py` artifacts plus matching raw source files."""
    deleted_compiled: list[str] = []
    deleted_sources: list[str] = []
    seen_paths: set[str] = set()

    for candidate in mib_candidates(mib_name):
        compiled_path = Path(compiled_dir) / f"{candidate}.py"
        if compiled_path.is_file():
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
) -> MibCompileResult:
    """Compile one or more MIBs using local raw sources plus optional remote fallback."""
    requested = [name for name in mib_names if name]
    if not requested:
        return MibCompileResult(results={}, compiled=[], builtin_names=set())

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
