# SNMP MIB module (TCP-MIB) expressed in pysnmp data model.
#
# This Python module is designed to be imported and executed by the
# pysnmp library.
#
# See https://www.pysnmp.com/pysnmp for further information.
#
# Notes
# -----
# ASN.1 source file://files/mibs/TCP-MIB
# Produced by pysmi-1.4.3 at Fri May  1 19:17:40 2026
# On host IN-PF62F5JS platform Linux version 6.6.87.2-microsoft-standard-WSL2 by user dhaka
# Using Python version 3.12.3 (main, Mar 23 2026, 19:04:32) [GCC 13.3.0]

if 'mibBuilder' not in globals():
    import sys

    sys.stderr.write(__doc__)
    sys.exit(1)

# Import base ASN.1 objects even if this MIB does not use it

(Integer,
 OctetString,
 ObjectIdentifier) = mibBuilder.importSymbols(
    "ASN1",
    "Integer",
    "OctetString",
    "ObjectIdentifier")

(NamedValues,) = mibBuilder.importSymbols(
    "ASN1-ENUMERATION",
    "NamedValues")
(ConstraintsIntersection,
 SingleValueConstraint,
 ValueRangeConstraint,
 ValueSizeConstraint,
 ConstraintsUnion) = mibBuilder.importSymbols(
    "ASN1-REFINEMENT",
    "ConstraintsIntersection",
    "SingleValueConstraint",
    "ValueRangeConstraint",
    "ValueSizeConstraint",
    "ConstraintsUnion")

# Import SMI symbols from the MIBs this MIB depends on

(InetAddressType,
 InetPortNumber,
 InetAddress) = mibBuilder.importSymbols(
    "INET-ADDRESS-MIB",
    "InetAddressType",
    "InetPortNumber",
    "InetAddress")

(ModuleCompliance,
 ObjectGroup,
 NotificationGroup) = mibBuilder.importSymbols(
    "SNMPv2-CONF",
    "ModuleCompliance",
    "ObjectGroup",
    "NotificationGroup")

(Gauge32,
 mib_2,
 MibIdentifier,
 IpAddress,
 Counter64,
 Integer32,
 ObjectIdentity,
 Counter32,
 MibScalar,
 MibTable,
 MibTableRow,
 MibTableColumn,
 iso,
 NotificationType,
 Unsigned32,
 TimeTicks,
 ModuleIdentity,
 Bits) = mibBuilder.importSymbols(
    "SNMPv2-SMI",
    "Gauge32",
    "mib-2",
    "MibIdentifier",
    "IpAddress",
    "Counter64",
    "Integer32",
    "ObjectIdentity",
    "Counter32",
    "MibScalar",
    "MibTable",
    "MibTableRow",
    "MibTableColumn",
    "iso",
    "NotificationType",
    "Unsigned32",
    "TimeTicks",
    "ModuleIdentity",
    "Bits")

(DisplayString,
 TextualConvention) = mibBuilder.importSymbols(
    "SNMPv2-TC",
    "DisplayString",
    "TextualConvention")


# MODULE-IDENTITY

tcpMIB = ModuleIdentity(
    (1, 3, 6, 1, 2, 1, 49)
)
tcpMIB.setRevisions(
        ("2005-02-18 00:00",
         "1994-11-01 00:00",
         "1991-03-31 00:00")
)


# Types definitions


# TEXTUAL-CONVENTIONS



# MIB Managed Objects in the order of their OIDs

_Tcp_ObjectIdentity = ObjectIdentity
tcp = _Tcp_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 6)
)


class _TcpRtoAlgorithm_Type(Integer32):
    """Custom type tcpRtoAlgorithm based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2,
              3,
              4,
              5)
        )
    )
    namedValues = NamedValues(
        *(("constant", 2),
          ("other", 1),
          ("rfc2988", 5),
          ("rsre", 3),
          ("vanj", 4))
    )


_TcpRtoAlgorithm_Type.__name__ = "Integer32"
_TcpRtoAlgorithm_Object = MibScalar
tcpRtoAlgorithm = _TcpRtoAlgorithm_Object(
    (1, 3, 6, 1, 2, 1, 6, 1),
    _TcpRtoAlgorithm_Type()
)
tcpRtoAlgorithm.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpRtoAlgorithm.setStatus("current")


class _TcpRtoMin_Type(Integer32):
    """Custom type tcpRtoMin based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 2147483647),
    )


