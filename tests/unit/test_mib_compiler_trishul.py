"""v1.5.0 lane A — trishul-smi compile path (TRAM_SNMP_STACK=trishul) tests.

These tests exercise the flag-on compile backend in ``tram/core/mib_compiler.py``:
raw ASN.1 MIBs compile to tsmi's native JSON IR bundle format (the
snmp-wire-harness reference format) inside ``TRAM_MIB_DIR`` alongside the
legacy pysmi ``.py`` corpus. No network is required — the fixtures are tiny
self-contained MIBs, plus the in-repo ``files/mibs/SNMPv2-SMI`` raw source
for the cross-module case.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("trishul_smi")

from tram.core.mib_compiler import (  # noqa: E402
    MibCompileFailure,
    MibSupportUnavailable,
    _run_async,
    available_compiled_mibs,
    compile_mibs,
    delete_mib_artifacts,
    is_mib_bundle_content,
    is_mib_bundle_json,
)

SELF_MIB = """SELF-MIB DEFINITIONS ::= BEGIN

selfMib OBJECT IDENTIFIER ::= { enterprises 99999 }

selfObject OBJECT-TYPE
    SYNTAX INTEGER
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "A self-contained test object"
    ::= { selfMib 1 }

END
"""

BASE_MIB = """BASE-MIB DEFINITIONS ::= BEGIN

baseRoot OBJECT IDENTIFIER ::= { enterprises 99998 }

baseObject OBJECT-TYPE
    SYNTAX INTEGER
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "Base object"
    ::= { baseRoot 1 }

END
"""

TOP_MIB = """TOP-MIB DEFINITIONS ::= BEGIN

IMPORTS
    MODULE-IDENTITY, OBJECT-TYPE FROM SNMPv2-SMI
    baseRoot FROM BASE-MIB;

topMib MODULE-IDENTITY
    LAST-UPDATED "202601010000Z"
    ORGANIZATION "TRAM"
    CONTACT-INFO "tram@example.com"
    DESCRIPTION "Top MIB"
    ::= { baseRoot 1 }

topObject OBJECT-TYPE
    SYNTAX INTEGER
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "Top object"
    ::= { topMib 1 }

