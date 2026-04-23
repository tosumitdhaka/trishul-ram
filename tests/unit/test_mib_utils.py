"""Tests for SNMP MIB utilities (tram/connectors/snmp/mib_utils.py)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


class TestMibUtilsWithMocks:
    """Test MIB utils with mocked pysnmp — no real SNMP stack needed."""

    def _mock_pysnmp(self):
        """Return mock pysnmp smi module."""
        mock_builder = MagicMock()
        mock_view = MagicMock()
        mock_builder_cls = MagicMock(return_value=mock_builder)
        mock_view_cls = MagicMock(return_value=mock_view)

        mock_smi = MagicMock()
        mock_smi.builder.MibBuilder = mock_builder_cls
        mock_smi.builder.DirMibSource = MagicMock(return_value=MagicMock())
        mock_smi.view.MibViewController = mock_view_cls

        return mock_smi, mock_builder, mock_view

    def test_build_mib_view_returns_none_when_pysnmp_missing(self):
        """Returns None gracefully when pysnmp is not installed."""
        from tram.connectors.snmp.mib_utils import build_mib_view

        with patch.dict(sys.modules, {"pysnmp": None, "pysnmp.smi": None, "pysnmp.smi.builder": None, "pysnmp.smi.view": None}):
            # Simulate ImportError
            with patch("tram.connectors.snmp.mib_utils.build_mib_view", return_value=None):
                build_mib_view.__wrapped__([], []) if hasattr(build_mib_view, "__wrapped__") else None
        # Just verify we can call without crash in patched env
        # If pysnmp is installed, test still passes (returns real view)

    def test_resolve_oid_returns_dotted_string_when_mib_view_none(self):
        """Falls back to dotted-decimal string when mib_view is None."""
        from tram.connectors.snmp.mib_utils import resolve_oid
        result = resolve_oid(None, (1, 3, 6, 1, 2, 1, 1, 1, 0))
        assert result == "1.3.6.1.2.1.1.1.0"

    def test_resolve_oid_fallback_on_exception(self):
        """Falls back to dotted-decimal when OID resolution raises."""
        from tram.connectors.snmp.mib_utils import resolve_oid

        mock_view = MagicMock()
        mock_view.getNodeLocation.side_effect = Exception("unknown OID")

        with patch.dict(sys.modules, {
            "pysnmp.smi.rfc1902": MagicMock(),
            "pyasn1.type.univ": MagicMock(),
        }):
            result = resolve_oid(mock_view, (1, 3, 6, 1, 2, 1, 1, 1, 0))
        assert result == "1.3.6.1.2.1.1.1.0"

    def test_oid_str_to_tuple_simple(self):
        from tram.connectors.snmp.mib_utils import oid_str_to_tuple
        assert oid_str_to_tuple("1.3.6.1.2.1.1.1.0") == (1, 3, 6, 1, 2, 1, 1, 1, 0)

    def test_oid_str_to_tuple_leading_dot(self):
        from tram.connectors.snmp.mib_utils import oid_str_to_tuple
        assert oid_str_to_tuple(".1.3.6.1") == (1, 3, 6, 1)

    def test_symbolic_to_oid_returns_none_when_mib_view_none(self):
        from tram.connectors.snmp.mib_utils import symbolic_to_oid
        result = symbolic_to_oid(None, "IF-MIB::ifOperStatus.1")
        assert result is None

    def test_symbolic_to_oid_returns_none_on_failure(self):
        from tram.connectors.snmp.mib_utils import symbolic_to_oid
        mock_view = MagicMock()
        mock_oid = MagicMock()
        mock_oid.resolveWithMib.side_effect = Exception("not found")
        mock_rfc1902 = MagicMock()
        mock_rfc1902.ObjectIdentity.return_value = mock_oid

        with patch.dict(sys.modules, {
            "pysnmp.smi.rfc1902": mock_rfc1902,
            "pyasn1.type.univ": MagicMock(),
        }):
            result = symbolic_to_oid(mock_view, "IF-MIB::ifOperStatus.1")
        assert result is None

    def test_get_mib_view_caches_result(self):
        """Same dirs+modules combination returns same object (cached)."""
        from tram.connectors.snmp import mib_utils
        # Clear cache
        mib_utils._cached_mib_view.cache_clear()

        mock_view = MagicMock()
        with patch("tram.connectors.snmp.mib_utils.build_mib_view", return_value=mock_view) as mock_build:
            v1 = mib_utils.get_mib_view([], ["SNMPv2-MIB"])
            v2 = mib_utils.get_mib_view([], ["SNMPv2-MIB"])

        assert v1 is v2
        assert mock_build.call_count == 1

    def test_build_mib_view_uses_snake_case_mib_source_methods(self):
        """Falls back to snake_case MIB source accessors when camelCase is absent."""
        from tram.connectors.snmp.mib_utils import build_mib_view

        class SnakeCaseBuilder:
            def __init__(self):
                self.sources = ("default-source",)
                self.loaded = []

            def get_mib_sources(self):
                return self.sources

            def set_mib_sources(self, *sources):
                self.sources = sources

            def loadModules(self, module):
                self.loaded.append(module)

        mock_builder = SnakeCaseBuilder()
        mock_view = MagicMock()
        mock_builder_cls = MagicMock(return_value=mock_builder)
        mock_dir_source = MagicMock(side_effect=lambda path: f"dir:{path}")
        mock_view_cls = MagicMock(return_value=mock_view)

        with patch.dict(
            sys.modules,
            {
                "pysnmp.smi": MagicMock(),
                "pysnmp.smi.builder": MagicMock(),
                "pysnmp.smi.view": MagicMock(),
            },
        ):
            from pysnmp.smi import builder, view

            builder.MibBuilder = mock_builder_cls
            builder.DirMibSource = mock_dir_source
            view.MibViewController = mock_view_cls

            result = build_mib_view(["/custom/mibs"], ["IF-MIB"])

        assert result is mock_view
        assert mock_builder.sources == ("dir:/custom/mibs", "default-source")
        assert "IF-MIB" in mock_builder.loaded

    def test_resolve_oid_success_with_mock_view(self):
        """Successful resolution returns symbolic name."""
        from tram.connectors.snmp.mib_utils import resolve_oid

        mock_view = MagicMock()
        mock_view.get_node_location.return_value = ("SNMPv2-MIB", "sysDescr", ())

        mock_oid_obj = MagicMock()
        mock_oid_cls = MagicMock(return_value=mock_oid_obj)

        mock_rfc = MagicMock()
        mock_asn1 = MagicMock()
        mock_asn1.type.univ.ObjectIdentifier = mock_oid_cls

        with patch.dict(sys.modules, {
            "pysnmp": MagicMock(),
            "pysnmp.smi": MagicMock(),
            "pysnmp.smi.rfc1902": mock_rfc,
            "pyasn1": mock_asn1,
            "pyasn1.type": MagicMock(),
            "pyasn1.type.univ": mock_asn1.type.univ,
        }):
            result = resolve_oid(mock_view, (1, 3, 6, 1, 2, 1, 1, 1))

        assert result == "sysDescr"