_TcpRtoMin_Type.__name__ = "Integer32"
_TcpRtoMin_Object = MibScalar
tcpRtoMin = _TcpRtoMin_Object(
    (1, 3, 6, 1, 2, 1, 6, 2),
    _TcpRtoMin_Type()
)
tcpRtoMin.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpRtoMin.setStatus("current")
if mibBuilder.loadTexts:
    tcpRtoMin.setUnits("milliseconds")


class _TcpRtoMax_Type(Integer32):
    """Custom type tcpRtoMax based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 2147483647),
    )


_TcpRtoMax_Type.__name__ = "Integer32"
_TcpRtoMax_Object = MibScalar
tcpRtoMax = _TcpRtoMax_Object(
    (1, 3, 6, 1, 2, 1, 6, 3),
    _TcpRtoMax_Type()
)
tcpRtoMax.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpRtoMax.setStatus("current")
if mibBuilder.loadTexts:
    tcpRtoMax.setUnits("milliseconds")


class _TcpMaxConn_Type(Integer32):
    """Custom type tcpMaxConn based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(-1, -1),
        ValueRangeConstraint(0, 2147483647),
    )


_TcpMaxConn_Type.__name__ = "Integer32"
_TcpMaxConn_Object = MibScalar
tcpMaxConn = _TcpMaxConn_Object(
    (1, 3, 6, 1, 2, 1, 6, 4),
    _TcpMaxConn_Type()
)
tcpMaxConn.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpMaxConn.setStatus("current")
_TcpActiveOpens_Type = Counter32
_TcpActiveOpens_Object = MibScalar
tcpActiveOpens = _TcpActiveOpens_Object(
    (1, 3, 6, 1, 2, 1, 6, 5),
    _TcpActiveOpens_Type()
)
tcpActiveOpens.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpActiveOpens.setStatus("current")
_TcpPassiveOpens_Type = Counter32
_TcpPassiveOpens_Object = MibScalar
tcpPassiveOpens = _TcpPassiveOpens_Object(
    (1, 3, 6, 1, 2, 1, 6, 6),
    _TcpPassiveOpens_Type()
)
tcpPassiveOpens.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpPassiveOpens.setStatus("current")
_TcpAttemptFails_Type = Counter32
_TcpAttemptFails_Object = MibScalar
tcpAttemptFails = _TcpAttemptFails_Object(
    (1, 3, 6, 1, 2, 1, 6, 7),
    _TcpAttemptFails_Type()
)
tcpAttemptFails.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpAttemptFails.setStatus("current")
_TcpEstabResets_Type = Counter32
_TcpEstabResets_Object = MibScalar
tcpEstabResets = _TcpEstabResets_Object(
    (1, 3, 6, 1, 2, 1, 6, 8),
    _TcpEstabResets_Type()
)
tcpEstabResets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpEstabResets.setStatus("current")
_TcpCurrEstab_Type = Gauge32
_TcpCurrEstab_Object = MibScalar
tcpCurrEstab = _TcpCurrEstab_Object(
    (1, 3, 6, 1, 2, 1, 6, 9),
    _TcpCurrEstab_Type()
)
tcpCurrEstab.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpCurrEstab.setStatus("current")
_TcpInSegs_Type = Counter32
_TcpInSegs_Object = MibScalar
tcpInSegs = _TcpInSegs_Object(
    (1, 3, 6, 1, 2, 1, 6, 10),
    _TcpInSegs_Type()
)
tcpInSegs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpInSegs.setStatus("current")
_TcpOutSegs_Type = Counter32
_TcpOutSegs_Object = MibScalar
tcpOutSegs = _TcpOutSegs_Object(
    (1, 3, 6, 1, 2, 1, 6, 11),
    _TcpOutSegs_Type()
)
tcpOutSegs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpOutSegs.setStatus("current")
_TcpRetransSegs_Type = Counter32
_TcpRetransSegs_Object = MibScalar
tcpRetransSegs = _TcpRetransSegs_Object(
    (1, 3, 6, 1, 2, 1, 6, 12),
    _TcpRetransSegs_Type()
)
tcpRetransSegs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpRetransSegs.setStatus("current")
_TcpConnTable_Object = MibTable
tcpConnTable = _TcpConnTable_Object(
    (1, 3, 6, 1, 2, 1, 6, 13)
)
if mibBuilder.loadTexts:
    tcpConnTable.setStatus("deprecated")