END
"""

REPO_SNMPV2_SMI = Path(__file__).resolve().parents[2] / "files" / "mibs" / "SNMPv2-SMI"


def _write_source_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    for name, content in files.items():
        (source_dir / name).write_text(content)
    return source_dir


class TestTsmiCompile:
    def test_compile_trishul_writes_json_bundle(self, tmp_path):
        source_dir = _write_source_dir(tmp_path, {"SELF-MIB": SELF_MIB})
        compiled_dir = tmp_path / "compiled"

        result = compile_mibs(
            ["SELF-MIB"], str(compiled_dir), source_dirs=[str(source_dir)], stack="trishul"
        )

        assert result.compiled == ["SELF-MIB"]
        assert result.results == {"SELF-MIB": "compiled"}
        bundle_path = compiled_dir / "SELF-MIB.json"
        assert bundle_path.is_file(), "tsmi must write the JSON IR bundle"
        bundle = json.loads(bundle_path.read_text())
        assert bundle["module"] == "SELF-MIB"
        assert "selfObject" in bundle["objects"]
        assert bundle["objects"]["selfObject"]["oid"] == "1.3.6.1.4.1.99999.1"
        # sidecars matching the snmp-wire-harness reference output
        assert (compiled_dir / "manifest.json").is_file()
        assert (compiled_dir / "oid_index.json").is_file()
        # no legacy .py is produced on the trishul path
        assert not (compiled_dir / "SELF-MIB.py").is_file()

    def test_compile_trishul_selected_by_env_flag(self, tmp_path):
        source_dir = _write_source_dir(tmp_path, {"SELF-MIB": SELF_MIB})
        compiled_dir = tmp_path / "compiled"

        with patch.dict(os.environ, {"TRAM_SNMP_STACK": "trishul"}):
            result = compile_mibs(["SELF-MIB"], str(compiled_dir), source_dirs=[str(source_dir)])

        assert result.compiled == ["SELF-MIB"]
        assert (compiled_dir / "SELF-MIB.json").is_file()

    def test_compile_trishul_bundle_loads_via_tsnmp(self, tmp_path):
        """The bundle is tsmi's native output — loadable by trishul-snmp."""
        pytest.importorskip("trishul_snmp")
        from trishul_snmp import load_bundle

        source_dir = _write_source_dir(tmp_path, {"SELF-MIB": SELF_MIB})
        compiled_dir = tmp_path / "compiled"

        compile_mibs(["SELF-MIB"], str(compiled_dir), source_dirs=[str(source_dir)], stack="trishul")

        bundle = load_bundle(str(compiled_dir / "SELF-MIB.json"))
        assert bundle.resolve("SELF-MIB::selfObject") == (1, 3, 6, 1, 4, 1, 99999, 1)
        match = bundle.lookup("1.3.6.1.4.1.99999.1")
        assert match.symbol == "selfObject"
        assert tuple(match.oid) == (1, 3, 6, 1, 4, 1, 99999, 1)

    def test_compile_trishul_resolves_local_dependency(self, tmp_path):
        """A cross-module import resolves from the same source store."""
        assert REPO_SNMPV2_SMI.is_file(), "in-repo SNMPv2-SMI raw source must exist"
        source_dir = _write_source_dir(
            tmp_path,
            {"TOP-MIB": TOP_MIB, "BASE-MIB": BASE_MIB, "SNMPv2-SMI": REPO_SNMPV2_SMI.read_text()},
        )
        compiled_dir = tmp_path / "compiled"

        result = compile_mibs(["TOP-MIB"], str(compiled_dir), source_dirs=[str(source_dir)], stack="trishul")

        assert sorted(result.compiled) == ["BASE-MIB", "TOP-MIB"]
        assert result.results["TOP-MIB"] == "compiled"
        assert result.results["BASE-MIB"] == "compiled"
        assert (compiled_dir / "TOP-MIB.json").is_file()
        assert (compiled_dir / "BASE-MIB.json").is_file()

    def test_compile_trishul_missing_dependency_fails(self, tmp_path):
        source_dir = _write_source_dir(tmp_path, {"TOP-MIB": TOP_MIB})
        compiled_dir = tmp_path / "compiled"

        result = compile_mibs(["TOP-MIB"], str(compiled_dir), source_dirs=[str(source_dir)], stack="trishul")

        assert result.compiled == []
        assert result.results["TOP-MIB"] == "failed"
        assert not (compiled_dir / "TOP-MIB.json").is_file()

    def test_compile_trishul_raises_support_unavailable_when_missing(self, tmp_path):
        with patch.dict(sys.modules, {"trishul_smi": None}):
            with pytest.raises(MibSupportUnavailable, match="trishul-smi"):
                compile_mibs(["SELF-MIB"], str(tmp_path / "compiled"), stack="trishul")

    def test_compile_trishul_wraps_compile_errors(self, tmp_path):
        source_dir = _write_source_dir(tmp_path, {"SELF-MIB": SELF_MIB})
        with patch("tram.core.mib_compiler._run_async", side_effect=RuntimeError("boom")):
            with pytest.raises(MibCompileFailure, match="boom"):
                compile_mibs(
                    ["SELF-MIB"],
                    str(tmp_path / "compiled"),
                    source_dirs=[str(source_dir)],
                    stack="trishul",
                )


