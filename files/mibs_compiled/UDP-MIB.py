# SNMP MIB module (UDP-MIB) expressed in pysnmp data model.
#
# This Python module is designed to be imported and executed by the
# pysnmp library.
#
# See https://www.pysnmp.com/pysnmp for further information.
#
# Notes
# -----
# ASN.1 source file://files/mibs/UDP-MIB
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

udpMIB = ModuleIdentity(
    (1, 3, 6, 1, 2, 1, 50)
)
udpMIB.setRevisions(
        ("2005-05-20 00:00",
         "1994-11-01 00:00",
         "1991-03-31 00:00")
)


# Types definitions


# TEXTUAL-CONVENTIONS



# MIB Managed Objects in the order of their OIDs

_Udp_ObjectIdentity = ObjectIdentity
udp = _Udp_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 7)
)
_UdpInDatagrams_Type = Counter32
_UdpInDatagrams_Object = MibScalar
udpInDatagrams = _UdpInDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 7, 1),
    _UdpInDatagrams_Type()
)
udpInDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    udpInDatagrams.setStatus("current")
_UdpNoPorts_Type = Counter32
_UdpNoPorts_Object = MibScalar
udpNoPorts = _UdpNoPorts_Object(
    (1, 3, 6, 1, 2, 1, 7, 2),
    _UdpNoPorts_Type()
)
udpNoPorts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    udpNoPorts.setStatus("current")
_UdpInErrors_Type = Counter32
_UdpInErrors_Object = MibScalar
udpInErrors = _UdpInErrors_Object(
    (1, 3, 6, 1, 2, 1, 7, 3),
    _UdpInErrors_Type()
)
udpInErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    udpInErrors.setStatus("current")
_UdpOutDatagrams_Type = Counter32
_UdpOutDatagrams_Object = MibScalar
udpOutDatagrams = _UdpOutDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 7, 4),
    _UdpOutDatagrams_Type()
)
udpOutDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    udpOutDatagrams.setStatus("current")
_UdpTable_Object = MibTable
udpTable = _UdpTable_Object(
    (1, 3, 6, 1, 2, 1, 7, 5)
)
if mibBuilder.loadTexts:
    udpTable.setStatus("deprecated")
_UdpEntry_Object = MibTableRow
udpEntry = _UdpEntry_Object(
    (1, 3, 6, 1, 2, 1, 7, 5, 1)
)
udpEntry.setIndexNames(
    (0, "UDP-MIB", "udpLocalAddress"),
    (0, "UDP-MIB", "udpLocalPort"),
)
if mibBuilder.loadTexts:
    udpEntry.setStatus("deprecated")
_UdpLocalAddress_Type = IpAddress
_UdpLocalAddress_Object = MibTableColumn
udpLocalAddress = _UdpLocalAddress_Object(
    (1, 3, 6, 1, 2, 1, 7, 5, 1, 1),
    _UdpLocalAddress_Type()
)
udpLocalAddress.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    udpLocalAddress.setStatus("deprecated")