_TcpConnEntry_Object = MibTableRow
tcpConnEntry = _TcpConnEntry_Object(
    (1, 3, 6, 1, 2, 1, 6, 13, 1)
)
tcpConnEntry.setIndexNames(
    (0, "TCP-MIB", "tcpConnLocalAddress"),
    (0, "TCP-MIB", "tcpConnLocalPort"),
    (0, "TCP-MIB", "tcpConnRemAddress"),
    (0, "TCP-MIB", "tcpConnRemPort"),
)
if mibBuilder.loadTexts:
    tcpConnEntry.setStatus("deprecated")


class _TcpConnState_Type(Integer32):
    """Custom type tcpConnState based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2,
              3,
              4,
              5,
              6,
              7,
              8,
              9,
              10,
              11,
              12)
        )
    )
    namedValues = NamedValues(
        *(("closeWait", 8),
          ("closed", 1),
          ("closing", 10),
          ("deleteTCB", 12),
          ("established", 5),
          ("finWait1", 6),
          ("finWait2", 7),
          ("lastAck", 9),
          ("listen", 2),
          ("synReceived", 4),
          ("synSent", 3),
          ("timeWait", 11))
    )


_TcpConnState_Type.__name__ = "Integer32"
_TcpConnState_Object = MibTableColumn
tcpConnState = _TcpConnState_Object(
    (1, 3, 6, 1, 2, 1, 6, 13, 1, 1),
    _TcpConnState_Type()
)
tcpConnState.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    tcpConnState.setStatus("deprecated")
_TcpConnLocalAddress_Type = IpAddress
_TcpConnLocalAddress_Object = MibTableColumn
tcpConnLocalAddress = _TcpConnLocalAddress_Object(
    (1, 3, 6, 1, 2, 1, 6, 13, 1, 2),
    _TcpConnLocalAddress_Type()
)
tcpConnLocalAddress.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpConnLocalAddress.setStatus("deprecated")


class _TcpConnLocalPort_Type(Integer32):
    """Custom type tcpConnLocalPort based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 65535),
    )


_TcpConnLocalPort_Type.__name__ = "Integer32"
_TcpConnLocalPort_Object = MibTableColumn
tcpConnLocalPort = _TcpConnLocalPort_Object(
    (1, 3, 6, 1, 2, 1, 6, 13, 1, 3),
    _TcpConnLocalPort_Type()
)
tcpConnLocalPort.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpConnLocalPort.setStatus("deprecated")
_TcpConnRemAddress_Type = IpAddress
_TcpConnRemAddress_Object = MibTableColumn
tcpConnRemAddress = _TcpConnRemAddress_Object(
    (1, 3, 6, 1, 2, 1, 6, 13, 1, 4),
    _TcpConnRemAddress_Type()
)
tcpConnRemAddress.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpConnRemAddress.setStatus("deprecated")


