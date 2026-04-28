"""Tests for shared SNMP MIB compilation helpers."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tram.core.mib_compiler import (
    MIB_HTTP_SOURCE_URL,
    MibCompileFailure,
    MibSupportUnavailable,
    _CachingReader,
    available_source_mibs,
    bundled_mib_source_dirs,
    compile_mibs,
    delete_mib_artifacts,
    is_supported_mib_source_filename,
    list_mib_source_files,
    mib_source_module_name,
)


def _module(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _package(name: str, **attrs) -> ModuleType:
    mod = _module(name, **attrs)
    mod.__path__ = []
    return mod


def _mock_pysmi_modules(
    mock_codegen: MagicMock,
    mock_compiler: MagicMock,
    *,
    file_reader: MagicMock | None = None,
    http_reader: MagicMock | None = None,
) -> dict[str, object]:
    codegen_cls = MagicMock(return_value=mock_codegen)
    codegen_cls.baseMibs = mock_codegen.baseMibs
    codegen_cls.fakeMibs = mock_codegen.fakeMibs
    compiler_cls = MagicMock(return_value=mock_compiler)
    file_reader_fn = file_reader or MagicMock(side_effect=lambda path: f"file:{path}")
    http_reader_fn = http_reader or MagicMock()
    pyfile_searcher_fn = MagicMock(side_effect=lambda path: f"search:{path}")
    stub_searcher_fn = MagicMock(return_value="stub-searcher")
    pyfile_writer_fn = MagicMock(return_value="writer")

    def parser_factory_fn():
        return lambda: MagicMock()

    pysmi_mod = _package("pysmi")
    pysmi_codegen_pysnmp_mod = _module("pysmi.codegen.pysnmp", PySnmpCodeGen=codegen_cls)
    pysmi_codegen_mod = _package("pysmi.codegen", pysnmp=pysmi_codegen_pysnmp_mod)
    pysmi_compiler_mod = _module("pysmi.compiler", MibCompiler=compiler_cls)
    pysmi_parser_smi_mod = _module("pysmi.parser.smi", parserFactory=parser_factory_fn)
    pysmi_parser_mod = _package("pysmi.parser", smi=pysmi_parser_smi_mod)
    pysmi_reader_mod = _module("pysmi.reader", FileReader=file_reader_fn, HttpReader=http_reader_fn)
    pysmi_searcher_mod = _module(
        "pysmi.searcher",
        PyFileSearcher=pyfile_searcher_fn,
        StubSearcher=stub_searcher_fn,
    )
    pysmi_writer_mod = _module("pysmi.writer", PyFileWriter=pyfile_writer_fn)

    pysmi_mod.codegen = pysmi_codegen_mod
    pysmi_mod.compiler = pysmi_compiler_mod
    pysmi_mod.parser = pysmi_parser_mod
    pysmi_mod.reader = pysmi_reader_mod
    pysmi_mod.searcher = pysmi_searcher_mod
    pysmi_mod.writer = pysmi_writer_mod

    return {
        "pysmi": pysmi_mod,
        "pysmi.codegen": pysmi_codegen_mod,
        "pysmi.codegen.pysnmp": pysmi_codegen_pysnmp_mod,
        "pysmi.compiler": pysmi_compiler_mod,
        "pysmi.parser": pysmi_parser_mod,
        "pysmi.parser.smi": pysmi_parser_smi_mod,
        "pysmi.reader": pysmi_reader_mod,
        "pysmi.searcher": pysmi_searcher_mod,
        "pysmi.writer": pysmi_writer_mod,
    }


def test_compile_mibs_uses_local_sources_and_remote_cache():
    mock_codegen = MagicMock()
    mock_codegen.baseMibs = ("SNMPv2-SMI",)
    mock_codegen.fakeMibs = ("__FAKE__",)

    mock_compiler = MagicMock()
    mock_compiler.compile.return_value = {"TEST-MIB": "compiled"}

    remote_reader = MagicMock()
    remote_reader.getData.return_value = (
        SimpleNamespace(file="REMOTE-MIB.mib"),
        "REMOTE-MIB DEFINITIONS ::= BEGIN END",
    )
    mock_http_reader = MagicMock(return_value=remote_reader)

    with tempfile.TemporaryDirectory() as compiled_dir, tempfile.TemporaryDirectory() as cache_dir:
        source_dirs = ["/upload", cache_dir]
        with patch.dict(
            sys.modules,
            _mock_pysmi_modules(
                mock_codegen,
                mock_compiler,
                http_reader=mock_http_reader,
            ),
        ):
            result = compile_mibs(
                ["TEST-MIB"],
                compiled_dir,
                source_dirs=source_dirs,
                resolve_missing=True,
                remote_cache_dir=cache_dir,
            )

        assert result.compiled == ["TEST-MIB"]
        assert "SNMPv2-SMI" in result.builtin_names
        mock_http_reader.assert_called_once_with(MIB_HTTP_SOURCE_URL)

        added_sources = [call.args[0] for call in mock_compiler.addSources.call_args_list]
        assert added_sources[0] == "file:/upload"
        assert added_sources[1] == f"file:{cache_dir}"

        remote_source = added_sources[2]
        remote_source.getData("REMOTE-MIB")
        assert Path(cache_dir, "REMOTE-MIB.mib").read_text() == "REMOTE-MIB DEFINITIONS ::= BEGIN END"


def test_caching_reader_supports_snake_case_reader_and_wrapper_method():
    class SnakeReader:
        def get_data(self, mibname, **options):
            return (
                SimpleNamespace(file=f"{mibname}.mib"),
                f"{mibname} DEFINITIONS ::= BEGIN END",
            )

        def __str__(self):
            return "snake-reader"

    with tempfile.TemporaryDirectory() as cache_dir:
        reader = _CachingReader(SnakeReader(), cache_dir)
        mib_info, mib_text = reader.get_data("TEST-MIB")

        assert mib_info.file == "TEST-MIB.mib"
        assert mib_text == "TEST-MIB DEFINITIONS ::= BEGIN END"
        assert Path(cache_dir, "TEST-MIB.mib").read_text() == "TEST-MIB DEFINITIONS ::= BEGIN END"


def test_caching_reader_exposes_both_method_names_for_actual_pysmi_reader():
    reader_mod = pytest.importorskip("pysmi.reader")
    HttpReader = getattr(reader_mod, "HttpReader")

    with tempfile.TemporaryDirectory() as cache_dir:
        wrapped = _CachingReader(HttpReader(MIB_HTTP_SOURCE_URL), cache_dir)

    assert hasattr(wrapped, "getData")
    assert hasattr(wrapped, "get_data")


def test_compile_mibs_raises_support_unavailable_when_pysmi_missing():
    with patch.dict(sys.modules, {"pysmi": None, "pysmi.compiler": None}):
        with pytest.raises(MibSupportUnavailable):
            compile_mibs(["TEST-MIB"], "/tmp/compiled")


def test_compile_mibs_wraps_compile_errors():
    mock_codegen = MagicMock()
    mock_codegen.baseMibs = ("SNMPv2-SMI",)
    mock_codegen.fakeMibs = ("__FAKE__",)

    mock_compiler = MagicMock()
    mock_compiler.compile.side_effect = RuntimeError("boom")

    with tempfile.TemporaryDirectory() as compiled_dir:
        with patch.dict(sys.modules, _mock_pysmi_modules(mock_codegen, mock_compiler)):
            with pytest.raises(MibCompileFailure, match="boom"):
                compile_mibs(["TEST-MIB"], compiled_dir, source_dirs=["/upload"])


def test_supported_mib_source_filename_contract_matches_expected_variants():
    assert is_supported_mib_source_filename("IF-MIB")
    assert is_supported_mib_source_filename("IF-MIB.mib")
    assert is_supported_mib_source_filename("IF-MIB.my")
    assert is_supported_mib_source_filename("IF-MIB.txt")
    assert is_supported_mib_source_filename("IF-MIB.MY")
    assert not is_supported_mib_source_filename("IF-MIB.yaml")
    assert not is_supported_mib_source_filename(".hidden.mib")

    assert mib_source_module_name("IF-MIB") == "IF-MIB"
    assert mib_source_module_name("IF-MIB.MIB") == "IF-MIB"
    assert mib_source_module_name("IF-MIB.my") == "IF-MIB"
    assert mib_source_module_name("IF-MIB.txt") == "IF-MIB"


def test_available_source_mibs_only_counts_supported_source_files():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "CISCO-SMI.mib").write_text("x")
        Path(d, "IF-MIB").write_text("x")
        Path(d, "ENTITY-MIB.MY").write_text("x")
        Path(d, "HOST-RESOURCES-MIB.txt").write_text("x")
        Path(d, "IGNORED.py").write_text("x")
        Path(d, "IGNORED.yaml").write_text("x")
        Path(d, ".hidden").mkdir()
        Path(d, ".hidden", "SECRET-MIB.my").write_text("x")

        available = available_source_mibs(d)

    assert "CISCO-SMI" in available
    assert "IF-MIB" in available
    assert "ENTITY-MIB" in available
    assert "HOST-RESOURCES-MIB" in available
    assert "IGNORED" not in available
    assert "SECRET-MIB" not in available


def test_list_mib_source_files_accepts_supported_extensions_and_extensionless():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "A-MIB").write_text("x")
        Path(d, "B-MIB.mib").write_text("x")
        Path(d, "C-MIB.MY").write_text("x")
        Path(d, "D-MIB.txt").write_text("x")
        Path(d, "skip.py").write_text("x")
        Path(d, "skip.yaml").write_text("x")

        listed = [path.name for path in list_mib_source_files(d)]

    assert listed == ["A-MIB", "B-MIB.mib", "C-MIB.MY", "D-MIB.txt"]


def test_delete_mib_artifacts_removes_compiled_and_raw_files():
    with tempfile.TemporaryDirectory() as compiled_dir, tempfile.TemporaryDirectory() as source_dir:
        compiled = Path(compiled_dir, "IF_MIB.py")
        compiled.write_text("# compiled")
        raw = Path(source_dir, "IF-MIB.MY")
        raw.write_text("IF-MIB DEFINITIONS ::= BEGIN END")
        ignored = Path(source_dir, "IF-MIB.yaml")
        ignored.write_text("not a mib source")

        deleted = delete_mib_artifacts("IF-MIB", compiled_dir, source_dir)

        assert deleted.compiled_files == ["IF_MIB.py"]
        assert deleted.source_files == ["IF-MIB.MY"]
        assert ignored.exists()

def test_bundled_mib_source_dirs_filters_missing_and_duplicates():
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        env_value = os.pathsep.join([one, two, one, "/does/not/exist"])
        with patch.dict("os.environ", {"TRAM_MIB_BUNDLED_SOURCE_DIR": env_value}):
            assert bundled_mib_source_dirs() == [one, two]