class TestTsmiCorpusHelpers:
    def test_available_compiled_mibs_counts_json_bundles_not_sidecars(self, tmp_path):
        compiled_dir = tmp_path / "compiled"
        compiled_dir.mkdir()
        (compiled_dir / "IF-MIB.py").write_text("# legacy")
        (compiled_dir / "IF-MIB.json").write_text('{"module": "IF-MIB", "objects": {}}')
        (compiled_dir / "manifest.json").write_text('{"modules": []}')
        (compiled_dir / "oid_index.json").write_text('{"oids": {}}')
        (compiled_dir / "junk.json").write_text("not a bundle")

        available = available_compiled_mibs(str(compiled_dir))

        assert "IF-MIB" in available
        assert "IF_MIB" in available
        assert "manifest" not in available
        assert "oid_index" not in available
        assert "junk" not in available

    def test_is_mib_bundle_json_detects_bundles_only(self, tmp_path):
        bundle = tmp_path / "M.json"
        bundle.write_text('{"module": "M", "objects": {}}')
        assert is_mib_bundle_json(bundle)

        for name, content in (
            ("manifest.json", '{"modules": []}'),
            ("oid_index.json", '{"oids": {}}'),
            ("broken.json", "{not json"),
        ):
            path = tmp_path / name
            path.write_text(content)
            assert not is_mib_bundle_json(path), name

    def test_is_mib_bundle_content_bytes_discriminator(self):
        """Review C7: the bytes-level discriminator rejects .py content that a
        pre-v1.5.0 manager would serve with HTTP 200 for ?format=json."""
        assert is_mib_bundle_content(b'{"module": "IF-MIB", "objects": {}}')
        assert not is_mib_bundle_content(b"# IF-MIB compiled")
        assert not is_mib_bundle_content(b'{"manifest": []}')
        assert not is_mib_bundle_content(b"not json at all")
        assert not is_mib_bundle_content(b"")
        assert not is_mib_bundle_content('\x00\xff binary'.encode("latin-1"))

    def test_tsmi_caching_reader_persists_and_serves_from_cache(self, tmp_path):
        """Review C5: the trishul backend's remote-reader wrapper persists
        fetched sources to the cache dir and serves from there on later
        compiles (no re-downloads; fetched sources become removable)."""
        from tram.core.mib_compiler import _TsmiCachingReader

        cache_dir = str(tmp_path / "source-cache")
        fetched: list[str] = []

        class _FakeRemote:
            async def fetch(self, mib_name):
                fetched.append(mib_name)
                return f"-- {mib_name} ASN.1 source --"

        import asyncio

        reader = _TsmiCachingReader(_FakeRemote(), cache_dir)
        assert asyncio.run(reader.fetch("SELF-MIB")) == "-- SELF-MIB ASN.1 source --"
        assert fetched == ["SELF-MIB"]
        assert (tmp_path / "source-cache" / "SELF-MIB.txt").is_file()

        # second fetch on the same instance and a fresh instance both serve
        # from the local store — the remote reader is not hit again
        assert asyncio.run(reader.fetch("SELF-MIB")) == "-- SELF-MIB ASN.1 source --"
        assert asyncio.run(_TsmiCachingReader(_FakeRemote(), cache_dir).fetch("SELF-MIB")) == "-- SELF-MIB ASN.1 source --"
        assert fetched == ["SELF-MIB"]

    def test_compile_trishul_wires_remote_cache_dir(self, tmp_path):
        """Review C5: compile_mibs forwards remote_cache_dir into the trishul
        backend, which wraps the remote reader in the caching reader."""
        from tram.core.mib_compiler import _TsmiCachingReader

        source_dir = _write_source_dir(tmp_path, {"SELF-MIB": SELF_MIB})
        compiled_dir = tmp_path / "compiled"
        cache_dir = tmp_path / "source-cache"

        class _FakeHttp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def fetch(self, mib_name):
                return f"-- {mib_name} --"

        with (
            patch("tram.core.mib_compiler._TsmiCachingReader", wraps=_TsmiCachingReader) as caching_mock,
            patch("trishul_smi.HttpReader", return_value=_FakeHttp()),
        ):
            compile_mibs(
                ["SELF-MIB"],
                str(compiled_dir),
                source_dirs=[str(source_dir)],
                resolve_missing=True,
                remote_cache_dir=str(cache_dir),
                stack="trishul",
            )

        assert caching_mock.called
        assert caching_mock.call_args.args[1] == str(cache_dir)
        assert (compiled_dir / "SELF-MIB.json").is_file()

    def test_delete_mib_artifacts_removes_both_formats(self, tmp_path):
        compiled_dir = tmp_path / "compiled"
        compiled_dir.mkdir()
        (compiled_dir / "IF-MIB.py").write_text("# legacy")
        (compiled_dir / "IF-MIB.json").write_text('{"module": "IF-MIB", "objects": {}}')
        (compiled_dir / "manifest.json").write_text('{"modules": []}')

        deleted = delete_mib_artifacts("IF-MIB", str(compiled_dir))

        assert deleted.compiled_files == ["IF-MIB.json", "IF-MIB.py"]
        assert not (compiled_dir / "IF-MIB.py").exists()
        assert not (compiled_dir / "IF-MIB.json").exists()
        assert (compiled_dir / "manifest.json").exists(), "sidecars survive a module delete"


class TestRunAsyncBridge:
    def test_run_async_from_sync_context(self):
        async def coro():
            return 42

        assert _run_async(coro) == 42

    def test_run_async_inside_running_loop(self):
        async def outer():
            async def coro():
                return 7

            return _run_async(coro)

        assert asyncio.run(outer()) == 7