class _TcpConnRemPort_Type(Integer32):
    """Custom type tcpConnRemPort based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 65535),
    )


_TcpConnRemPort_Type.__name__ = "Integer32"
_TcpConnRemPort_Object = MibTableColumn
tcpConnRemPort = _TcpConnRemPort_Object(
    (1, 3, 6, 1, 2, 1, 6, 13, 1, 5),
    _TcpConnRemPort_Type()
)
tcpConnRemPort.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpConnRemPort.setStatus("deprecated")
_TcpInErrs_Type = Counter32
_TcpInErrs_Object = MibScalar
tcpInErrs = _TcpInErrs_Object(
    (1, 3, 6, 1, 2, 1, 6, 14),
    _TcpInErrs_Type()
)
tcpInErrs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpInErrs.setStatus("current")
_TcpOutRsts_Type = Counter32
_TcpOutRsts_Object = MibScalar
tcpOutRsts = _TcpOutRsts_Object(
    (1, 3, 6, 1, 2, 1, 6, 15),
    _TcpOutRsts_Type()
)
tcpOutRsts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpOutRsts.setStatus("current")
_TcpHCInSegs_Type = Counter64
_TcpHCInSegs_Object = MibScalar
tcpHCInSegs = _TcpHCInSegs_Object(
    (1, 3, 6, 1, 2, 1, 6, 17),
    _TcpHCInSegs_Type()
)
tcpHCInSegs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpHCInSegs.setStatus("current")
_TcpHCOutSegs_Type = Counter64
_TcpHCOutSegs_Object = MibScalar
tcpHCOutSegs = _TcpHCOutSegs_Object(
    (1, 3, 6, 1, 2, 1, 6, 18),
    _TcpHCOutSegs_Type()
)
tcpHCOutSegs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpHCOutSegs.setStatus("current")
_TcpConnectionTable_Object = MibTable
tcpConnectionTable = _TcpConnectionTable_Object(
    (1, 3, 6, 1, 2, 1, 6, 19)
)
if mibBuilder.loadTexts:
    tcpConnectionTable.setStatus("current")
_TcpConnectionEntry_Object = MibTableRow
tcpConnectionEntry = _TcpConnectionEntry_Object(
    (1, 3, 6, 1, 2, 1, 6, 19, 1)
)
tcpConnectionEntry.setIndexNames(
    (0, "TCP-MIB", "tcpConnectionLocalAddressType"),
    (0, "TCP-MIB", "tcpConnectionLocalAddress"),
    (0, "TCP-MIB", "tcpConnectionLocalPort"),
    (0, "TCP-MIB", "tcpConnectionRemAddressType"),
    (0, "TCP-MIB", "tcpConnectionRemAddress"),
    (0, "TCP-MIB", "tcpConnectionRemPort"),
)
if mibBuilder.loadTexts:
    tcpConnectionEntry.setStatus("current")
_TcpConnectionLocalAddressType_Type = InetAddressType
_TcpConnectionLocalAddressType_Object = MibTableColumn
tcpConnectionLocalAddressType = _TcpConnectionLocalAddressType_Object(
    (1, 3, 6, 1, 2, 1, 6, 19, 1, 1),
    _TcpConnectionLocalAddressType_Type()
)
tcpConnectionLocalAddressType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    tcpConnectionLocalAddressType.setStatus("current")
_TcpConnectionLocalAddress_Type = InetAddress
_TcpConnectionLocalAddress_Object = MibTableColumn
tcpConnectionLocalAddress = _TcpConnectionLocalAddress_Object(
    (1, 3, 6, 1, 2, 1, 6, 19, 1, 2),
    _TcpConnectionLocalAddress_Type()
)
tcpConnectionLocalAddress.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    tcpConnectionLocalAddress.setStatus("current")
_TcpConnectionLocalPort_Type = InetPortNumber
_TcpConnectionLocalPort_Object = MibTableColumn
tcpConnectionLocalPort = _TcpConnectionLocalPort_Object(
    (1, 3, 6, 1, 2, 1, 6, 19, 1, 3),
    _TcpConnectionLocalPort_Type()
)
tcpConnectionLocalPort.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    tcpConnectionLocalPort.setStatus("current")
_TcpConnectionRemAddressType_Type = InetAddressType
_TcpConnectionRemAddressType_Object = MibTableColumn
tcpConnectionRemAddressType = _TcpConnectionRemAddressType_Object(
    (1, 3, 6, 1, 2, 1, 6, 19, 1, 4),
    _TcpConnectionRemAddressType_Type()
)
tcpConnectionRemAddressType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    tcpConnectionRemAddressType.setStatus("current")
_TcpConnectionRemAddress_Type = InetAddress
_TcpConnectionRemAddress_Object = MibTableColumn
tcpConnectionRemAddress = _TcpConnectionRemAddress_Object(
    (1, 3, 6, 1, 2, 1, 6, 19, 1, 5),
    _TcpConnectionRemAddress_Type()
)
tcpConnectionRemAddress.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    tcpConnectionRemAddress.setStatus("current")
_TcpConnectionRemPort_Type = InetPortNumber
_TcpConnectionRemPort_Object = MibTableColumn
tcpConnectionRemPort = _TcpConnectionRemPort_Object(
    (1, 3, 6, 1, 2, 1, 6, 19, 1, 6),
    _TcpConnectionRemPort_Type()
)
tcpConnectionRemPort.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    tcpConnectionRemPort.setStatus("current")


class _TcpConnectionState_Type(Integer32):
    """Custom type tcpConnectionState based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2,
              3,
              4,
              5,
              6,
              7,
              8,
              9,
              10,
              11,
              12)
        )
    )
    namedValues = NamedValues(
        *(("closeWait", 8),
          ("closed", 1),
          ("closing", 10),
          ("deleteTCB", 12),
          ("established", 5),
          ("finWait1", 6),
          ("finWait2", 7),
          ("lastAck", 9),
          ("listen", 2),
          ("synReceived", 4),
          ("synSent", 3),
          ("timeWait", 11))
    )