class _UdpLocalPort_Type(Integer32):
    """Custom type udpLocalPort based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 65535),
    )


_UdpLocalPort_Type.__name__ = "Integer32"
_UdpLocalPort_Object = MibTableColumn
udpLocalPort = _UdpLocalPort_Object(
    (1, 3, 6, 1, 2, 1, 7, 5, 1, 2),
    _UdpLocalPort_Type()
)
udpLocalPort.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    udpLocalPort.setStatus("deprecated")
_UdpEndpointTable_Object = MibTable
udpEndpointTable = _UdpEndpointTable_Object(
    (1, 3, 6, 1, 2, 1, 7, 7)
)
if mibBuilder.loadTexts:
    udpEndpointTable.setStatus("current")
_UdpEndpointEntry_Object = MibTableRow
udpEndpointEntry = _UdpEndpointEntry_Object(
    (1, 3, 6, 1, 2, 1, 7, 7, 1)
)
udpEndpointEntry.setIndexNames(
    (0, "UDP-MIB", "udpEndpointLocalAddressType"),
    (0, "UDP-MIB", "udpEndpointLocalAddress"),
    (0, "UDP-MIB", "udpEndpointLocalPort"),
    (0, "UDP-MIB", "udpEndpointRemoteAddressType"),
    (0, "UDP-MIB", "udpEndpointRemoteAddress"),
    (0, "UDP-MIB", "udpEndpointRemotePort"),
    (0, "UDP-MIB", "udpEndpointInstance"),
)
if mibBuilder.loadTexts:
    udpEndpointEntry.setStatus("current")
_UdpEndpointLocalAddressType_Type = InetAddressType
_UdpEndpointLocalAddressType_Object = MibTableColumn
udpEndpointLocalAddressType = _UdpEndpointLocalAddressType_Object(
    (1, 3, 6, 1, 2, 1, 7, 7, 1, 1),
    _UdpEndpointLocalAddressType_Type()
)
udpEndpointLocalAddressType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    udpEndpointLocalAddressType.setStatus("current")
_UdpEndpointLocalAddress_Type = InetAddress
_UdpEndpointLocalAddress_Object = MibTableColumn
udpEndpointLocalAddress = _UdpEndpointLocalAddress_Object(
    (1, 3, 6, 1, 2, 1, 7, 7, 1, 2),
    _UdpEndpointLocalAddress_Type()
)
udpEndpointLocalAddress.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    udpEndpointLocalAddress.setStatus("current")
_UdpEndpointLocalPort_Type = InetPortNumber
_UdpEndpointLocalPort_Object = MibTableColumn
udpEndpointLocalPort = _UdpEndpointLocalPort_Object(
    (1, 3, 6, 1, 2, 1, 7, 7, 1, 3),
    _UdpEndpointLocalPort_Type()
)
udpEndpointLocalPort.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    udpEndpointLocalPort.setStatus("current")
_UdpEndpointRemoteAddressType_Type = InetAddressType
_UdpEndpointRemoteAddressType_Object = MibTableColumn
udpEndpointRemoteAddressType = _UdpEndpointRemoteAddressType_Object(
    (1, 3, 6, 1, 2, 1, 7, 7, 1, 4),
    _UdpEndpointRemoteAddressType_Type()
)
udpEndpointRemoteAddressType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    udpEndpointRemoteAddressType.setStatus("current")
_UdpEndpointRemoteAddress_Type = InetAddress
_UdpEndpointRemoteAddress_Object = MibTableColumn
udpEndpointRemoteAddress = _UdpEndpointRemoteAddress_Object(
    (1, 3, 6, 1, 2, 1, 7, 7, 1, 5),
    _UdpEndpointRemoteAddress_Type()
)
udpEndpointRemoteAddress.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    udpEndpointRemoteAddress.setStatus("current")
_UdpEndpointRemotePort_Type = InetPortNumber
_UdpEndpointRemotePort_Object = MibTableColumn
udpEndpointRemotePort = _UdpEndpointRemotePort_Object(
    (1, 3, 6, 1, 2, 1, 7, 7, 1, 6),
    _UdpEndpointRemotePort_Type()
)
udpEndpointRemotePort.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    udpEndpointRemotePort.setStatus("current")


class _UdpEndpointInstance_Type(Unsigned32):
    """Custom type udpEndpointInstance based on Unsigned32"""
    subtypeSpec = Unsigned32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(1, 4294967295),
    )


_UdpEndpointInstance_Type.__name__ = "Unsigned32"
_UdpEndpointInstance_Object = MibTableColumn
udpEndpointInstance = _UdpEndpointInstance_Object(
    (1, 3, 6, 1, 2, 1, 7, 7, 1, 7),
    _UdpEndpointInstance_Type()
)
udpEndpointInstance.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    udpEndpointInstance.setStatus("current")
_UdpEndpointProcess_Type = Unsigned32
_UdpEndpointProcess_Object = MibTableColumn
udpEndpointProcess = _UdpEndpointProcess_Object(
    (1, 3, 6, 1, 2, 1, 7, 7, 1, 8),
    _UdpEndpointProcess_Type()
)
udpEndpointProcess.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    udpEndpointProcess.setStatus("current")
_UdpHCInDatagrams_Type = Counter64
_UdpHCInDatagrams_Object = MibScalar
udpHCInDatagrams = _UdpHCInDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 7, 8),
    _UdpHCInDatagrams_Type()
)
udpHCInDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    udpHCInDatagrams.setStatus("current")
_UdpHCOutDatagrams_Type = Counter64
_UdpHCOutDatagrams_Object = MibScalar
udpHCOutDatagrams = _UdpHCOutDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 7, 9),
    _UdpHCOutDatagrams_Type()
)
udpHCOutDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    udpHCOutDatagrams.setStatus("current")
_UdpMIBConformance_ObjectIdentity = ObjectIdentity
udpMIBConformance = _UdpMIBConformance_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 50, 2)
)
_UdpMIBCompliances_ObjectIdentity = ObjectIdentity
udpMIBCompliances = _UdpMIBCompliances_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 50, 2, 1)
)
_UdpMIBGroups_ObjectIdentity = ObjectIdentity
udpMIBGroups = _UdpMIBGroups_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 50, 2, 2)
)

# Managed Objects groups

udpGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 50, 2, 2, 1)
)
udpGroup.setObjects(
      *(("UDP-MIB", "udpInDatagrams"),
        ("UDP-MIB", "udpNoPorts"),
        ("UDP-MIB", "udpInErrors"),
        ("UDP-MIB", "udpOutDatagrams"),
        ("UDP-MIB", "udpLocalAddress"),
        ("UDP-MIB", "udpLocalPort"))
)
if mibBuilder.loadTexts:
    udpGroup.setStatus("deprecated")

udpBaseGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 50, 2, 2, 2)
)
udpBaseGroup.setObjects(
      *(("UDP-MIB", "udpInDatagrams"),
        ("UDP-MIB", "udpNoPorts"),
        ("UDP-MIB", "udpInErrors"),
        ("UDP-MIB", "udpOutDatagrams"))
)
if mibBuilder.loadTexts:
    udpBaseGroup.setStatus("current")

udpHCGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 50, 2, 2, 3)
)
udpHCGroup.setObjects(
      *(("UDP-MIB", "udpHCInDatagrams"),
        ("UDP-MIB", "udpHCOutDatagrams"))
)
if mibBuilder.loadTexts:
    udpHCGroup.setStatus("current")

udpEndpointGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 50, 2, 2, 4)
)
udpEndpointGroup.setObjects(
    ("UDP-MIB", "udpEndpointProcess")
)
if mibBuilder.loadTexts:
    udpEndpointGroup.setStatus("current")


# Notification objects


# Notifications groups


# Agent capabilities


# Module compliance

udpMIBCompliance = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 50, 2, 1, 1)
)
if mibBuilder.loadTexts:
    udpMIBCompliance.setStatus(
        "deprecated"
    )

udpMIBCompliance2 = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 50, 2, 1, 2)
)
if mibBuilder.loadTexts:
    udpMIBCompliance2.setStatus(
        "current"
    )


# Export all MIB objects to the MIB builder

mibBuilder.exportSymbols(
    "UDP-MIB",
    **{"udp": udp,
       "udpInDatagrams": udpInDatagrams,
       "udpNoPorts": udpNoPorts,
       "udpInErrors": udpInErrors,
       "udpOutDatagrams": udpOutDatagrams,
       "udpTable": udpTable,
       "udpEntry": udpEntry,
       "udpLocalAddress": udpLocalAddress,
       "udpLocalPort": udpLocalPort,
       "udpEndpointTable": udpEndpointTable,
       "udpEndpointEntry": udpEndpointEntry,
       "udpEndpointLocalAddressType": udpEndpointLocalAddressType,
       "udpEndpointLocalAddress": udpEndpointLocalAddress,
       "udpEndpointLocalPort": udpEndpointLocalPort,
       "udpEndpointRemoteAddressType": udpEndpointRemoteAddressType,
       "udpEndpointRemoteAddress": udpEndpointRemoteAddress,
       "udpEndpointRemotePort": udpEndpointRemotePort,
       "udpEndpointInstance": udpEndpointInstance,
       "udpEndpointProcess": udpEndpointProcess,
       "udpHCInDatagrams": udpHCInDatagrams,
       "udpHCOutDatagrams": udpHCOutDatagrams,
       "udpMIB": udpMIB,
       "udpMIBConformance": udpMIBConformance,
       "udpMIBCompliances": udpMIBCompliances,
       "udpMIBCompliance": udpMIBCompliance,
       "udpMIBCompliance2": udpMIBCompliance2,
       "udpMIBGroups": udpMIBGroups,
       "udpGroup": udpGroup,
       "udpBaseGroup": udpBaseGroup,
       "udpHCGroup": udpHCGroup,
       "udpEndpointGroup": udpEndpointGroup}
)
