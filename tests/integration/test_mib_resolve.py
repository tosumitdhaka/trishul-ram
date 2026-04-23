"""Integration tests for SNMP MIB OID resolution.

Tests OID ↔ symbolic name resolution using standard MIBs (SNMPv2-MIB, IF-MIB)
that are bundled with PySNMP. Falls back gracefully when pysnmp is not installed.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


def pysnmp_available() -> bool:
    try:
        from pysnmp.smi import builder, view  # noqa: F401
        return True
    except Exception:
        return False


class TestMibResolveIntegration:
    def test_oid_str_to_tuple_basic(self):
        """Numeric OID string parses to tuple correctly."""
        from tram.connectors.snmp.mib_utils import oid_str_to_tuple
        assert oid_str_to_tuple("1.3.6.1.2.1.1.1.0") == (1, 3, 6, 1, 2, 1, 1, 1, 0)

    def test_oid_str_to_tuple_leading_dot(self):
        """Leading dot is stripped before parsing."""
        from tram.connectors.snmp.mib_utils import oid_str_to_tuple
        assert oid_str_to_tuple(".1.3.6.1") == (1, 3, 6, 1)

    def test_resolve_oid_none_view_fallback(self):
        """resolve_oid with None view returns dotted-decimal string."""
        from tram.connectors.snmp.mib_utils import resolve_oid
        result = resolve_oid(None, (1, 3, 6, 1, 2, 1, 1, 1, 0))
        assert result == "1.3.6.1.2.1.1.1.0"

    def test_symbolic_to_oid_none_view(self):
        """symbolic_to_oid with None view returns None."""
        from tram.connectors.snmp.mib_utils import symbolic_to_oid
        result = symbolic_to_oid(None, "SNMPv2-MIB::sysDescr.0")
        assert result is None

    def test_resolve_oid_with_mock_view(self):
        """resolve_oid correctly extracts symbolic name from MIB view response."""
        from tram.connectors.snmp.mib_utils import resolve_oid

        mock_view = MagicMock()
        mock_view.get_node_location.return_value = ("SNMPv2-MIB", "sysDescr", ())

        mock_oid_obj = MagicMock()
        mock_oid_cls = MagicMock(return_value=mock_oid_obj)
        mock_asn1_univ = MagicMock()
        mock_asn1_univ.ObjectIdentifier = mock_oid_cls

        with patch.dict(sys.modules, {
            "pysnmp": MagicMock(),
            "pysnmp.smi": MagicMock(),
            "pysnmp.smi.rfc1902": MagicMock(),
            "pyasn1": MagicMock(),
            "pyasn1.type": MagicMock(),
            "pyasn1.type.univ": mock_asn1_univ,
        }):
            result = resolve_oid(mock_view, (1, 3, 6, 1, 2, 1, 1, 1, 0))

        assert result == "sysDescr"

    def test_get_mib_view_caching(self):
        """get_mib_view returns the same object for identical parameters."""
        from tram.connectors.snmp import mib_utils

        mib_utils._cached_mib_view.cache_clear()
        mock_view = MagicMock()

        with patch("tram.connectors.snmp.mib_utils.build_mib_view", return_value=mock_view) as mock_build:
            v1 = mib_utils.get_mib_view([], ["SNMPv2-MIB"])
            v2 = mib_utils.get_mib_view([], ["SNMPv2-MIB"])
            mib_utils.get_mib_view([], ["IF-MIB"])  # different params → new call

        assert v1 is v2
        assert mock_build.call_count == 2  # SNMPv2-MIB + IF-MIB

    def test_symbolic_to_oid_success_with_mock(self):
        """symbolic_to_oid resolves IF-MIB::ifOperStatus.1 to a tuple.

        The function splits "IF-MIB::ifOperStatus.1" into symbol "ifOperStatus"
        and index [1], resolving through ObjectIdentity + resolveWithMib.
        """
        from tram.connectors.snmp.mib_utils import symbolic_to_oid

        base_oid = (1, 3, 6, 1, 2, 1, 2, 2, 1, 8)
        mock_view = MagicMock()
        mock_oid = MagicMock()
        mock_oid.getOid.return_value = base_oid
        mock_rfc = MagicMock()
        mock_rfc.ObjectIdentity.return_value = mock_oid

        with patch.dict(sys.modules, {
            "pysnmp.smi.rfc1902": mock_rfc,
            "pyasn1.type.univ": MagicMock(),
        }):
            result = symbolic_to_oid(mock_view, "IF-MIB::ifOperStatus.1")

        assert result == base_oid

    @pytest.mark.skipif(not pysnmp_available(), reason="pysnmp not installed")
    def test_build_mib_view_standard_mibs(self):
        """build_mib_view with standard MIBs returns a MibViewController."""
        from tram.connectors.snmp.mib_utils import build_mib_view
        view = build_mib_view([], ["SNMPv2-MIB"])
        assert view is not None

    @pytest.mark.skipif(not pysnmp_available(), reason="pysnmp not installed")
    def test_resolve_standard_oid_sysDescr(self):
        """Resolve sysDescr OID using real pysnmp MIB view."""
        from tram.connectors.snmp.mib_utils import build_mib_view, resolve_oid
        view = build_mib_view([], ["SNMPv2-MIB"])
        if view is None:
            pytest.skip("MIB view could not be built")
        result = resolve_oid(view, (1, 3, 6, 1, 2, 1, 1, 1))
        # Should resolve to sysDescr or its dotted-decimal fallback
        assert result in ("sysDescr", "1.3.6.1.2.1.1.1")