_TcpConnectionState_Type.__name__ = "Integer32"
_TcpConnectionState_Object = MibTableColumn
tcpConnectionState = _TcpConnectionState_Object(
    (1, 3, 6, 1, 2, 1, 6, 19, 1, 7),
    _TcpConnectionState_Type()
)
tcpConnectionState.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    tcpConnectionState.setStatus("current")
_TcpConnectionProcess_Type = Unsigned32
_TcpConnectionProcess_Object = MibTableColumn
tcpConnectionProcess = _TcpConnectionProcess_Object(
    (1, 3, 6, 1, 2, 1, 6, 19, 1, 8),
    _TcpConnectionProcess_Type()
)
tcpConnectionProcess.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpConnectionProcess.setStatus("current")
_TcpListenerTable_Object = MibTable
tcpListenerTable = _TcpListenerTable_Object(
    (1, 3, 6, 1, 2, 1, 6, 20)
)
if mibBuilder.loadTexts:
    tcpListenerTable.setStatus("current")
_TcpListenerEntry_Object = MibTableRow
tcpListenerEntry = _TcpListenerEntry_Object(
    (1, 3, 6, 1, 2, 1, 6, 20, 1)
)
tcpListenerEntry.setIndexNames(
    (0, "TCP-MIB", "tcpListenerLocalAddressType"),
    (0, "TCP-MIB", "tcpListenerLocalAddress"),
    (0, "TCP-MIB", "tcpListenerLocalPort"),
)
if mibBuilder.loadTexts:
    tcpListenerEntry.setStatus("current")
_TcpListenerLocalAddressType_Type = InetAddressType
_TcpListenerLocalAddressType_Object = MibTableColumn
tcpListenerLocalAddressType = _TcpListenerLocalAddressType_Object(
    (1, 3, 6, 1, 2, 1, 6, 20, 1, 1),
    _TcpListenerLocalAddressType_Type()
)
tcpListenerLocalAddressType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    tcpListenerLocalAddressType.setStatus("current")
_TcpListenerLocalAddress_Type = InetAddress
_TcpListenerLocalAddress_Object = MibTableColumn
tcpListenerLocalAddress = _TcpListenerLocalAddress_Object(
    (1, 3, 6, 1, 2, 1, 6, 20, 1, 2),
    _TcpListenerLocalAddress_Type()
)
tcpListenerLocalAddress.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    tcpListenerLocalAddress.setStatus("current")
_TcpListenerLocalPort_Type = InetPortNumber
_TcpListenerLocalPort_Object = MibTableColumn
tcpListenerLocalPort = _TcpListenerLocalPort_Object(
    (1, 3, 6, 1, 2, 1, 6, 20, 1, 3),
    _TcpListenerLocalPort_Type()
)
tcpListenerLocalPort.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    tcpListenerLocalPort.setStatus("current")
_TcpListenerProcess_Type = Unsigned32
_TcpListenerProcess_Object = MibTableColumn
tcpListenerProcess = _TcpListenerProcess_Object(
    (1, 3, 6, 1, 2, 1, 6, 20, 1, 4),
    _TcpListenerProcess_Type()
)
tcpListenerProcess.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    tcpListenerProcess.setStatus("current")
_TcpMIBConformance_ObjectIdentity = ObjectIdentity
tcpMIBConformance = _TcpMIBConformance_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 49, 2)
)
_TcpMIBCompliances_ObjectIdentity = ObjectIdentity
tcpMIBCompliances = _TcpMIBCompliances_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 49, 2, 1)
)
_TcpMIBGroups_ObjectIdentity = ObjectIdentity
tcpMIBGroups = _TcpMIBGroups_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 49, 2, 2)
)

# Managed Objects groups

tcpGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 49, 2, 2, 1)
)
tcpGroup.setObjects(
      *(("TCP-MIB", "tcpRtoAlgorithm"),
        ("TCP-MIB", "tcpRtoMin"),
        ("TCP-MIB", "tcpRtoMax"),
        ("TCP-MIB", "tcpMaxConn"),
        ("TCP-MIB", "tcpActiveOpens"),
        ("TCP-MIB", "tcpPassiveOpens"),
        ("TCP-MIB", "tcpAttemptFails"),
        ("TCP-MIB", "tcpEstabResets"),
        ("TCP-MIB", "tcpCurrEstab"),
        ("TCP-MIB", "tcpInSegs"),
        ("TCP-MIB", "tcpOutSegs"),
        ("TCP-MIB", "tcpRetransSegs"),
        ("TCP-MIB", "tcpConnState"),
        ("TCP-MIB", "tcpConnLocalAddress"),
        ("TCP-MIB", "tcpConnLocalPort"),
        ("TCP-MIB", "tcpConnRemAddress"),
        ("TCP-MIB", "tcpConnRemPort"),
        ("TCP-MIB", "tcpInErrs"),
        ("TCP-MIB", "tcpOutRsts"))
)
if mibBuilder.loadTexts:
    tcpGroup.setStatus("deprecated")

tcpBaseGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 49, 2, 2, 2)
)
tcpBaseGroup.setObjects(
      *(("TCP-MIB", "tcpRtoAlgorithm"),
        ("TCP-MIB", "tcpRtoMin"),
        ("TCP-MIB", "tcpRtoMax"),
        ("TCP-MIB", "tcpMaxConn"),
        ("TCP-MIB", "tcpActiveOpens"),
        ("TCP-MIB", "tcpPassiveOpens"),
        ("TCP-MIB", "tcpAttemptFails"),
        ("TCP-MIB", "tcpEstabResets"),
        ("TCP-MIB", "tcpCurrEstab"),
        ("TCP-MIB", "tcpInSegs"),
        ("TCP-MIB", "tcpOutSegs"),
        ("TCP-MIB", "tcpRetransSegs"),
        ("TCP-MIB", "tcpInErrs"),
        ("TCP-MIB", "tcpOutRsts"))
)
if mibBuilder.loadTexts:
    tcpBaseGroup.setStatus("current")

tcpConnectionGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 49, 2, 2, 3)
)
tcpConnectionGroup.setObjects(
      *(("TCP-MIB", "tcpConnectionState"),
        ("TCP-MIB", "tcpConnectionProcess"))
)
if mibBuilder.loadTexts:
    tcpConnectionGroup.setStatus("current")

tcpListenerGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 49, 2, 2, 4)
)
tcpListenerGroup.setObjects(
    ("TCP-MIB", "tcpListenerProcess")
)
if mibBuilder.loadTexts:
    tcpListenerGroup.setStatus("current")

tcpHCGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 49, 2, 2, 5)
)
tcpHCGroup.setObjects(
      *(("TCP-MIB", "tcpHCInSegs"),
        ("TCP-MIB", "tcpHCOutSegs"))
)
if mibBuilder.loadTexts:
    tcpHCGroup.setStatus("current")


# Notification objects


# Notifications groups


# Agent capabilities


# Module compliance

tcpMIBCompliance = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 49, 2, 1, 1)
)
if mibBuilder.loadTexts:
    tcpMIBCompliance.setStatus(
        "deprecated"
    )

tcpMIBCompliance2 = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 49, 2, 1, 2)
)
if mibBuilder.loadTexts:
    tcpMIBCompliance2.setStatus(
        "current"
    )


# Export all MIB objects to the MIB builder

mibBuilder.exportSymbols(
    "TCP-MIB",
    **{"tcp": tcp,
       "tcpRtoAlgorithm": tcpRtoAlgorithm,
       "tcpRtoMin": tcpRtoMin,
       "tcpRtoMax": tcpRtoMax,
       "tcpMaxConn": tcpMaxConn,
       "tcpActiveOpens": tcpActiveOpens,
       "tcpPassiveOpens": tcpPassiveOpens,
       "tcpAttemptFails": tcpAttemptFails,
       "tcpEstabResets": tcpEstabResets,
       "tcpCurrEstab": tcpCurrEstab,
       "tcpInSegs": tcpInSegs,
       "tcpOutSegs": tcpOutSegs,
       "tcpRetransSegs": tcpRetransSegs,
       "tcpConnTable": tcpConnTable,
       "tcpConnEntry": tcpConnEntry,
       "tcpConnState": tcpConnState,
       "tcpConnLocalAddress": tcpConnLocalAddress,
       "tcpConnLocalPort": tcpConnLocalPort,
       "tcpConnRemAddress": tcpConnRemAddress,
       "tcpConnRemPort": tcpConnRemPort,
       "tcpInErrs": tcpInErrs,
       "tcpOutRsts": tcpOutRsts,
       "tcpHCInSegs": tcpHCInSegs,
       "tcpHCOutSegs": tcpHCOutSegs,
       "tcpConnectionTable": tcpConnectionTable,
       "tcpConnectionEntry": tcpConnectionEntry,
       "tcpConnectionLocalAddressType": tcpConnectionLocalAddressType,
       "tcpConnectionLocalAddress": tcpConnectionLocalAddress,
       "tcpConnectionLocalPort": tcpConnectionLocalPort,
       "tcpConnectionRemAddressType": tcpConnectionRemAddressType,
       "tcpConnectionRemAddress": tcpConnectionRemAddress,
       "tcpConnectionRemPort": tcpConnectionRemPort,
       "tcpConnectionState": tcpConnectionState,
       "tcpConnectionProcess": tcpConnectionProcess,
       "tcpListenerTable": tcpListenerTable,
       "tcpListenerEntry": tcpListenerEntry,
       "tcpListenerLocalAddressType": tcpListenerLocalAddressType,
       "tcpListenerLocalAddress": tcpListenerLocalAddress,
       "tcpListenerLocalPort": tcpListenerLocalPort,
       "tcpListenerProcess": tcpListenerProcess,
       "tcpMIB": tcpMIB,
       "tcpMIBConformance": tcpMIBConformance,
       "tcpMIBCompliances": tcpMIBCompliances,
       "tcpMIBCompliance": tcpMIBCompliance,
       "tcpMIBCompliance2": tcpMIBCompliance2,
       "tcpMIBGroups": tcpMIBGroups,
       "tcpGroup": tcpGroup,
       "tcpBaseGroup": tcpBaseGroup,
       "tcpConnectionGroup": tcpConnectionGroup,
       "tcpListenerGroup": tcpListenerGroup,
       "tcpHCGroup": tcpHCGroup}
)
