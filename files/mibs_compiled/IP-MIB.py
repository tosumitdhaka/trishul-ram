# SNMP MIB module (IP-MIB) expressed in pysnmp data model.
#
# This Python module is designed to be imported and executed by the
# pysnmp library.
#
# See https://www.pysnmp.com/pysnmp for further information.
#
# Notes
# -----
# ASN.1 source file://files/mibs/IP-MIB
# Produced by pysmi-1.4.3 at Fri May  1 19:17:39 2026
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

(InterfaceIndex,) = mibBuilder.importSymbols(
    "IF-MIB",
    "InterfaceIndex")

(InetAddress,
 InetZoneIndex,
 InetVersion,
 InetAddressPrefixLength,
 InetAddressType) = mibBuilder.importSymbols(
    "INET-ADDRESS-MIB",
    "InetAddress",
    "InetZoneIndex",
    "InetVersion",
    "InetAddressPrefixLength",
    "InetAddressType")

(ModuleCompliance,
 ObjectGroup,
 NotificationGroup) = mibBuilder.importSymbols(
    "SNMPv2-CONF",
    "ModuleCompliance",
    "ObjectGroup",
    "NotificationGroup")

(Gauge32,
 mib_2,
 zeroDotZero,
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
    "zeroDotZero",
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
 TextualConvention,
 TruthValue,
 RowStatus,
 StorageType,
 TimeStamp,
 TestAndIncr,
 RowPointer,
 PhysAddress) = mibBuilder.importSymbols(
    "SNMPv2-TC",
    "DisplayString",
    "TextualConvention",
    "TruthValue",
    "RowStatus",
    "StorageType",
    "TimeStamp",
    "TestAndIncr",
    "RowPointer",
    "PhysAddress")


# MODULE-IDENTITY

ipMIB = ModuleIdentity(
    (1, 3, 6, 1, 2, 1, 48)
)
ipMIB.setRevisions(
        ("2006-02-02 00:00",
         "1994-11-01 00:00",
         "1991-03-31 00:00")
)


# Types definitions


# TEXTUAL-CONVENTIONS



class IpAddressOriginTC(TextualConvention, Integer32):
    status = "current"
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2,
              4,
              5,
              6)
        )
    )
    namedValues = NamedValues(
        *(("dhcp", 4),
          ("linklayer", 5),
          ("manual", 2),
          ("other", 1),
          ("random", 6))
    )



class IpAddressStatusTC(TextualConvention, Integer32):
    status = "current"
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
              8)
        )
    )
    namedValues = NamedValues(
        *(("deprecated", 2),
          ("duplicate", 7),
          ("inaccessible", 4),
          ("invalid", 3),
          ("optimistic", 8),
          ("preferred", 1),
          ("tentative", 6),
          ("unknown", 5))
    )



class IpAddressPrefixOriginTC(TextualConvention, Integer32):
    status = "current"
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
        *(("dhcp", 4),
          ("manual", 2),
          ("other", 1),
          ("routeradv", 5),
          ("wellknown", 3))
    )



class Ipv6AddressIfIdentifierTC(TextualConvention, OctetString):
    status = "current"
    displayHint = "2x:"
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 8),
    )



# MIB Managed Objects in the order of their OIDs

_Ip_ObjectIdentity = ObjectIdentity
ip = _Ip_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 4)
)


class _IpForwarding_Type(Integer32):
    """Custom type ipForwarding based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2)
        )
    )
    namedValues = NamedValues(
        *(("forwarding", 1),
          ("notForwarding", 2))
    )


_IpForwarding_Type.__name__ = "Integer32"
_IpForwarding_Object = MibScalar
ipForwarding = _IpForwarding_Object(
    (1, 3, 6, 1, 2, 1, 4, 1),
    _IpForwarding_Type()
)
ipForwarding.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ipForwarding.setStatus("current")


class _IpDefaultTTL_Type(Integer32):
    """Custom type ipDefaultTTL based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(1, 255),
    )


_IpDefaultTTL_Type.__name__ = "Integer32"
_IpDefaultTTL_Object = MibScalar
ipDefaultTTL = _IpDefaultTTL_Object(
    (1, 3, 6, 1, 2, 1, 4, 2),
    _IpDefaultTTL_Type()
)
ipDefaultTTL.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ipDefaultTTL.setStatus("current")
_IpInReceives_Type = Counter32
_IpInReceives_Object = MibScalar
ipInReceives = _IpInReceives_Object(
    (1, 3, 6, 1, 2, 1, 4, 3),
    _IpInReceives_Type()
)
ipInReceives.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipInReceives.setStatus("deprecated")
_IpInHdrErrors_Type = Counter32
_IpInHdrErrors_Object = MibScalar
ipInHdrErrors = _IpInHdrErrors_Object(
    (1, 3, 6, 1, 2, 1, 4, 4),
    _IpInHdrErrors_Type()
)
ipInHdrErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipInHdrErrors.setStatus("deprecated")
_IpInAddrErrors_Type = Counter32
_IpInAddrErrors_Object = MibScalar
ipInAddrErrors = _IpInAddrErrors_Object(
    (1, 3, 6, 1, 2, 1, 4, 5),
    _IpInAddrErrors_Type()
)
ipInAddrErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipInAddrErrors.setStatus("deprecated")
_IpForwDatagrams_Type = Counter32
_IpForwDatagrams_Object = MibScalar
ipForwDatagrams = _IpForwDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 4, 6),
    _IpForwDatagrams_Type()
)
ipForwDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipForwDatagrams.setStatus("deprecated")
_IpInUnknownProtos_Type = Counter32
_IpInUnknownProtos_Object = MibScalar
ipInUnknownProtos = _IpInUnknownProtos_Object(
    (1, 3, 6, 1, 2, 1, 4, 7),
    _IpInUnknownProtos_Type()
)
ipInUnknownProtos.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipInUnknownProtos.setStatus("deprecated")
_IpInDiscards_Type = Counter32
_IpInDiscards_Object = MibScalar
ipInDiscards = _IpInDiscards_Object(
    (1, 3, 6, 1, 2, 1, 4, 8),
    _IpInDiscards_Type()
)
ipInDiscards.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipInDiscards.setStatus("deprecated")
_IpInDelivers_Type = Counter32
_IpInDelivers_Object = MibScalar
ipInDelivers = _IpInDelivers_Object(
    (1, 3, 6, 1, 2, 1, 4, 9),
    _IpInDelivers_Type()
)
ipInDelivers.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipInDelivers.setStatus("deprecated")
_IpOutRequests_Type = Counter32
_IpOutRequests_Object = MibScalar
ipOutRequests = _IpOutRequests_Object(
    (1, 3, 6, 1, 2, 1, 4, 10),
    _IpOutRequests_Type()
)
ipOutRequests.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipOutRequests.setStatus("deprecated")
_IpOutDiscards_Type = Counter32
_IpOutDiscards_Object = MibScalar
ipOutDiscards = _IpOutDiscards_Object(
    (1, 3, 6, 1, 2, 1, 4, 11),
    _IpOutDiscards_Type()
)
ipOutDiscards.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipOutDiscards.setStatus("deprecated")
_IpOutNoRoutes_Type = Counter32
_IpOutNoRoutes_Object = MibScalar
ipOutNoRoutes = _IpOutNoRoutes_Object(
    (1, 3, 6, 1, 2, 1, 4, 12),
    _IpOutNoRoutes_Type()
)
ipOutNoRoutes.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipOutNoRoutes.setStatus("deprecated")
_IpReasmTimeout_Type = Integer32
_IpReasmTimeout_Object = MibScalar
ipReasmTimeout = _IpReasmTimeout_Object(
    (1, 3, 6, 1, 2, 1, 4, 13),
    _IpReasmTimeout_Type()
)
ipReasmTimeout.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipReasmTimeout.setStatus("current")
if mibBuilder.loadTexts:
    ipReasmTimeout.setUnits("seconds")
_IpReasmReqds_Type = Counter32
_IpReasmReqds_Object = MibScalar
ipReasmReqds = _IpReasmReqds_Object(
    (1, 3, 6, 1, 2, 1, 4, 14),
    _IpReasmReqds_Type()
)
ipReasmReqds.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipReasmReqds.setStatus("deprecated")
_IpReasmOKs_Type = Counter32
_IpReasmOKs_Object = MibScalar
ipReasmOKs = _IpReasmOKs_Object(
    (1, 3, 6, 1, 2, 1, 4, 15),
    _IpReasmOKs_Type()
)
ipReasmOKs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipReasmOKs.setStatus("deprecated")
_IpReasmFails_Type = Counter32
_IpReasmFails_Object = MibScalar
ipReasmFails = _IpReasmFails_Object(
    (1, 3, 6, 1, 2, 1, 4, 16),
    _IpReasmFails_Type()
)
ipReasmFails.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipReasmFails.setStatus("deprecated")
_IpFragOKs_Type = Counter32
_IpFragOKs_Object = MibScalar
ipFragOKs = _IpFragOKs_Object(
    (1, 3, 6, 1, 2, 1, 4, 17),
    _IpFragOKs_Type()
)
ipFragOKs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipFragOKs.setStatus("deprecated")
_IpFragFails_Type = Counter32
_IpFragFails_Object = MibScalar
ipFragFails = _IpFragFails_Object(
    (1, 3, 6, 1, 2, 1, 4, 18),
    _IpFragFails_Type()
)
ipFragFails.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipFragFails.setStatus("deprecated")
_IpFragCreates_Type = Counter32
_IpFragCreates_Object = MibScalar
ipFragCreates = _IpFragCreates_Object(
    (1, 3, 6, 1, 2, 1, 4, 19),
    _IpFragCreates_Type()
)
ipFragCreates.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipFragCreates.setStatus("deprecated")
_IpAddrTable_Object = MibTable
ipAddrTable = _IpAddrTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 20)
)
if mibBuilder.loadTexts:
    ipAddrTable.setStatus("deprecated")
_IpAddrEntry_Object = MibTableRow
ipAddrEntry = _IpAddrEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 20, 1)
)
ipAddrEntry.setIndexNames(
    (0, "IP-MIB", "ipAdEntAddr"),
)
if mibBuilder.loadTexts:
    ipAddrEntry.setStatus("deprecated")
_IpAdEntAddr_Type = IpAddress
_IpAdEntAddr_Object = MibTableColumn
ipAdEntAddr = _IpAdEntAddr_Object(
    (1, 3, 6, 1, 2, 1, 4, 20, 1, 1),
    _IpAdEntAddr_Type()
)
ipAdEntAddr.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAdEntAddr.setStatus("deprecated")


class _IpAdEntIfIndex_Type(Integer32):
    """Custom type ipAdEntIfIndex based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(1, 2147483647),
    )


_IpAdEntIfIndex_Type.__name__ = "Integer32"
_IpAdEntIfIndex_Object = MibTableColumn
ipAdEntIfIndex = _IpAdEntIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 20, 1, 2),
    _IpAdEntIfIndex_Type()
)
ipAdEntIfIndex.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAdEntIfIndex.setStatus("deprecated")
_IpAdEntNetMask_Type = IpAddress
_IpAdEntNetMask_Object = MibTableColumn
ipAdEntNetMask = _IpAdEntNetMask_Object(
    (1, 3, 6, 1, 2, 1, 4, 20, 1, 3),
    _IpAdEntNetMask_Type()
)
ipAdEntNetMask.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAdEntNetMask.setStatus("deprecated")


class _IpAdEntBcastAddr_Type(Integer32):
    """Custom type ipAdEntBcastAddr based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 1),
    )


_IpAdEntBcastAddr_Type.__name__ = "Integer32"
_IpAdEntBcastAddr_Object = MibTableColumn
ipAdEntBcastAddr = _IpAdEntBcastAddr_Object(
    (1, 3, 6, 1, 2, 1, 4, 20, 1, 4),
    _IpAdEntBcastAddr_Type()
)
ipAdEntBcastAddr.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAdEntBcastAddr.setStatus("deprecated")


class _IpAdEntReasmMaxSize_Type(Integer32):
    """Custom type ipAdEntReasmMaxSize based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 65535),
    )


_IpAdEntReasmMaxSize_Type.__name__ = "Integer32"
_IpAdEntReasmMaxSize_Object = MibTableColumn
ipAdEntReasmMaxSize = _IpAdEntReasmMaxSize_Object(
    (1, 3, 6, 1, 2, 1, 4, 20, 1, 5),
    _IpAdEntReasmMaxSize_Type()
)
ipAdEntReasmMaxSize.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAdEntReasmMaxSize.setStatus("deprecated")
_IpNetToMediaTable_Object = MibTable
ipNetToMediaTable = _IpNetToMediaTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 22)
)
if mibBuilder.loadTexts:
    ipNetToMediaTable.setStatus("deprecated")
_IpNetToMediaEntry_Object = MibTableRow
ipNetToMediaEntry = _IpNetToMediaEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 22, 1)
)
ipNetToMediaEntry.setIndexNames(
    (0, "IP-MIB", "ipNetToMediaIfIndex"),
    (0, "IP-MIB", "ipNetToMediaNetAddress"),
)
if mibBuilder.loadTexts:
    ipNetToMediaEntry.setStatus("deprecated")


class _IpNetToMediaIfIndex_Type(Integer32):
    """Custom type ipNetToMediaIfIndex based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(1, 2147483647),
    )


_IpNetToMediaIfIndex_Type.__name__ = "Integer32"
_IpNetToMediaIfIndex_Object = MibTableColumn
ipNetToMediaIfIndex = _IpNetToMediaIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 22, 1, 1),
    _IpNetToMediaIfIndex_Type()
)
ipNetToMediaIfIndex.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipNetToMediaIfIndex.setStatus("deprecated")


class _IpNetToMediaPhysAddress_Type(PhysAddress):
    """Custom type ipNetToMediaPhysAddress based on PhysAddress"""
    subtypeSpec = PhysAddress.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 65535),
    )


_IpNetToMediaPhysAddress_Type.__name__ = "PhysAddress"
_IpNetToMediaPhysAddress_Object = MibTableColumn
ipNetToMediaPhysAddress = _IpNetToMediaPhysAddress_Object(
    (1, 3, 6, 1, 2, 1, 4, 22, 1, 2),
    _IpNetToMediaPhysAddress_Type()
)
ipNetToMediaPhysAddress.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipNetToMediaPhysAddress.setStatus("deprecated")
_IpNetToMediaNetAddress_Type = IpAddress
_IpNetToMediaNetAddress_Object = MibTableColumn
ipNetToMediaNetAddress = _IpNetToMediaNetAddress_Object(
    (1, 3, 6, 1, 2, 1, 4, 22, 1, 3),
    _IpNetToMediaNetAddress_Type()
)
ipNetToMediaNetAddress.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipNetToMediaNetAddress.setStatus("deprecated")


class _IpNetToMediaType_Type(Integer32):
    """Custom type ipNetToMediaType based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2,
              3,
              4)
        )
    )
    namedValues = NamedValues(
        *(("dynamic", 3),
          ("invalid", 2),
          ("other", 1),
          ("static", 4))
    )


_IpNetToMediaType_Type.__name__ = "Integer32"
_IpNetToMediaType_Object = MibTableColumn
ipNetToMediaType = _IpNetToMediaType_Object(
    (1, 3, 6, 1, 2, 1, 4, 22, 1, 4),
    _IpNetToMediaType_Type()
)
ipNetToMediaType.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipNetToMediaType.setStatus("deprecated")
_IpRoutingDiscards_Type = Counter32
_IpRoutingDiscards_Object = MibScalar
ipRoutingDiscards = _IpRoutingDiscards_Object(
    (1, 3, 6, 1, 2, 1, 4, 23),
    _IpRoutingDiscards_Type()
)
ipRoutingDiscards.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipRoutingDiscards.setStatus("deprecated")


class _Ipv6IpForwarding_Type(Integer32):
    """Custom type ipv6IpForwarding based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2)
        )
    )
    namedValues = NamedValues(
        *(("forwarding", 1),
          ("notForwarding", 2))
    )


_Ipv6IpForwarding_Type.__name__ = "Integer32"
_Ipv6IpForwarding_Object = MibScalar
ipv6IpForwarding = _Ipv6IpForwarding_Object(
    (1, 3, 6, 1, 2, 1, 4, 25),
    _Ipv6IpForwarding_Type()
)
ipv6IpForwarding.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ipv6IpForwarding.setStatus("current")


class _Ipv6IpDefaultHopLimit_Type(Integer32):
    """Custom type ipv6IpDefaultHopLimit based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 255),
    )


_Ipv6IpDefaultHopLimit_Type.__name__ = "Integer32"
_Ipv6IpDefaultHopLimit_Object = MibScalar
ipv6IpDefaultHopLimit = _Ipv6IpDefaultHopLimit_Object(
    (1, 3, 6, 1, 2, 1, 4, 26),
    _Ipv6IpDefaultHopLimit_Type()
)
ipv6IpDefaultHopLimit.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ipv6IpDefaultHopLimit.setStatus("current")
_Ipv4InterfaceTableLastChange_Type = TimeStamp
_Ipv4InterfaceTableLastChange_Object = MibScalar
ipv4InterfaceTableLastChange = _Ipv4InterfaceTableLastChange_Object(
    (1, 3, 6, 1, 2, 1, 4, 27),
    _Ipv4InterfaceTableLastChange_Type()
)
ipv4InterfaceTableLastChange.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv4InterfaceTableLastChange.setStatus("current")
_Ipv4InterfaceTable_Object = MibTable
ipv4InterfaceTable = _Ipv4InterfaceTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 28)
)
if mibBuilder.loadTexts:
    ipv4InterfaceTable.setStatus("current")
_Ipv4InterfaceEntry_Object = MibTableRow
ipv4InterfaceEntry = _Ipv4InterfaceEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 28, 1)
)
ipv4InterfaceEntry.setIndexNames(
    (0, "IP-MIB", "ipv4InterfaceIfIndex"),
)
if mibBuilder.loadTexts:
    ipv4InterfaceEntry.setStatus("current")
_Ipv4InterfaceIfIndex_Type = InterfaceIndex
_Ipv4InterfaceIfIndex_Object = MibTableColumn
ipv4InterfaceIfIndex = _Ipv4InterfaceIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 28, 1, 1),
    _Ipv4InterfaceIfIndex_Type()
)
ipv4InterfaceIfIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipv4InterfaceIfIndex.setStatus("current")


class _Ipv4InterfaceReasmMaxSize_Type(Integer32):
    """Custom type ipv4InterfaceReasmMaxSize based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 65535),
    )


_Ipv4InterfaceReasmMaxSize_Type.__name__ = "Integer32"
_Ipv4InterfaceReasmMaxSize_Object = MibTableColumn
ipv4InterfaceReasmMaxSize = _Ipv4InterfaceReasmMaxSize_Object(
    (1, 3, 6, 1, 2, 1, 4, 28, 1, 2),
    _Ipv4InterfaceReasmMaxSize_Type()
)
ipv4InterfaceReasmMaxSize.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv4InterfaceReasmMaxSize.setStatus("current")


class _Ipv4InterfaceEnableStatus_Type(Integer32):
    """Custom type ipv4InterfaceEnableStatus based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2)
        )
    )
    namedValues = NamedValues(
        *(("down", 2),
          ("up", 1))
    )


_Ipv4InterfaceEnableStatus_Type.__name__ = "Integer32"
_Ipv4InterfaceEnableStatus_Object = MibTableColumn
ipv4InterfaceEnableStatus = _Ipv4InterfaceEnableStatus_Object(
    (1, 3, 6, 1, 2, 1, 4, 28, 1, 3),
    _Ipv4InterfaceEnableStatus_Type()
)
ipv4InterfaceEnableStatus.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ipv4InterfaceEnableStatus.setStatus("current")


class _Ipv4InterfaceRetransmitTime_Type(Unsigned32):
    """Custom type ipv4InterfaceRetransmitTime based on Unsigned32"""
    defaultValue = 1000


_Ipv4InterfaceRetransmitTime_Object = MibTableColumn
ipv4InterfaceRetransmitTime = _Ipv4InterfaceRetransmitTime_Object(
    (1, 3, 6, 1, 2, 1, 4, 28, 1, 4),
    _Ipv4InterfaceRetransmitTime_Type()
)
ipv4InterfaceRetransmitTime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv4InterfaceRetransmitTime.setStatus("current")
if mibBuilder.loadTexts:
    ipv4InterfaceRetransmitTime.setUnits("milliseconds")
_Ipv6InterfaceTableLastChange_Type = TimeStamp
_Ipv6InterfaceTableLastChange_Object = MibScalar
ipv6InterfaceTableLastChange = _Ipv6InterfaceTableLastChange_Object(
    (1, 3, 6, 1, 2, 1, 4, 29),
    _Ipv6InterfaceTableLastChange_Type()
)
ipv6InterfaceTableLastChange.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6InterfaceTableLastChange.setStatus("current")
_Ipv6InterfaceTable_Object = MibTable
ipv6InterfaceTable = _Ipv6InterfaceTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 30)
)
if mibBuilder.loadTexts:
    ipv6InterfaceTable.setStatus("current")
_Ipv6InterfaceEntry_Object = MibTableRow
ipv6InterfaceEntry = _Ipv6InterfaceEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 30, 1)
)
ipv6InterfaceEntry.setIndexNames(
    (0, "IP-MIB", "ipv6InterfaceIfIndex"),
)
if mibBuilder.loadTexts:
    ipv6InterfaceEntry.setStatus("current")
_Ipv6InterfaceIfIndex_Type = InterfaceIndex
_Ipv6InterfaceIfIndex_Object = MibTableColumn
ipv6InterfaceIfIndex = _Ipv6InterfaceIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 30, 1, 1),
    _Ipv6InterfaceIfIndex_Type()
)
ipv6InterfaceIfIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipv6InterfaceIfIndex.setStatus("current")


class _Ipv6InterfaceReasmMaxSize_Type(Unsigned32):
    """Custom type ipv6InterfaceReasmMaxSize based on Unsigned32"""
    subtypeSpec = Unsigned32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(1500, 65535),
    )


_Ipv6InterfaceReasmMaxSize_Type.__name__ = "Unsigned32"
_Ipv6InterfaceReasmMaxSize_Object = MibTableColumn
ipv6InterfaceReasmMaxSize = _Ipv6InterfaceReasmMaxSize_Object(
    (1, 3, 6, 1, 2, 1, 4, 30, 1, 2),
    _Ipv6InterfaceReasmMaxSize_Type()
)
ipv6InterfaceReasmMaxSize.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6InterfaceReasmMaxSize.setStatus("current")
if mibBuilder.loadTexts:
    ipv6InterfaceReasmMaxSize.setUnits("octets")
_Ipv6InterfaceIdentifier_Type = Ipv6AddressIfIdentifierTC
_Ipv6InterfaceIdentifier_Object = MibTableColumn
ipv6InterfaceIdentifier = _Ipv6InterfaceIdentifier_Object(
    (1, 3, 6, 1, 2, 1, 4, 30, 1, 3),
    _Ipv6InterfaceIdentifier_Type()
)
ipv6InterfaceIdentifier.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6InterfaceIdentifier.setStatus("current")


class _Ipv6InterfaceEnableStatus_Type(Integer32):
    """Custom type ipv6InterfaceEnableStatus based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2)
        )
    )
    namedValues = NamedValues(
        *(("down", 2),
          ("up", 1))
    )


_Ipv6InterfaceEnableStatus_Type.__name__ = "Integer32"
_Ipv6InterfaceEnableStatus_Object = MibTableColumn
ipv6InterfaceEnableStatus = _Ipv6InterfaceEnableStatus_Object(
    (1, 3, 6, 1, 2, 1, 4, 30, 1, 5),
    _Ipv6InterfaceEnableStatus_Type()
)
ipv6InterfaceEnableStatus.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ipv6InterfaceEnableStatus.setStatus("current")
_Ipv6InterfaceReachableTime_Type = Unsigned32
_Ipv6InterfaceReachableTime_Object = MibTableColumn
ipv6InterfaceReachableTime = _Ipv6InterfaceReachableTime_Object(
    (1, 3, 6, 1, 2, 1, 4, 30, 1, 6),
    _Ipv6InterfaceReachableTime_Type()
)
ipv6InterfaceReachableTime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6InterfaceReachableTime.setStatus("current")
if mibBuilder.loadTexts:
    ipv6InterfaceReachableTime.setUnits("milliseconds")
_Ipv6InterfaceRetransmitTime_Type = Unsigned32
_Ipv6InterfaceRetransmitTime_Object = MibTableColumn
ipv6InterfaceRetransmitTime = _Ipv6InterfaceRetransmitTime_Object(
    (1, 3, 6, 1, 2, 1, 4, 30, 1, 7),
    _Ipv6InterfaceRetransmitTime_Type()
)
ipv6InterfaceRetransmitTime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6InterfaceRetransmitTime.setStatus("current")
if mibBuilder.loadTexts:
    ipv6InterfaceRetransmitTime.setUnits("milliseconds")


class _Ipv6InterfaceForwarding_Type(Integer32):
    """Custom type ipv6InterfaceForwarding based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2)
        )
    )
    namedValues = NamedValues(
        *(("forwarding", 1),
          ("notForwarding", 2))
    )


_Ipv6InterfaceForwarding_Type.__name__ = "Integer32"
_Ipv6InterfaceForwarding_Object = MibTableColumn
ipv6InterfaceForwarding = _Ipv6InterfaceForwarding_Object(
    (1, 3, 6, 1, 2, 1, 4, 30, 1, 8),
    _Ipv6InterfaceForwarding_Type()
)
ipv6InterfaceForwarding.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ipv6InterfaceForwarding.setStatus("current")
_IpTrafficStats_ObjectIdentity = ObjectIdentity
ipTrafficStats = _IpTrafficStats_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 4, 31)
)
_IpSystemStatsTable_Object = MibTable
ipSystemStatsTable = _IpSystemStatsTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1)
)
if mibBuilder.loadTexts:
    ipSystemStatsTable.setStatus("current")
_IpSystemStatsEntry_Object = MibTableRow
ipSystemStatsEntry = _IpSystemStatsEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1)
)
ipSystemStatsEntry.setIndexNames(
    (0, "IP-MIB", "ipSystemStatsIPVersion"),
)
if mibBuilder.loadTexts:
    ipSystemStatsEntry.setStatus("current")
_IpSystemStatsIPVersion_Type = InetVersion
_IpSystemStatsIPVersion_Object = MibTableColumn
ipSystemStatsIPVersion = _IpSystemStatsIPVersion_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 1),
    _IpSystemStatsIPVersion_Type()
)
ipSystemStatsIPVersion.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipSystemStatsIPVersion.setStatus("current")
_IpSystemStatsInReceives_Type = Counter32
_IpSystemStatsInReceives_Object = MibTableColumn
ipSystemStatsInReceives = _IpSystemStatsInReceives_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 3),
    _IpSystemStatsInReceives_Type()
)
ipSystemStatsInReceives.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInReceives.setStatus("current")
_IpSystemStatsHCInReceives_Type = Counter64
_IpSystemStatsHCInReceives_Object = MibTableColumn
ipSystemStatsHCInReceives = _IpSystemStatsHCInReceives_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 4),
    _IpSystemStatsHCInReceives_Type()
)
ipSystemStatsHCInReceives.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCInReceives.setStatus("current")
_IpSystemStatsInOctets_Type = Counter32
_IpSystemStatsInOctets_Object = MibTableColumn
ipSystemStatsInOctets = _IpSystemStatsInOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 5),
    _IpSystemStatsInOctets_Type()
)
ipSystemStatsInOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInOctets.setStatus("current")
_IpSystemStatsHCInOctets_Type = Counter64
_IpSystemStatsHCInOctets_Object = MibTableColumn
ipSystemStatsHCInOctets = _IpSystemStatsHCInOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 6),
    _IpSystemStatsHCInOctets_Type()
)
ipSystemStatsHCInOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCInOctets.setStatus("current")
_IpSystemStatsInHdrErrors_Type = Counter32
_IpSystemStatsInHdrErrors_Object = MibTableColumn
ipSystemStatsInHdrErrors = _IpSystemStatsInHdrErrors_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 7),
    _IpSystemStatsInHdrErrors_Type()
)
ipSystemStatsInHdrErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInHdrErrors.setStatus("current")
_IpSystemStatsInNoRoutes_Type = Counter32
_IpSystemStatsInNoRoutes_Object = MibTableColumn
ipSystemStatsInNoRoutes = _IpSystemStatsInNoRoutes_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 8),
    _IpSystemStatsInNoRoutes_Type()
)
ipSystemStatsInNoRoutes.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInNoRoutes.setStatus("current")
_IpSystemStatsInAddrErrors_Type = Counter32
_IpSystemStatsInAddrErrors_Object = MibTableColumn
ipSystemStatsInAddrErrors = _IpSystemStatsInAddrErrors_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 9),
    _IpSystemStatsInAddrErrors_Type()
)
ipSystemStatsInAddrErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInAddrErrors.setStatus("current")
_IpSystemStatsInUnknownProtos_Type = Counter32
_IpSystemStatsInUnknownProtos_Object = MibTableColumn
ipSystemStatsInUnknownProtos = _IpSystemStatsInUnknownProtos_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 10),
    _IpSystemStatsInUnknownProtos_Type()
)
ipSystemStatsInUnknownProtos.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInUnknownProtos.setStatus("current")
_IpSystemStatsInTruncatedPkts_Type = Counter32
_IpSystemStatsInTruncatedPkts_Object = MibTableColumn
ipSystemStatsInTruncatedPkts = _IpSystemStatsInTruncatedPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 11),
    _IpSystemStatsInTruncatedPkts_Type()
)
ipSystemStatsInTruncatedPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInTruncatedPkts.setStatus("current")
_IpSystemStatsInForwDatagrams_Type = Counter32
_IpSystemStatsInForwDatagrams_Object = MibTableColumn
ipSystemStatsInForwDatagrams = _IpSystemStatsInForwDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 12),
    _IpSystemStatsInForwDatagrams_Type()
)
ipSystemStatsInForwDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInForwDatagrams.setStatus("current")
_IpSystemStatsHCInForwDatagrams_Type = Counter64
_IpSystemStatsHCInForwDatagrams_Object = MibTableColumn
ipSystemStatsHCInForwDatagrams = _IpSystemStatsHCInForwDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 13),
    _IpSystemStatsHCInForwDatagrams_Type()
)
ipSystemStatsHCInForwDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCInForwDatagrams.setStatus("current")
_IpSystemStatsReasmReqds_Type = Counter32
_IpSystemStatsReasmReqds_Object = MibTableColumn
ipSystemStatsReasmReqds = _IpSystemStatsReasmReqds_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 14),
    _IpSystemStatsReasmReqds_Type()
)
ipSystemStatsReasmReqds.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsReasmReqds.setStatus("current")
_IpSystemStatsReasmOKs_Type = Counter32
_IpSystemStatsReasmOKs_Object = MibTableColumn
ipSystemStatsReasmOKs = _IpSystemStatsReasmOKs_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 15),
    _IpSystemStatsReasmOKs_Type()
)
ipSystemStatsReasmOKs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsReasmOKs.setStatus("current")
_IpSystemStatsReasmFails_Type = Counter32
_IpSystemStatsReasmFails_Object = MibTableColumn
ipSystemStatsReasmFails = _IpSystemStatsReasmFails_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 16),
    _IpSystemStatsReasmFails_Type()
)
ipSystemStatsReasmFails.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsReasmFails.setStatus("current")
_IpSystemStatsInDiscards_Type = Counter32
_IpSystemStatsInDiscards_Object = MibTableColumn
ipSystemStatsInDiscards = _IpSystemStatsInDiscards_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 17),
    _IpSystemStatsInDiscards_Type()
)
ipSystemStatsInDiscards.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInDiscards.setStatus("current")
_IpSystemStatsInDelivers_Type = Counter32
_IpSystemStatsInDelivers_Object = MibTableColumn
ipSystemStatsInDelivers = _IpSystemStatsInDelivers_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 18),
    _IpSystemStatsInDelivers_Type()
)
ipSystemStatsInDelivers.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInDelivers.setStatus("current")
_IpSystemStatsHCInDelivers_Type = Counter64
_IpSystemStatsHCInDelivers_Object = MibTableColumn
ipSystemStatsHCInDelivers = _IpSystemStatsHCInDelivers_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 19),
    _IpSystemStatsHCInDelivers_Type()
)
ipSystemStatsHCInDelivers.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCInDelivers.setStatus("current")
_IpSystemStatsOutRequests_Type = Counter32
_IpSystemStatsOutRequests_Object = MibTableColumn
ipSystemStatsOutRequests = _IpSystemStatsOutRequests_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 20),
    _IpSystemStatsOutRequests_Type()
)
ipSystemStatsOutRequests.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutRequests.setStatus("current")
_IpSystemStatsHCOutRequests_Type = Counter64
_IpSystemStatsHCOutRequests_Object = MibTableColumn
ipSystemStatsHCOutRequests = _IpSystemStatsHCOutRequests_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 21),
    _IpSystemStatsHCOutRequests_Type()
)
ipSystemStatsHCOutRequests.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCOutRequests.setStatus("current")
_IpSystemStatsOutNoRoutes_Type = Counter32
_IpSystemStatsOutNoRoutes_Object = MibTableColumn
ipSystemStatsOutNoRoutes = _IpSystemStatsOutNoRoutes_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 22),
    _IpSystemStatsOutNoRoutes_Type()
)
ipSystemStatsOutNoRoutes.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutNoRoutes.setStatus("current")
_IpSystemStatsOutForwDatagrams_Type = Counter32
_IpSystemStatsOutForwDatagrams_Object = MibTableColumn
ipSystemStatsOutForwDatagrams = _IpSystemStatsOutForwDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 23),
    _IpSystemStatsOutForwDatagrams_Type()
)
ipSystemStatsOutForwDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutForwDatagrams.setStatus("current")
_IpSystemStatsHCOutForwDatagrams_Type = Counter64
_IpSystemStatsHCOutForwDatagrams_Object = MibTableColumn
ipSystemStatsHCOutForwDatagrams = _IpSystemStatsHCOutForwDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 24),
    _IpSystemStatsHCOutForwDatagrams_Type()
)
ipSystemStatsHCOutForwDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCOutForwDatagrams.setStatus("current")
_IpSystemStatsOutDiscards_Type = Counter32
_IpSystemStatsOutDiscards_Object = MibTableColumn
ipSystemStatsOutDiscards = _IpSystemStatsOutDiscards_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 25),
    _IpSystemStatsOutDiscards_Type()
)
ipSystemStatsOutDiscards.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutDiscards.setStatus("current")
_IpSystemStatsOutFragReqds_Type = Counter32
_IpSystemStatsOutFragReqds_Object = MibTableColumn
ipSystemStatsOutFragReqds = _IpSystemStatsOutFragReqds_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 26),
    _IpSystemStatsOutFragReqds_Type()
)
ipSystemStatsOutFragReqds.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutFragReqds.setStatus("current")
_IpSystemStatsOutFragOKs_Type = Counter32
_IpSystemStatsOutFragOKs_Object = MibTableColumn
ipSystemStatsOutFragOKs = _IpSystemStatsOutFragOKs_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 27),
    _IpSystemStatsOutFragOKs_Type()
)
ipSystemStatsOutFragOKs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutFragOKs.setStatus("current")
_IpSystemStatsOutFragFails_Type = Counter32
_IpSystemStatsOutFragFails_Object = MibTableColumn
ipSystemStatsOutFragFails = _IpSystemStatsOutFragFails_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 28),
    _IpSystemStatsOutFragFails_Type()
)
ipSystemStatsOutFragFails.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutFragFails.setStatus("current")
_IpSystemStatsOutFragCreates_Type = Counter32
_IpSystemStatsOutFragCreates_Object = MibTableColumn
ipSystemStatsOutFragCreates = _IpSystemStatsOutFragCreates_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 29),
    _IpSystemStatsOutFragCreates_Type()
)
ipSystemStatsOutFragCreates.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutFragCreates.setStatus("current")
_IpSystemStatsOutTransmits_Type = Counter32
_IpSystemStatsOutTransmits_Object = MibTableColumn
ipSystemStatsOutTransmits = _IpSystemStatsOutTransmits_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 30),
    _IpSystemStatsOutTransmits_Type()
)
ipSystemStatsOutTransmits.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutTransmits.setStatus("current")
_IpSystemStatsHCOutTransmits_Type = Counter64
_IpSystemStatsHCOutTransmits_Object = MibTableColumn
ipSystemStatsHCOutTransmits = _IpSystemStatsHCOutTransmits_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 31),
    _IpSystemStatsHCOutTransmits_Type()
)
ipSystemStatsHCOutTransmits.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCOutTransmits.setStatus("current")
_IpSystemStatsOutOctets_Type = Counter32
_IpSystemStatsOutOctets_Object = MibTableColumn
ipSystemStatsOutOctets = _IpSystemStatsOutOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 32),
    _IpSystemStatsOutOctets_Type()
)
ipSystemStatsOutOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutOctets.setStatus("current")
_IpSystemStatsHCOutOctets_Type = Counter64
_IpSystemStatsHCOutOctets_Object = MibTableColumn
ipSystemStatsHCOutOctets = _IpSystemStatsHCOutOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 33),
    _IpSystemStatsHCOutOctets_Type()
)
ipSystemStatsHCOutOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCOutOctets.setStatus("current")
_IpSystemStatsInMcastPkts_Type = Counter32
_IpSystemStatsInMcastPkts_Object = MibTableColumn
ipSystemStatsInMcastPkts = _IpSystemStatsInMcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 34),
    _IpSystemStatsInMcastPkts_Type()
)
ipSystemStatsInMcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInMcastPkts.setStatus("current")
_IpSystemStatsHCInMcastPkts_Type = Counter64
_IpSystemStatsHCInMcastPkts_Object = MibTableColumn
ipSystemStatsHCInMcastPkts = _IpSystemStatsHCInMcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 35),
    _IpSystemStatsHCInMcastPkts_Type()
)
ipSystemStatsHCInMcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCInMcastPkts.setStatus("current")
_IpSystemStatsInMcastOctets_Type = Counter32
_IpSystemStatsInMcastOctets_Object = MibTableColumn
ipSystemStatsInMcastOctets = _IpSystemStatsInMcastOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 36),
    _IpSystemStatsInMcastOctets_Type()
)
ipSystemStatsInMcastOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInMcastOctets.setStatus("current")
_IpSystemStatsHCInMcastOctets_Type = Counter64
_IpSystemStatsHCInMcastOctets_Object = MibTableColumn
ipSystemStatsHCInMcastOctets = _IpSystemStatsHCInMcastOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 37),
    _IpSystemStatsHCInMcastOctets_Type()
)
ipSystemStatsHCInMcastOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCInMcastOctets.setStatus("current")
_IpSystemStatsOutMcastPkts_Type = Counter32
_IpSystemStatsOutMcastPkts_Object = MibTableColumn
ipSystemStatsOutMcastPkts = _IpSystemStatsOutMcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 38),
    _IpSystemStatsOutMcastPkts_Type()
)
ipSystemStatsOutMcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutMcastPkts.setStatus("current")
_IpSystemStatsHCOutMcastPkts_Type = Counter64
_IpSystemStatsHCOutMcastPkts_Object = MibTableColumn
ipSystemStatsHCOutMcastPkts = _IpSystemStatsHCOutMcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 39),
    _IpSystemStatsHCOutMcastPkts_Type()
)
ipSystemStatsHCOutMcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCOutMcastPkts.setStatus("current")
_IpSystemStatsOutMcastOctets_Type = Counter32
_IpSystemStatsOutMcastOctets_Object = MibTableColumn
ipSystemStatsOutMcastOctets = _IpSystemStatsOutMcastOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 40),
    _IpSystemStatsOutMcastOctets_Type()
)
ipSystemStatsOutMcastOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutMcastOctets.setStatus("current")
_IpSystemStatsHCOutMcastOctets_Type = Counter64
_IpSystemStatsHCOutMcastOctets_Object = MibTableColumn
ipSystemStatsHCOutMcastOctets = _IpSystemStatsHCOutMcastOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 41),
    _IpSystemStatsHCOutMcastOctets_Type()
)
ipSystemStatsHCOutMcastOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCOutMcastOctets.setStatus("current")
_IpSystemStatsInBcastPkts_Type = Counter32
_IpSystemStatsInBcastPkts_Object = MibTableColumn
ipSystemStatsInBcastPkts = _IpSystemStatsInBcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 42),
    _IpSystemStatsInBcastPkts_Type()
)
ipSystemStatsInBcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsInBcastPkts.setStatus("current")
_IpSystemStatsHCInBcastPkts_Type = Counter64
_IpSystemStatsHCInBcastPkts_Object = MibTableColumn
ipSystemStatsHCInBcastPkts = _IpSystemStatsHCInBcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 43),
    _IpSystemStatsHCInBcastPkts_Type()
)
ipSystemStatsHCInBcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCInBcastPkts.setStatus("current")
_IpSystemStatsOutBcastPkts_Type = Counter32
_IpSystemStatsOutBcastPkts_Object = MibTableColumn
ipSystemStatsOutBcastPkts = _IpSystemStatsOutBcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 44),
    _IpSystemStatsOutBcastPkts_Type()
)
ipSystemStatsOutBcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsOutBcastPkts.setStatus("current")
_IpSystemStatsHCOutBcastPkts_Type = Counter64
_IpSystemStatsHCOutBcastPkts_Object = MibTableColumn
ipSystemStatsHCOutBcastPkts = _IpSystemStatsHCOutBcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 45),
    _IpSystemStatsHCOutBcastPkts_Type()
)
ipSystemStatsHCOutBcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsHCOutBcastPkts.setStatus("current")
_IpSystemStatsDiscontinuityTime_Type = TimeStamp
_IpSystemStatsDiscontinuityTime_Object = MibTableColumn
ipSystemStatsDiscontinuityTime = _IpSystemStatsDiscontinuityTime_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 46),
    _IpSystemStatsDiscontinuityTime_Type()
)
ipSystemStatsDiscontinuityTime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsDiscontinuityTime.setStatus("current")
_IpSystemStatsRefreshRate_Type = Unsigned32
_IpSystemStatsRefreshRate_Object = MibTableColumn
ipSystemStatsRefreshRate = _IpSystemStatsRefreshRate_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 1, 1, 47),
    _IpSystemStatsRefreshRate_Type()
)
ipSystemStatsRefreshRate.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipSystemStatsRefreshRate.setStatus("current")
if mibBuilder.loadTexts:
    ipSystemStatsRefreshRate.setUnits("milli-seconds")
_IpIfStatsTableLastChange_Type = TimeStamp
_IpIfStatsTableLastChange_Object = MibScalar
ipIfStatsTableLastChange = _IpIfStatsTableLastChange_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 2),
    _IpIfStatsTableLastChange_Type()
)
ipIfStatsTableLastChange.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsTableLastChange.setStatus("current")
_IpIfStatsTable_Object = MibTable
ipIfStatsTable = _IpIfStatsTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3)
)
if mibBuilder.loadTexts:
    ipIfStatsTable.setStatus("current")
_IpIfStatsEntry_Object = MibTableRow
ipIfStatsEntry = _IpIfStatsEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1)
)
ipIfStatsEntry.setIndexNames(
    (0, "IP-MIB", "ipIfStatsIPVersion"),
    (0, "IP-MIB", "ipIfStatsIfIndex"),
)
if mibBuilder.loadTexts:
    ipIfStatsEntry.setStatus("current")
_IpIfStatsIPVersion_Type = InetVersion
_IpIfStatsIPVersion_Object = MibTableColumn
ipIfStatsIPVersion = _IpIfStatsIPVersion_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 1),
    _IpIfStatsIPVersion_Type()
)
ipIfStatsIPVersion.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipIfStatsIPVersion.setStatus("current")
_IpIfStatsIfIndex_Type = InterfaceIndex
_IpIfStatsIfIndex_Object = MibTableColumn
ipIfStatsIfIndex = _IpIfStatsIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 2),
    _IpIfStatsIfIndex_Type()
)
ipIfStatsIfIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipIfStatsIfIndex.setStatus("current")
_IpIfStatsInReceives_Type = Counter32
_IpIfStatsInReceives_Object = MibTableColumn
ipIfStatsInReceives = _IpIfStatsInReceives_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 3),
    _IpIfStatsInReceives_Type()
)
ipIfStatsInReceives.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInReceives.setStatus("current")
_IpIfStatsHCInReceives_Type = Counter64
_IpIfStatsHCInReceives_Object = MibTableColumn
ipIfStatsHCInReceives = _IpIfStatsHCInReceives_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 4),
    _IpIfStatsHCInReceives_Type()
)
ipIfStatsHCInReceives.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCInReceives.setStatus("current")
_IpIfStatsInOctets_Type = Counter32
_IpIfStatsInOctets_Object = MibTableColumn
ipIfStatsInOctets = _IpIfStatsInOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 5),
    _IpIfStatsInOctets_Type()
)
ipIfStatsInOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInOctets.setStatus("current")
_IpIfStatsHCInOctets_Type = Counter64
_IpIfStatsHCInOctets_Object = MibTableColumn
ipIfStatsHCInOctets = _IpIfStatsHCInOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 6),
    _IpIfStatsHCInOctets_Type()
)
ipIfStatsHCInOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCInOctets.setStatus("current")
_IpIfStatsInHdrErrors_Type = Counter32
_IpIfStatsInHdrErrors_Object = MibTableColumn
ipIfStatsInHdrErrors = _IpIfStatsInHdrErrors_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 7),
    _IpIfStatsInHdrErrors_Type()
)
ipIfStatsInHdrErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInHdrErrors.setStatus("current")
_IpIfStatsInNoRoutes_Type = Counter32
_IpIfStatsInNoRoutes_Object = MibTableColumn
ipIfStatsInNoRoutes = _IpIfStatsInNoRoutes_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 8),
    _IpIfStatsInNoRoutes_Type()
)
ipIfStatsInNoRoutes.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInNoRoutes.setStatus("current")
_IpIfStatsInAddrErrors_Type = Counter32
_IpIfStatsInAddrErrors_Object = MibTableColumn
ipIfStatsInAddrErrors = _IpIfStatsInAddrErrors_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 9),
    _IpIfStatsInAddrErrors_Type()
)
ipIfStatsInAddrErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInAddrErrors.setStatus("current")
_IpIfStatsInUnknownProtos_Type = Counter32
_IpIfStatsInUnknownProtos_Object = MibTableColumn
ipIfStatsInUnknownProtos = _IpIfStatsInUnknownProtos_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 10),
    _IpIfStatsInUnknownProtos_Type()
)
ipIfStatsInUnknownProtos.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInUnknownProtos.setStatus("current")
_IpIfStatsInTruncatedPkts_Type = Counter32
_IpIfStatsInTruncatedPkts_Object = MibTableColumn
ipIfStatsInTruncatedPkts = _IpIfStatsInTruncatedPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 11),
    _IpIfStatsInTruncatedPkts_Type()
)
ipIfStatsInTruncatedPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInTruncatedPkts.setStatus("current")
_IpIfStatsInForwDatagrams_Type = Counter32
_IpIfStatsInForwDatagrams_Object = MibTableColumn
ipIfStatsInForwDatagrams = _IpIfStatsInForwDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 12),
    _IpIfStatsInForwDatagrams_Type()
)
ipIfStatsInForwDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInForwDatagrams.setStatus("current")
_IpIfStatsHCInForwDatagrams_Type = Counter64
_IpIfStatsHCInForwDatagrams_Object = MibTableColumn
ipIfStatsHCInForwDatagrams = _IpIfStatsHCInForwDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 13),
    _IpIfStatsHCInForwDatagrams_Type()
)
ipIfStatsHCInForwDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCInForwDatagrams.setStatus("current")
_IpIfStatsReasmReqds_Type = Counter32
_IpIfStatsReasmReqds_Object = MibTableColumn
ipIfStatsReasmReqds = _IpIfStatsReasmReqds_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 14),
    _IpIfStatsReasmReqds_Type()
)
ipIfStatsReasmReqds.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsReasmReqds.setStatus("current")
_IpIfStatsReasmOKs_Type = Counter32
_IpIfStatsReasmOKs_Object = MibTableColumn
ipIfStatsReasmOKs = _IpIfStatsReasmOKs_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 15),
    _IpIfStatsReasmOKs_Type()
)
ipIfStatsReasmOKs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsReasmOKs.setStatus("current")
_IpIfStatsReasmFails_Type = Counter32
_IpIfStatsReasmFails_Object = MibTableColumn
ipIfStatsReasmFails = _IpIfStatsReasmFails_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 16),
    _IpIfStatsReasmFails_Type()
)
ipIfStatsReasmFails.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsReasmFails.setStatus("current")
_IpIfStatsInDiscards_Type = Counter32
_IpIfStatsInDiscards_Object = MibTableColumn
ipIfStatsInDiscards = _IpIfStatsInDiscards_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 17),
    _IpIfStatsInDiscards_Type()
)
ipIfStatsInDiscards.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInDiscards.setStatus("current")
_IpIfStatsInDelivers_Type = Counter32
_IpIfStatsInDelivers_Object = MibTableColumn
ipIfStatsInDelivers = _IpIfStatsInDelivers_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 18),
    _IpIfStatsInDelivers_Type()
)
ipIfStatsInDelivers.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInDelivers.setStatus("current")
_IpIfStatsHCInDelivers_Type = Counter64
_IpIfStatsHCInDelivers_Object = MibTableColumn
ipIfStatsHCInDelivers = _IpIfStatsHCInDelivers_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 19),
    _IpIfStatsHCInDelivers_Type()
)
ipIfStatsHCInDelivers.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCInDelivers.setStatus("current")
_IpIfStatsOutRequests_Type = Counter32
_IpIfStatsOutRequests_Object = MibTableColumn
ipIfStatsOutRequests = _IpIfStatsOutRequests_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 20),
    _IpIfStatsOutRequests_Type()
)
ipIfStatsOutRequests.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutRequests.setStatus("current")
_IpIfStatsHCOutRequests_Type = Counter64
_IpIfStatsHCOutRequests_Object = MibTableColumn
ipIfStatsHCOutRequests = _IpIfStatsHCOutRequests_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 21),
    _IpIfStatsHCOutRequests_Type()
)
ipIfStatsHCOutRequests.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCOutRequests.setStatus("current")
_IpIfStatsOutForwDatagrams_Type = Counter32
_IpIfStatsOutForwDatagrams_Object = MibTableColumn
ipIfStatsOutForwDatagrams = _IpIfStatsOutForwDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 23),
    _IpIfStatsOutForwDatagrams_Type()
)
ipIfStatsOutForwDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutForwDatagrams.setStatus("current")
_IpIfStatsHCOutForwDatagrams_Type = Counter64
_IpIfStatsHCOutForwDatagrams_Object = MibTableColumn
ipIfStatsHCOutForwDatagrams = _IpIfStatsHCOutForwDatagrams_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 24),
    _IpIfStatsHCOutForwDatagrams_Type()
)
ipIfStatsHCOutForwDatagrams.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCOutForwDatagrams.setStatus("current")
_IpIfStatsOutDiscards_Type = Counter32
_IpIfStatsOutDiscards_Object = MibTableColumn
ipIfStatsOutDiscards = _IpIfStatsOutDiscards_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 25),
    _IpIfStatsOutDiscards_Type()
)
ipIfStatsOutDiscards.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutDiscards.setStatus("current")
_IpIfStatsOutFragReqds_Type = Counter32
_IpIfStatsOutFragReqds_Object = MibTableColumn
ipIfStatsOutFragReqds = _IpIfStatsOutFragReqds_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 26),
    _IpIfStatsOutFragReqds_Type()
)
ipIfStatsOutFragReqds.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutFragReqds.setStatus("current")
_IpIfStatsOutFragOKs_Type = Counter32
_IpIfStatsOutFragOKs_Object = MibTableColumn
ipIfStatsOutFragOKs = _IpIfStatsOutFragOKs_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 27),
    _IpIfStatsOutFragOKs_Type()
)
ipIfStatsOutFragOKs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutFragOKs.setStatus("current")
_IpIfStatsOutFragFails_Type = Counter32
_IpIfStatsOutFragFails_Object = MibTableColumn
ipIfStatsOutFragFails = _IpIfStatsOutFragFails_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 28),
    _IpIfStatsOutFragFails_Type()
)
ipIfStatsOutFragFails.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutFragFails.setStatus("current")
_IpIfStatsOutFragCreates_Type = Counter32
_IpIfStatsOutFragCreates_Object = MibTableColumn
ipIfStatsOutFragCreates = _IpIfStatsOutFragCreates_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 29),
    _IpIfStatsOutFragCreates_Type()
)
ipIfStatsOutFragCreates.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutFragCreates.setStatus("current")
_IpIfStatsOutTransmits_Type = Counter32
_IpIfStatsOutTransmits_Object = MibTableColumn
ipIfStatsOutTransmits = _IpIfStatsOutTransmits_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 30),
    _IpIfStatsOutTransmits_Type()
)
ipIfStatsOutTransmits.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutTransmits.setStatus("current")
_IpIfStatsHCOutTransmits_Type = Counter64
_IpIfStatsHCOutTransmits_Object = MibTableColumn
ipIfStatsHCOutTransmits = _IpIfStatsHCOutTransmits_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 31),
    _IpIfStatsHCOutTransmits_Type()
)
ipIfStatsHCOutTransmits.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCOutTransmits.setStatus("current")
_IpIfStatsOutOctets_Type = Counter32
_IpIfStatsOutOctets_Object = MibTableColumn
ipIfStatsOutOctets = _IpIfStatsOutOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 32),
    _IpIfStatsOutOctets_Type()
)
ipIfStatsOutOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutOctets.setStatus("current")
_IpIfStatsHCOutOctets_Type = Counter64
_IpIfStatsHCOutOctets_Object = MibTableColumn
ipIfStatsHCOutOctets = _IpIfStatsHCOutOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 33),
    _IpIfStatsHCOutOctets_Type()
)
ipIfStatsHCOutOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCOutOctets.setStatus("current")
_IpIfStatsInMcastPkts_Type = Counter32
_IpIfStatsInMcastPkts_Object = MibTableColumn
ipIfStatsInMcastPkts = _IpIfStatsInMcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 34),
    _IpIfStatsInMcastPkts_Type()
)
ipIfStatsInMcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInMcastPkts.setStatus("current")
_IpIfStatsHCInMcastPkts_Type = Counter64
_IpIfStatsHCInMcastPkts_Object = MibTableColumn
ipIfStatsHCInMcastPkts = _IpIfStatsHCInMcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 35),
    _IpIfStatsHCInMcastPkts_Type()
)
ipIfStatsHCInMcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCInMcastPkts.setStatus("current")
_IpIfStatsInMcastOctets_Type = Counter32
_IpIfStatsInMcastOctets_Object = MibTableColumn
ipIfStatsInMcastOctets = _IpIfStatsInMcastOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 36),
    _IpIfStatsInMcastOctets_Type()
)
ipIfStatsInMcastOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInMcastOctets.setStatus("current")
_IpIfStatsHCInMcastOctets_Type = Counter64
_IpIfStatsHCInMcastOctets_Object = MibTableColumn
ipIfStatsHCInMcastOctets = _IpIfStatsHCInMcastOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 37),
    _IpIfStatsHCInMcastOctets_Type()
)
ipIfStatsHCInMcastOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCInMcastOctets.setStatus("current")
_IpIfStatsOutMcastPkts_Type = Counter32
_IpIfStatsOutMcastPkts_Object = MibTableColumn
ipIfStatsOutMcastPkts = _IpIfStatsOutMcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 38),
    _IpIfStatsOutMcastPkts_Type()
)
ipIfStatsOutMcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutMcastPkts.setStatus("current")
_IpIfStatsHCOutMcastPkts_Type = Counter64
_IpIfStatsHCOutMcastPkts_Object = MibTableColumn
ipIfStatsHCOutMcastPkts = _IpIfStatsHCOutMcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 39),
    _IpIfStatsHCOutMcastPkts_Type()
)
ipIfStatsHCOutMcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCOutMcastPkts.setStatus("current")
_IpIfStatsOutMcastOctets_Type = Counter32
_IpIfStatsOutMcastOctets_Object = MibTableColumn
ipIfStatsOutMcastOctets = _IpIfStatsOutMcastOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 40),
    _IpIfStatsOutMcastOctets_Type()
)
ipIfStatsOutMcastOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutMcastOctets.setStatus("current")
_IpIfStatsHCOutMcastOctets_Type = Counter64
_IpIfStatsHCOutMcastOctets_Object = MibTableColumn
ipIfStatsHCOutMcastOctets = _IpIfStatsHCOutMcastOctets_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 41),
    _IpIfStatsHCOutMcastOctets_Type()
)
ipIfStatsHCOutMcastOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCOutMcastOctets.setStatus("current")
_IpIfStatsInBcastPkts_Type = Counter32
_IpIfStatsInBcastPkts_Object = MibTableColumn
ipIfStatsInBcastPkts = _IpIfStatsInBcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 42),
    _IpIfStatsInBcastPkts_Type()
)
ipIfStatsInBcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsInBcastPkts.setStatus("current")
_IpIfStatsHCInBcastPkts_Type = Counter64
_IpIfStatsHCInBcastPkts_Object = MibTableColumn
ipIfStatsHCInBcastPkts = _IpIfStatsHCInBcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 43),
    _IpIfStatsHCInBcastPkts_Type()
)
ipIfStatsHCInBcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCInBcastPkts.setStatus("current")
_IpIfStatsOutBcastPkts_Type = Counter32
_IpIfStatsOutBcastPkts_Object = MibTableColumn
ipIfStatsOutBcastPkts = _IpIfStatsOutBcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 44),
    _IpIfStatsOutBcastPkts_Type()
)
ipIfStatsOutBcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsOutBcastPkts.setStatus("current")
_IpIfStatsHCOutBcastPkts_Type = Counter64
_IpIfStatsHCOutBcastPkts_Object = MibTableColumn
ipIfStatsHCOutBcastPkts = _IpIfStatsHCOutBcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 45),
    _IpIfStatsHCOutBcastPkts_Type()
)
ipIfStatsHCOutBcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsHCOutBcastPkts.setStatus("current")
_IpIfStatsDiscontinuityTime_Type = TimeStamp
_IpIfStatsDiscontinuityTime_Object = MibTableColumn
ipIfStatsDiscontinuityTime = _IpIfStatsDiscontinuityTime_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 46),
    _IpIfStatsDiscontinuityTime_Type()
)
ipIfStatsDiscontinuityTime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsDiscontinuityTime.setStatus("current")
_IpIfStatsRefreshRate_Type = Unsigned32
_IpIfStatsRefreshRate_Object = MibTableColumn
ipIfStatsRefreshRate = _IpIfStatsRefreshRate_Object(
    (1, 3, 6, 1, 2, 1, 4, 31, 3, 1, 47),
    _IpIfStatsRefreshRate_Type()
)
ipIfStatsRefreshRate.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipIfStatsRefreshRate.setStatus("current")
if mibBuilder.loadTexts:
    ipIfStatsRefreshRate.setUnits("milli-seconds")
_IpAddressPrefixTable_Object = MibTable
ipAddressPrefixTable = _IpAddressPrefixTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 32)
)
if mibBuilder.loadTexts:
    ipAddressPrefixTable.setStatus("current")
_IpAddressPrefixEntry_Object = MibTableRow
ipAddressPrefixEntry = _IpAddressPrefixEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1)
)
ipAddressPrefixEntry.setIndexNames(
    (0, "IP-MIB", "ipAddressPrefixIfIndex"),
    (0, "IP-MIB", "ipAddressPrefixType"),
    (0, "IP-MIB", "ipAddressPrefixPrefix"),
    (0, "IP-MIB", "ipAddressPrefixLength"),
)
if mibBuilder.loadTexts:
    ipAddressPrefixEntry.setStatus("current")
_IpAddressPrefixIfIndex_Type = InterfaceIndex
_IpAddressPrefixIfIndex_Object = MibTableColumn
ipAddressPrefixIfIndex = _IpAddressPrefixIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1, 1),
    _IpAddressPrefixIfIndex_Type()
)
ipAddressPrefixIfIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipAddressPrefixIfIndex.setStatus("current")
_IpAddressPrefixType_Type = InetAddressType
_IpAddressPrefixType_Object = MibTableColumn
ipAddressPrefixType = _IpAddressPrefixType_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1, 2),
    _IpAddressPrefixType_Type()
)
ipAddressPrefixType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipAddressPrefixType.setStatus("current")
_IpAddressPrefixPrefix_Type = InetAddress
_IpAddressPrefixPrefix_Object = MibTableColumn
ipAddressPrefixPrefix = _IpAddressPrefixPrefix_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1, 3),
    _IpAddressPrefixPrefix_Type()
)
ipAddressPrefixPrefix.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipAddressPrefixPrefix.setStatus("current")
_IpAddressPrefixLength_Type = InetAddressPrefixLength
_IpAddressPrefixLength_Object = MibTableColumn
ipAddressPrefixLength = _IpAddressPrefixLength_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1, 4),
    _IpAddressPrefixLength_Type()
)
ipAddressPrefixLength.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipAddressPrefixLength.setStatus("current")
_IpAddressPrefixOrigin_Type = IpAddressPrefixOriginTC
_IpAddressPrefixOrigin_Object = MibTableColumn
ipAddressPrefixOrigin = _IpAddressPrefixOrigin_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1, 5),
    _IpAddressPrefixOrigin_Type()
)
ipAddressPrefixOrigin.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAddressPrefixOrigin.setStatus("current")
_IpAddressPrefixOnLinkFlag_Type = TruthValue
_IpAddressPrefixOnLinkFlag_Object = MibTableColumn
ipAddressPrefixOnLinkFlag = _IpAddressPrefixOnLinkFlag_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1, 6),
    _IpAddressPrefixOnLinkFlag_Type()
)
ipAddressPrefixOnLinkFlag.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAddressPrefixOnLinkFlag.setStatus("current")
_IpAddressPrefixAutonomousFlag_Type = TruthValue
_IpAddressPrefixAutonomousFlag_Object = MibTableColumn
ipAddressPrefixAutonomousFlag = _IpAddressPrefixAutonomousFlag_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1, 7),
    _IpAddressPrefixAutonomousFlag_Type()
)
ipAddressPrefixAutonomousFlag.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAddressPrefixAutonomousFlag.setStatus("current")
_IpAddressPrefixAdvPreferredLifetime_Type = Unsigned32
_IpAddressPrefixAdvPreferredLifetime_Object = MibTableColumn
ipAddressPrefixAdvPreferredLifetime = _IpAddressPrefixAdvPreferredLifetime_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1, 8),
    _IpAddressPrefixAdvPreferredLifetime_Type()
)
ipAddressPrefixAdvPreferredLifetime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAddressPrefixAdvPreferredLifetime.setStatus("current")
if mibBuilder.loadTexts:
    ipAddressPrefixAdvPreferredLifetime.setUnits("seconds")
_IpAddressPrefixAdvValidLifetime_Type = Unsigned32
_IpAddressPrefixAdvValidLifetime_Object = MibTableColumn
ipAddressPrefixAdvValidLifetime = _IpAddressPrefixAdvValidLifetime_Object(
    (1, 3, 6, 1, 2, 1, 4, 32, 1, 9),
    _IpAddressPrefixAdvValidLifetime_Type()
)
ipAddressPrefixAdvValidLifetime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAddressPrefixAdvValidLifetime.setStatus("current")
if mibBuilder.loadTexts:
    ipAddressPrefixAdvValidLifetime.setUnits("seconds")
_IpAddressSpinLock_Type = TestAndIncr
_IpAddressSpinLock_Object = MibScalar
ipAddressSpinLock = _IpAddressSpinLock_Object(
    (1, 3, 6, 1, 2, 1, 4, 33),
    _IpAddressSpinLock_Type()
)
ipAddressSpinLock.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ipAddressSpinLock.setStatus("current")
_IpAddressTable_Object = MibTable
ipAddressTable = _IpAddressTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 34)
)
if mibBuilder.loadTexts:
    ipAddressTable.setStatus("current")
_IpAddressEntry_Object = MibTableRow
ipAddressEntry = _IpAddressEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1)
)
ipAddressEntry.setIndexNames(
    (0, "IP-MIB", "ipAddressAddrType"),
    (0, "IP-MIB", "ipAddressAddr"),
)
if mibBuilder.loadTexts:
    ipAddressEntry.setStatus("current")
_IpAddressAddrType_Type = InetAddressType
_IpAddressAddrType_Object = MibTableColumn
ipAddressAddrType = _IpAddressAddrType_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 1),
    _IpAddressAddrType_Type()
)
ipAddressAddrType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipAddressAddrType.setStatus("current")
_IpAddressAddr_Type = InetAddress
_IpAddressAddr_Object = MibTableColumn
ipAddressAddr = _IpAddressAddr_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 2),
    _IpAddressAddr_Type()
)
ipAddressAddr.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipAddressAddr.setStatus("current")
_IpAddressIfIndex_Type = InterfaceIndex
_IpAddressIfIndex_Object = MibTableColumn
ipAddressIfIndex = _IpAddressIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 3),
    _IpAddressIfIndex_Type()
)
ipAddressIfIndex.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipAddressIfIndex.setStatus("current")


class _IpAddressType_Type(Integer32):
    """Custom type ipAddressType based on Integer32"""
    defaultValue = 1

    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2,
              3)
        )
    )
    namedValues = NamedValues(
        *(("anycast", 2),
          ("broadcast", 3),
          ("unicast", 1))
    )


_IpAddressType_Type.__name__ = "Integer32"
_IpAddressType_Object = MibTableColumn
ipAddressType = _IpAddressType_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 4),
    _IpAddressType_Type()
)
ipAddressType.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipAddressType.setStatus("current")


class _IpAddressPrefix_Type(RowPointer):
    """Custom type ipAddressPrefix based on RowPointer"""
    defaultValue = "(0, 0)"


_IpAddressPrefix_Object = MibTableColumn
ipAddressPrefix = _IpAddressPrefix_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 5),
    _IpAddressPrefix_Type()
)
ipAddressPrefix.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAddressPrefix.setStatus("current")
_IpAddressOrigin_Type = IpAddressOriginTC
_IpAddressOrigin_Object = MibTableColumn
ipAddressOrigin = _IpAddressOrigin_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 6),
    _IpAddressOrigin_Type()
)
ipAddressOrigin.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAddressOrigin.setStatus("current")


class _IpAddressStatus_Type(IpAddressStatusTC):
    """Custom type ipAddressStatus based on IpAddressStatusTC"""


_IpAddressStatus_Object = MibTableColumn
ipAddressStatus = _IpAddressStatus_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 7),
    _IpAddressStatus_Type()
)
ipAddressStatus.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipAddressStatus.setStatus("current")
_IpAddressCreated_Type = TimeStamp
_IpAddressCreated_Object = MibTableColumn
ipAddressCreated = _IpAddressCreated_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 8),
    _IpAddressCreated_Type()
)
ipAddressCreated.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAddressCreated.setStatus("current")
_IpAddressLastChanged_Type = TimeStamp
_IpAddressLastChanged_Object = MibTableColumn
ipAddressLastChanged = _IpAddressLastChanged_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 9),
    _IpAddressLastChanged_Type()
)
ipAddressLastChanged.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipAddressLastChanged.setStatus("current")
_IpAddressRowStatus_Type = RowStatus
_IpAddressRowStatus_Object = MibTableColumn
ipAddressRowStatus = _IpAddressRowStatus_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 10),
    _IpAddressRowStatus_Type()
)
ipAddressRowStatus.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipAddressRowStatus.setStatus("current")


class _IpAddressStorageType_Type(StorageType):
    """Custom type ipAddressStorageType based on StorageType"""


_IpAddressStorageType_Object = MibTableColumn
ipAddressStorageType = _IpAddressStorageType_Object(
    (1, 3, 6, 1, 2, 1, 4, 34, 1, 11),
    _IpAddressStorageType_Type()
)
ipAddressStorageType.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipAddressStorageType.setStatus("current")
_IpNetToPhysicalTable_Object = MibTable
ipNetToPhysicalTable = _IpNetToPhysicalTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 35)
)
if mibBuilder.loadTexts:
    ipNetToPhysicalTable.setStatus("current")
_IpNetToPhysicalEntry_Object = MibTableRow
ipNetToPhysicalEntry = _IpNetToPhysicalEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 35, 1)
)
ipNetToPhysicalEntry.setIndexNames(
    (0, "IP-MIB", "ipNetToPhysicalIfIndex"),
    (0, "IP-MIB", "ipNetToPhysicalNetAddressType"),
    (0, "IP-MIB", "ipNetToPhysicalNetAddress"),
)
if mibBuilder.loadTexts:
    ipNetToPhysicalEntry.setStatus("current")
_IpNetToPhysicalIfIndex_Type = InterfaceIndex
_IpNetToPhysicalIfIndex_Object = MibTableColumn
ipNetToPhysicalIfIndex = _IpNetToPhysicalIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 35, 1, 1),
    _IpNetToPhysicalIfIndex_Type()
)
ipNetToPhysicalIfIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipNetToPhysicalIfIndex.setStatus("current")
_IpNetToPhysicalNetAddressType_Type = InetAddressType
_IpNetToPhysicalNetAddressType_Object = MibTableColumn
ipNetToPhysicalNetAddressType = _IpNetToPhysicalNetAddressType_Object(
    (1, 3, 6, 1, 2, 1, 4, 35, 1, 2),
    _IpNetToPhysicalNetAddressType_Type()
)
ipNetToPhysicalNetAddressType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipNetToPhysicalNetAddressType.setStatus("current")
_IpNetToPhysicalNetAddress_Type = InetAddress
_IpNetToPhysicalNetAddress_Object = MibTableColumn
ipNetToPhysicalNetAddress = _IpNetToPhysicalNetAddress_Object(
    (1, 3, 6, 1, 2, 1, 4, 35, 1, 3),
    _IpNetToPhysicalNetAddress_Type()
)
ipNetToPhysicalNetAddress.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipNetToPhysicalNetAddress.setStatus("current")


class _IpNetToPhysicalPhysAddress_Type(PhysAddress):
    """Custom type ipNetToPhysicalPhysAddress based on PhysAddress"""
    subtypeSpec = PhysAddress.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 65535),
    )


_IpNetToPhysicalPhysAddress_Type.__name__ = "PhysAddress"
_IpNetToPhysicalPhysAddress_Object = MibTableColumn
ipNetToPhysicalPhysAddress = _IpNetToPhysicalPhysAddress_Object(
    (1, 3, 6, 1, 2, 1, 4, 35, 1, 4),
    _IpNetToPhysicalPhysAddress_Type()
)
ipNetToPhysicalPhysAddress.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipNetToPhysicalPhysAddress.setStatus("current")
_IpNetToPhysicalLastUpdated_Type = TimeStamp
_IpNetToPhysicalLastUpdated_Object = MibTableColumn
ipNetToPhysicalLastUpdated = _IpNetToPhysicalLastUpdated_Object(
    (1, 3, 6, 1, 2, 1, 4, 35, 1, 5),
    _IpNetToPhysicalLastUpdated_Type()
)
ipNetToPhysicalLastUpdated.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipNetToPhysicalLastUpdated.setStatus("current")


class _IpNetToPhysicalType_Type(Integer32):
    """Custom type ipNetToPhysicalType based on Integer32"""
    defaultValue = 4

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
        *(("dynamic", 3),
          ("invalid", 2),
          ("local", 5),
          ("other", 1),
          ("static", 4))
    )


_IpNetToPhysicalType_Type.__name__ = "Integer32"
_IpNetToPhysicalType_Object = MibTableColumn
ipNetToPhysicalType = _IpNetToPhysicalType_Object(
    (1, 3, 6, 1, 2, 1, 4, 35, 1, 6),
    _IpNetToPhysicalType_Type()
)
ipNetToPhysicalType.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipNetToPhysicalType.setStatus("current")


class _IpNetToPhysicalState_Type(Integer32):
    """Custom type ipNetToPhysicalState based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2,
              3,
              4,
              5,
              6,
              7)
        )
    )
    namedValues = NamedValues(
        *(("delay", 3),
          ("incomplete", 7),
          ("invalid", 5),
          ("probe", 4),
          ("reachable", 1),
          ("stale", 2),
          ("unknown", 6))
    )


_IpNetToPhysicalState_Type.__name__ = "Integer32"
_IpNetToPhysicalState_Object = MibTableColumn
ipNetToPhysicalState = _IpNetToPhysicalState_Object(
    (1, 3, 6, 1, 2, 1, 4, 35, 1, 7),
    _IpNetToPhysicalState_Type()
)
ipNetToPhysicalState.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipNetToPhysicalState.setStatus("current")
_IpNetToPhysicalRowStatus_Type = RowStatus
_IpNetToPhysicalRowStatus_Object = MibTableColumn
ipNetToPhysicalRowStatus = _IpNetToPhysicalRowStatus_Object(
    (1, 3, 6, 1, 2, 1, 4, 35, 1, 8),
    _IpNetToPhysicalRowStatus_Type()
)
ipNetToPhysicalRowStatus.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipNetToPhysicalRowStatus.setStatus("current")
_Ipv6ScopeZoneIndexTable_Object = MibTable
ipv6ScopeZoneIndexTable = _Ipv6ScopeZoneIndexTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 36)
)
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexTable.setStatus("current")
_Ipv6ScopeZoneIndexEntry_Object = MibTableRow
ipv6ScopeZoneIndexEntry = _Ipv6ScopeZoneIndexEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1)
)
ipv6ScopeZoneIndexEntry.setIndexNames(
    (0, "IP-MIB", "ipv6ScopeZoneIndexIfIndex"),
)
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexEntry.setStatus("current")
_Ipv6ScopeZoneIndexIfIndex_Type = InterfaceIndex
_Ipv6ScopeZoneIndexIfIndex_Object = MibTableColumn
ipv6ScopeZoneIndexIfIndex = _Ipv6ScopeZoneIndexIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 1),
    _Ipv6ScopeZoneIndexIfIndex_Type()
)
ipv6ScopeZoneIndexIfIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexIfIndex.setStatus("current")
_Ipv6ScopeZoneIndexLinkLocal_Type = InetZoneIndex
_Ipv6ScopeZoneIndexLinkLocal_Object = MibTableColumn
ipv6ScopeZoneIndexLinkLocal = _Ipv6ScopeZoneIndexLinkLocal_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 2),
    _Ipv6ScopeZoneIndexLinkLocal_Type()
)
ipv6ScopeZoneIndexLinkLocal.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexLinkLocal.setStatus("current")
_Ipv6ScopeZoneIndex3_Type = InetZoneIndex
_Ipv6ScopeZoneIndex3_Object = MibTableColumn
ipv6ScopeZoneIndex3 = _Ipv6ScopeZoneIndex3_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 3),
    _Ipv6ScopeZoneIndex3_Type()
)
ipv6ScopeZoneIndex3.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndex3.setStatus("current")
_Ipv6ScopeZoneIndexAdminLocal_Type = InetZoneIndex
_Ipv6ScopeZoneIndexAdminLocal_Object = MibTableColumn
ipv6ScopeZoneIndexAdminLocal = _Ipv6ScopeZoneIndexAdminLocal_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 4),
    _Ipv6ScopeZoneIndexAdminLocal_Type()
)
ipv6ScopeZoneIndexAdminLocal.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexAdminLocal.setStatus("current")
_Ipv6ScopeZoneIndexSiteLocal_Type = InetZoneIndex
_Ipv6ScopeZoneIndexSiteLocal_Object = MibTableColumn
ipv6ScopeZoneIndexSiteLocal = _Ipv6ScopeZoneIndexSiteLocal_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 5),
    _Ipv6ScopeZoneIndexSiteLocal_Type()
)
ipv6ScopeZoneIndexSiteLocal.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexSiteLocal.setStatus("current")
_Ipv6ScopeZoneIndex6_Type = InetZoneIndex
_Ipv6ScopeZoneIndex6_Object = MibTableColumn
ipv6ScopeZoneIndex6 = _Ipv6ScopeZoneIndex6_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 6),
    _Ipv6ScopeZoneIndex6_Type()
)
ipv6ScopeZoneIndex6.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndex6.setStatus("current")
_Ipv6ScopeZoneIndex7_Type = InetZoneIndex
_Ipv6ScopeZoneIndex7_Object = MibTableColumn
ipv6ScopeZoneIndex7 = _Ipv6ScopeZoneIndex7_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 7),
    _Ipv6ScopeZoneIndex7_Type()
)
ipv6ScopeZoneIndex7.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndex7.setStatus("current")
_Ipv6ScopeZoneIndexOrganizationLocal_Type = InetZoneIndex
_Ipv6ScopeZoneIndexOrganizationLocal_Object = MibTableColumn
ipv6ScopeZoneIndexOrganizationLocal = _Ipv6ScopeZoneIndexOrganizationLocal_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 8),
    _Ipv6ScopeZoneIndexOrganizationLocal_Type()
)
ipv6ScopeZoneIndexOrganizationLocal.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexOrganizationLocal.setStatus("current")
_Ipv6ScopeZoneIndex9_Type = InetZoneIndex
_Ipv6ScopeZoneIndex9_Object = MibTableColumn
ipv6ScopeZoneIndex9 = _Ipv6ScopeZoneIndex9_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 9),
    _Ipv6ScopeZoneIndex9_Type()
)
ipv6ScopeZoneIndex9.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndex9.setStatus("current")
_Ipv6ScopeZoneIndexA_Type = InetZoneIndex
_Ipv6ScopeZoneIndexA_Object = MibTableColumn
ipv6ScopeZoneIndexA = _Ipv6ScopeZoneIndexA_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 10),
    _Ipv6ScopeZoneIndexA_Type()
)
ipv6ScopeZoneIndexA.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexA.setStatus("current")
_Ipv6ScopeZoneIndexB_Type = InetZoneIndex
_Ipv6ScopeZoneIndexB_Object = MibTableColumn
ipv6ScopeZoneIndexB = _Ipv6ScopeZoneIndexB_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 11),
    _Ipv6ScopeZoneIndexB_Type()
)
ipv6ScopeZoneIndexB.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexB.setStatus("current")
_Ipv6ScopeZoneIndexC_Type = InetZoneIndex
_Ipv6ScopeZoneIndexC_Object = MibTableColumn
ipv6ScopeZoneIndexC = _Ipv6ScopeZoneIndexC_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 12),
    _Ipv6ScopeZoneIndexC_Type()
)
ipv6ScopeZoneIndexC.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexC.setStatus("current")
_Ipv6ScopeZoneIndexD_Type = InetZoneIndex
_Ipv6ScopeZoneIndexD_Object = MibTableColumn
ipv6ScopeZoneIndexD = _Ipv6ScopeZoneIndexD_Object(
    (1, 3, 6, 1, 2, 1, 4, 36, 1, 13),
    _Ipv6ScopeZoneIndexD_Type()
)
ipv6ScopeZoneIndexD.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipv6ScopeZoneIndexD.setStatus("current")
_IpDefaultRouterTable_Object = MibTable
ipDefaultRouterTable = _IpDefaultRouterTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 37)
)
if mibBuilder.loadTexts:
    ipDefaultRouterTable.setStatus("current")
_IpDefaultRouterEntry_Object = MibTableRow
ipDefaultRouterEntry = _IpDefaultRouterEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 37, 1)
)
ipDefaultRouterEntry.setIndexNames(
    (0, "IP-MIB", "ipDefaultRouterAddressType"),
    (0, "IP-MIB", "ipDefaultRouterAddress"),
    (0, "IP-MIB", "ipDefaultRouterIfIndex"),
)
if mibBuilder.loadTexts:
    ipDefaultRouterEntry.setStatus("current")
_IpDefaultRouterAddressType_Type = InetAddressType
_IpDefaultRouterAddressType_Object = MibTableColumn
ipDefaultRouterAddressType = _IpDefaultRouterAddressType_Object(
    (1, 3, 6, 1, 2, 1, 4, 37, 1, 1),
    _IpDefaultRouterAddressType_Type()
)
ipDefaultRouterAddressType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipDefaultRouterAddressType.setStatus("current")
_IpDefaultRouterAddress_Type = InetAddress
_IpDefaultRouterAddress_Object = MibTableColumn
ipDefaultRouterAddress = _IpDefaultRouterAddress_Object(
    (1, 3, 6, 1, 2, 1, 4, 37, 1, 2),
    _IpDefaultRouterAddress_Type()
)
ipDefaultRouterAddress.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipDefaultRouterAddress.setStatus("current")
_IpDefaultRouterIfIndex_Type = InterfaceIndex
_IpDefaultRouterIfIndex_Object = MibTableColumn
ipDefaultRouterIfIndex = _IpDefaultRouterIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 37, 1, 3),
    _IpDefaultRouterIfIndex_Type()
)
ipDefaultRouterIfIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipDefaultRouterIfIndex.setStatus("current")


class _IpDefaultRouterLifetime_Type(Unsigned32):
    """Custom type ipDefaultRouterLifetime based on Unsigned32"""
    subtypeSpec = Unsigned32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 65535),
    )


_IpDefaultRouterLifetime_Type.__name__ = "Unsigned32"
_IpDefaultRouterLifetime_Object = MibTableColumn
ipDefaultRouterLifetime = _IpDefaultRouterLifetime_Object(
    (1, 3, 6, 1, 2, 1, 4, 37, 1, 4),
    _IpDefaultRouterLifetime_Type()
)
ipDefaultRouterLifetime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipDefaultRouterLifetime.setStatus("current")
if mibBuilder.loadTexts:
    ipDefaultRouterLifetime.setUnits("seconds")


class _IpDefaultRouterPreference_Type(Integer32):
    """Custom type ipDefaultRouterPreference based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(-2,
              -1,
              0,
              1)
        )
    )
    namedValues = NamedValues(
        *(("high", 1),
          ("low", -1),
          ("medium", 0),
          ("reserved", -2))
    )


_IpDefaultRouterPreference_Type.__name__ = "Integer32"
_IpDefaultRouterPreference_Object = MibTableColumn
ipDefaultRouterPreference = _IpDefaultRouterPreference_Object(
    (1, 3, 6, 1, 2, 1, 4, 37, 1, 5),
    _IpDefaultRouterPreference_Type()
)
ipDefaultRouterPreference.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ipDefaultRouterPreference.setStatus("current")
_Ipv6RouterAdvertSpinLock_Type = TestAndIncr
_Ipv6RouterAdvertSpinLock_Object = MibScalar
ipv6RouterAdvertSpinLock = _Ipv6RouterAdvertSpinLock_Object(
    (1, 3, 6, 1, 2, 1, 4, 38),
    _Ipv6RouterAdvertSpinLock_Type()
)
ipv6RouterAdvertSpinLock.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ipv6RouterAdvertSpinLock.setStatus("current")
_Ipv6RouterAdvertTable_Object = MibTable
ipv6RouterAdvertTable = _Ipv6RouterAdvertTable_Object(
    (1, 3, 6, 1, 2, 1, 4, 39)
)
if mibBuilder.loadTexts:
    ipv6RouterAdvertTable.setStatus("current")
_Ipv6RouterAdvertEntry_Object = MibTableRow
ipv6RouterAdvertEntry = _Ipv6RouterAdvertEntry_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1)
)
ipv6RouterAdvertEntry.setIndexNames(
    (0, "IP-MIB", "ipv6RouterAdvertIfIndex"),
)
if mibBuilder.loadTexts:
    ipv6RouterAdvertEntry.setStatus("current")
_Ipv6RouterAdvertIfIndex_Type = InterfaceIndex
_Ipv6RouterAdvertIfIndex_Object = MibTableColumn
ipv6RouterAdvertIfIndex = _Ipv6RouterAdvertIfIndex_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 1),
    _Ipv6RouterAdvertIfIndex_Type()
)
ipv6RouterAdvertIfIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ipv6RouterAdvertIfIndex.setStatus("current")


class _Ipv6RouterAdvertSendAdverts_Type(TruthValue):
    """Custom type ipv6RouterAdvertSendAdverts based on TruthValue"""


_Ipv6RouterAdvertSendAdverts_Object = MibTableColumn
ipv6RouterAdvertSendAdverts = _Ipv6RouterAdvertSendAdverts_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 2),
    _Ipv6RouterAdvertSendAdverts_Type()
)
ipv6RouterAdvertSendAdverts.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertSendAdverts.setStatus("current")


class _Ipv6RouterAdvertMaxInterval_Type(Unsigned32):
    """Custom type ipv6RouterAdvertMaxInterval based on Unsigned32"""
    defaultValue = 600

    subtypeSpec = Unsigned32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(4, 1800),
    )


_Ipv6RouterAdvertMaxInterval_Type.__name__ = "Unsigned32"
_Ipv6RouterAdvertMaxInterval_Object = MibTableColumn
ipv6RouterAdvertMaxInterval = _Ipv6RouterAdvertMaxInterval_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 3),
    _Ipv6RouterAdvertMaxInterval_Type()
)
ipv6RouterAdvertMaxInterval.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertMaxInterval.setStatus("current")
if mibBuilder.loadTexts:
    ipv6RouterAdvertMaxInterval.setUnits("seconds")


class _Ipv6RouterAdvertMinInterval_Type(Unsigned32):
    """Custom type ipv6RouterAdvertMinInterval based on Unsigned32"""
    subtypeSpec = Unsigned32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(3, 1350),
    )


_Ipv6RouterAdvertMinInterval_Type.__name__ = "Unsigned32"
_Ipv6RouterAdvertMinInterval_Object = MibTableColumn
ipv6RouterAdvertMinInterval = _Ipv6RouterAdvertMinInterval_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 4),
    _Ipv6RouterAdvertMinInterval_Type()
)
ipv6RouterAdvertMinInterval.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertMinInterval.setStatus("current")
if mibBuilder.loadTexts:
    ipv6RouterAdvertMinInterval.setUnits("seconds")


class _Ipv6RouterAdvertManagedFlag_Type(TruthValue):
    """Custom type ipv6RouterAdvertManagedFlag based on TruthValue"""


_Ipv6RouterAdvertManagedFlag_Object = MibTableColumn
ipv6RouterAdvertManagedFlag = _Ipv6RouterAdvertManagedFlag_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 5),
    _Ipv6RouterAdvertManagedFlag_Type()
)
ipv6RouterAdvertManagedFlag.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertManagedFlag.setStatus("current")


class _Ipv6RouterAdvertOtherConfigFlag_Type(TruthValue):
    """Custom type ipv6RouterAdvertOtherConfigFlag based on TruthValue"""


_Ipv6RouterAdvertOtherConfigFlag_Object = MibTableColumn
ipv6RouterAdvertOtherConfigFlag = _Ipv6RouterAdvertOtherConfigFlag_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 6),
    _Ipv6RouterAdvertOtherConfigFlag_Type()
)
ipv6RouterAdvertOtherConfigFlag.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertOtherConfigFlag.setStatus("current")
_Ipv6RouterAdvertLinkMTU_Type = Unsigned32
_Ipv6RouterAdvertLinkMTU_Object = MibTableColumn
ipv6RouterAdvertLinkMTU = _Ipv6RouterAdvertLinkMTU_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 7),
    _Ipv6RouterAdvertLinkMTU_Type()
)
ipv6RouterAdvertLinkMTU.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertLinkMTU.setStatus("current")


class _Ipv6RouterAdvertReachableTime_Type(Unsigned32):
    """Custom type ipv6RouterAdvertReachableTime based on Unsigned32"""
    subtypeSpec = Unsigned32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 3600000),
    )


_Ipv6RouterAdvertReachableTime_Type.__name__ = "Unsigned32"
_Ipv6RouterAdvertReachableTime_Object = MibTableColumn
ipv6RouterAdvertReachableTime = _Ipv6RouterAdvertReachableTime_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 8),
    _Ipv6RouterAdvertReachableTime_Type()
)
ipv6RouterAdvertReachableTime.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertReachableTime.setStatus("current")
if mibBuilder.loadTexts:
    ipv6RouterAdvertReachableTime.setUnits("milliseconds")
_Ipv6RouterAdvertRetransmitTime_Type = Unsigned32
_Ipv6RouterAdvertRetransmitTime_Object = MibTableColumn
ipv6RouterAdvertRetransmitTime = _Ipv6RouterAdvertRetransmitTime_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 9),
    _Ipv6RouterAdvertRetransmitTime_Type()
)
ipv6RouterAdvertRetransmitTime.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertRetransmitTime.setStatus("current")
if mibBuilder.loadTexts:
    ipv6RouterAdvertRetransmitTime.setUnits("milliseconds")


class _Ipv6RouterAdvertCurHopLimit_Type(Unsigned32):
    """Custom type ipv6RouterAdvertCurHopLimit based on Unsigned32"""
    subtypeSpec = Unsigned32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 255),
    )


_Ipv6RouterAdvertCurHopLimit_Type.__name__ = "Unsigned32"
_Ipv6RouterAdvertCurHopLimit_Object = MibTableColumn
ipv6RouterAdvertCurHopLimit = _Ipv6RouterAdvertCurHopLimit_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 10),
    _Ipv6RouterAdvertCurHopLimit_Type()
)
ipv6RouterAdvertCurHopLimit.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertCurHopLimit.setStatus("current")


class _Ipv6RouterAdvertDefaultLifetime_Type(Unsigned32):
    """Custom type ipv6RouterAdvertDefaultLifetime based on Unsigned32"""
    subtypeSpec = Unsigned32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 0),
        ValueRangeConstraint(4, 9000),
    )


_Ipv6RouterAdvertDefaultLifetime_Type.__name__ = "Unsigned32"
_Ipv6RouterAdvertDefaultLifetime_Object = MibTableColumn
ipv6RouterAdvertDefaultLifetime = _Ipv6RouterAdvertDefaultLifetime_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 11),
    _Ipv6RouterAdvertDefaultLifetime_Type()
)
ipv6RouterAdvertDefaultLifetime.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertDefaultLifetime.setStatus("current")
if mibBuilder.loadTexts:
    ipv6RouterAdvertDefaultLifetime.setUnits("seconds")
_Ipv6RouterAdvertRowStatus_Type = RowStatus
_Ipv6RouterAdvertRowStatus_Object = MibTableColumn
ipv6RouterAdvertRowStatus = _Ipv6RouterAdvertRowStatus_Object(
    (1, 3, 6, 1, 2, 1, 4, 39, 1, 12),
    _Ipv6RouterAdvertRowStatus_Type()
)
ipv6RouterAdvertRowStatus.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ipv6RouterAdvertRowStatus.setStatus("current")
_Icmp_ObjectIdentity = ObjectIdentity
icmp = _Icmp_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 5)
)
_IcmpInMsgs_Type = Counter32
_IcmpInMsgs_Object = MibScalar
icmpInMsgs = _IcmpInMsgs_Object(
    (1, 3, 6, 1, 2, 1, 5, 1),
    _IcmpInMsgs_Type()
)
icmpInMsgs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInMsgs.setStatus("deprecated")
_IcmpInErrors_Type = Counter32
_IcmpInErrors_Object = MibScalar
icmpInErrors = _IcmpInErrors_Object(
    (1, 3, 6, 1, 2, 1, 5, 2),
    _IcmpInErrors_Type()
)
icmpInErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInErrors.setStatus("deprecated")
_IcmpInDestUnreachs_Type = Counter32
_IcmpInDestUnreachs_Object = MibScalar
icmpInDestUnreachs = _IcmpInDestUnreachs_Object(
    (1, 3, 6, 1, 2, 1, 5, 3),
    _IcmpInDestUnreachs_Type()
)
icmpInDestUnreachs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInDestUnreachs.setStatus("deprecated")
_IcmpInTimeExcds_Type = Counter32
_IcmpInTimeExcds_Object = MibScalar
icmpInTimeExcds = _IcmpInTimeExcds_Object(
    (1, 3, 6, 1, 2, 1, 5, 4),
    _IcmpInTimeExcds_Type()
)
icmpInTimeExcds.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInTimeExcds.setStatus("deprecated")
_IcmpInParmProbs_Type = Counter32
_IcmpInParmProbs_Object = MibScalar
icmpInParmProbs = _IcmpInParmProbs_Object(
    (1, 3, 6, 1, 2, 1, 5, 5),
    _IcmpInParmProbs_Type()
)
icmpInParmProbs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInParmProbs.setStatus("deprecated")
_IcmpInSrcQuenchs_Type = Counter32
_IcmpInSrcQuenchs_Object = MibScalar
icmpInSrcQuenchs = _IcmpInSrcQuenchs_Object(
    (1, 3, 6, 1, 2, 1, 5, 6),
    _IcmpInSrcQuenchs_Type()
)
icmpInSrcQuenchs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInSrcQuenchs.setStatus("deprecated")
_IcmpInRedirects_Type = Counter32
_IcmpInRedirects_Object = MibScalar
icmpInRedirects = _IcmpInRedirects_Object(
    (1, 3, 6, 1, 2, 1, 5, 7),
    _IcmpInRedirects_Type()
)
icmpInRedirects.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInRedirects.setStatus("deprecated")
_IcmpInEchos_Type = Counter32
_IcmpInEchos_Object = MibScalar
icmpInEchos = _IcmpInEchos_Object(
    (1, 3, 6, 1, 2, 1, 5, 8),
    _IcmpInEchos_Type()
)
icmpInEchos.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInEchos.setStatus("deprecated")
_IcmpInEchoReps_Type = Counter32
_IcmpInEchoReps_Object = MibScalar
icmpInEchoReps = _IcmpInEchoReps_Object(
    (1, 3, 6, 1, 2, 1, 5, 9),
    _IcmpInEchoReps_Type()
)
icmpInEchoReps.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInEchoReps.setStatus("deprecated")
_IcmpInTimestamps_Type = Counter32
_IcmpInTimestamps_Object = MibScalar
icmpInTimestamps = _IcmpInTimestamps_Object(
    (1, 3, 6, 1, 2, 1, 5, 10),
    _IcmpInTimestamps_Type()
)
icmpInTimestamps.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInTimestamps.setStatus("deprecated")
_IcmpInTimestampReps_Type = Counter32
_IcmpInTimestampReps_Object = MibScalar
icmpInTimestampReps = _IcmpInTimestampReps_Object(
    (1, 3, 6, 1, 2, 1, 5, 11),
    _IcmpInTimestampReps_Type()
)
icmpInTimestampReps.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInTimestampReps.setStatus("deprecated")
_IcmpInAddrMasks_Type = Counter32
_IcmpInAddrMasks_Object = MibScalar
icmpInAddrMasks = _IcmpInAddrMasks_Object(
    (1, 3, 6, 1, 2, 1, 5, 12),
    _IcmpInAddrMasks_Type()
)
icmpInAddrMasks.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInAddrMasks.setStatus("deprecated")
_IcmpInAddrMaskReps_Type = Counter32
_IcmpInAddrMaskReps_Object = MibScalar
icmpInAddrMaskReps = _IcmpInAddrMaskReps_Object(
    (1, 3, 6, 1, 2, 1, 5, 13),
    _IcmpInAddrMaskReps_Type()
)
icmpInAddrMaskReps.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpInAddrMaskReps.setStatus("deprecated")
_IcmpOutMsgs_Type = Counter32
_IcmpOutMsgs_Object = MibScalar
icmpOutMsgs = _IcmpOutMsgs_Object(
    (1, 3, 6, 1, 2, 1, 5, 14),
    _IcmpOutMsgs_Type()
)
icmpOutMsgs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutMsgs.setStatus("deprecated")
_IcmpOutErrors_Type = Counter32
_IcmpOutErrors_Object = MibScalar
icmpOutErrors = _IcmpOutErrors_Object(
    (1, 3, 6, 1, 2, 1, 5, 15),
    _IcmpOutErrors_Type()
)
icmpOutErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutErrors.setStatus("deprecated")
_IcmpOutDestUnreachs_Type = Counter32
_IcmpOutDestUnreachs_Object = MibScalar
icmpOutDestUnreachs = _IcmpOutDestUnreachs_Object(
    (1, 3, 6, 1, 2, 1, 5, 16),
    _IcmpOutDestUnreachs_Type()
)
icmpOutDestUnreachs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutDestUnreachs.setStatus("deprecated")
_IcmpOutTimeExcds_Type = Counter32
_IcmpOutTimeExcds_Object = MibScalar
icmpOutTimeExcds = _IcmpOutTimeExcds_Object(
    (1, 3, 6, 1, 2, 1, 5, 17),
    _IcmpOutTimeExcds_Type()
)
icmpOutTimeExcds.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutTimeExcds.setStatus("deprecated")
_IcmpOutParmProbs_Type = Counter32
_IcmpOutParmProbs_Object = MibScalar
icmpOutParmProbs = _IcmpOutParmProbs_Object(
    (1, 3, 6, 1, 2, 1, 5, 18),
    _IcmpOutParmProbs_Type()
)
icmpOutParmProbs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutParmProbs.setStatus("deprecated")
_IcmpOutSrcQuenchs_Type = Counter32
_IcmpOutSrcQuenchs_Object = MibScalar
icmpOutSrcQuenchs = _IcmpOutSrcQuenchs_Object(
    (1, 3, 6, 1, 2, 1, 5, 19),
    _IcmpOutSrcQuenchs_Type()
)
icmpOutSrcQuenchs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutSrcQuenchs.setStatus("deprecated")
_IcmpOutRedirects_Type = Counter32
_IcmpOutRedirects_Object = MibScalar
icmpOutRedirects = _IcmpOutRedirects_Object(
    (1, 3, 6, 1, 2, 1, 5, 20),
    _IcmpOutRedirects_Type()
)
icmpOutRedirects.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutRedirects.setStatus("deprecated")
_IcmpOutEchos_Type = Counter32
_IcmpOutEchos_Object = MibScalar
icmpOutEchos = _IcmpOutEchos_Object(
    (1, 3, 6, 1, 2, 1, 5, 21),
    _IcmpOutEchos_Type()
)
icmpOutEchos.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutEchos.setStatus("deprecated")
_IcmpOutEchoReps_Type = Counter32
_IcmpOutEchoReps_Object = MibScalar
icmpOutEchoReps = _IcmpOutEchoReps_Object(
    (1, 3, 6, 1, 2, 1, 5, 22),
    _IcmpOutEchoReps_Type()
)
icmpOutEchoReps.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutEchoReps.setStatus("deprecated")
_IcmpOutTimestamps_Type = Counter32
_IcmpOutTimestamps_Object = MibScalar
icmpOutTimestamps = _IcmpOutTimestamps_Object(
    (1, 3, 6, 1, 2, 1, 5, 23),
    _IcmpOutTimestamps_Type()
)
icmpOutTimestamps.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutTimestamps.setStatus("deprecated")
_IcmpOutTimestampReps_Type = Counter32
_IcmpOutTimestampReps_Object = MibScalar
icmpOutTimestampReps = _IcmpOutTimestampReps_Object(
    (1, 3, 6, 1, 2, 1, 5, 24),
    _IcmpOutTimestampReps_Type()
)
icmpOutTimestampReps.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutTimestampReps.setStatus("deprecated")
_IcmpOutAddrMasks_Type = Counter32
_IcmpOutAddrMasks_Object = MibScalar
icmpOutAddrMasks = _IcmpOutAddrMasks_Object(
    (1, 3, 6, 1, 2, 1, 5, 25),
    _IcmpOutAddrMasks_Type()
)
icmpOutAddrMasks.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutAddrMasks.setStatus("deprecated")
_IcmpOutAddrMaskReps_Type = Counter32
_IcmpOutAddrMaskReps_Object = MibScalar
icmpOutAddrMaskReps = _IcmpOutAddrMaskReps_Object(
    (1, 3, 6, 1, 2, 1, 5, 26),
    _IcmpOutAddrMaskReps_Type()
)
icmpOutAddrMaskReps.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpOutAddrMaskReps.setStatus("deprecated")
_IcmpStatsTable_Object = MibTable
icmpStatsTable = _IcmpStatsTable_Object(
    (1, 3, 6, 1, 2, 1, 5, 29)
)
if mibBuilder.loadTexts:
    icmpStatsTable.setStatus("current")
_IcmpStatsEntry_Object = MibTableRow
icmpStatsEntry = _IcmpStatsEntry_Object(
    (1, 3, 6, 1, 2, 1, 5, 29, 1)
)
icmpStatsEntry.setIndexNames(
    (0, "IP-MIB", "icmpStatsIPVersion"),
)
if mibBuilder.loadTexts:
    icmpStatsEntry.setStatus("current")
_IcmpStatsIPVersion_Type = InetVersion
_IcmpStatsIPVersion_Object = MibTableColumn
icmpStatsIPVersion = _IcmpStatsIPVersion_Object(
    (1, 3, 6, 1, 2, 1, 5, 29, 1, 1),
    _IcmpStatsIPVersion_Type()
)
icmpStatsIPVersion.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    icmpStatsIPVersion.setStatus("current")
_IcmpStatsInMsgs_Type = Counter32
_IcmpStatsInMsgs_Object = MibTableColumn
icmpStatsInMsgs = _IcmpStatsInMsgs_Object(
    (1, 3, 6, 1, 2, 1, 5, 29, 1, 2),
    _IcmpStatsInMsgs_Type()
)
icmpStatsInMsgs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpStatsInMsgs.setStatus("current")
_IcmpStatsInErrors_Type = Counter32
_IcmpStatsInErrors_Object = MibTableColumn
icmpStatsInErrors = _IcmpStatsInErrors_Object(
    (1, 3, 6, 1, 2, 1, 5, 29, 1, 3),
    _IcmpStatsInErrors_Type()
)
icmpStatsInErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpStatsInErrors.setStatus("current")
_IcmpStatsOutMsgs_Type = Counter32
_IcmpStatsOutMsgs_Object = MibTableColumn
icmpStatsOutMsgs = _IcmpStatsOutMsgs_Object(
    (1, 3, 6, 1, 2, 1, 5, 29, 1, 4),
    _IcmpStatsOutMsgs_Type()
)
icmpStatsOutMsgs.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpStatsOutMsgs.setStatus("current")
_IcmpStatsOutErrors_Type = Counter32
_IcmpStatsOutErrors_Object = MibTableColumn
icmpStatsOutErrors = _IcmpStatsOutErrors_Object(
    (1, 3, 6, 1, 2, 1, 5, 29, 1, 5),
    _IcmpStatsOutErrors_Type()
)
icmpStatsOutErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpStatsOutErrors.setStatus("current")
_IcmpMsgStatsTable_Object = MibTable
icmpMsgStatsTable = _IcmpMsgStatsTable_Object(
    (1, 3, 6, 1, 2, 1, 5, 30)
)
if mibBuilder.loadTexts:
    icmpMsgStatsTable.setStatus("current")
_IcmpMsgStatsEntry_Object = MibTableRow
icmpMsgStatsEntry = _IcmpMsgStatsEntry_Object(
    (1, 3, 6, 1, 2, 1, 5, 30, 1)
)
icmpMsgStatsEntry.setIndexNames(
    (0, "IP-MIB", "icmpMsgStatsIPVersion"),
    (0, "IP-MIB", "icmpMsgStatsType"),
)
if mibBuilder.loadTexts:
    icmpMsgStatsEntry.setStatus("current")
_IcmpMsgStatsIPVersion_Type = InetVersion
_IcmpMsgStatsIPVersion_Object = MibTableColumn
icmpMsgStatsIPVersion = _IcmpMsgStatsIPVersion_Object(
    (1, 3, 6, 1, 2, 1, 5, 30, 1, 1),
    _IcmpMsgStatsIPVersion_Type()
)
icmpMsgStatsIPVersion.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    icmpMsgStatsIPVersion.setStatus("current")


class _IcmpMsgStatsType_Type(Integer32):
    """Custom type icmpMsgStatsType based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 255),
    )


_IcmpMsgStatsType_Type.__name__ = "Integer32"
_IcmpMsgStatsType_Object = MibTableColumn
icmpMsgStatsType = _IcmpMsgStatsType_Object(
    (1, 3, 6, 1, 2, 1, 5, 30, 1, 2),
    _IcmpMsgStatsType_Type()
)
icmpMsgStatsType.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    icmpMsgStatsType.setStatus("current")
_IcmpMsgStatsInPkts_Type = Counter32
_IcmpMsgStatsInPkts_Object = MibTableColumn
icmpMsgStatsInPkts = _IcmpMsgStatsInPkts_Object(
    (1, 3, 6, 1, 2, 1, 5, 30, 1, 3),
    _IcmpMsgStatsInPkts_Type()
)
icmpMsgStatsInPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpMsgStatsInPkts.setStatus("current")
_IcmpMsgStatsOutPkts_Type = Counter32
_IcmpMsgStatsOutPkts_Object = MibTableColumn
icmpMsgStatsOutPkts = _IcmpMsgStatsOutPkts_Object(
    (1, 3, 6, 1, 2, 1, 5, 30, 1, 4),
    _IcmpMsgStatsOutPkts_Type()
)
icmpMsgStatsOutPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    icmpMsgStatsOutPkts.setStatus("current")
_IpMIBConformance_ObjectIdentity = ObjectIdentity
ipMIBConformance = _IpMIBConformance_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 48, 2)
)
_IpMIBCompliances_ObjectIdentity = ObjectIdentity
ipMIBCompliances = _IpMIBCompliances_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 48, 2, 1)
)
_IpMIBGroups_ObjectIdentity = ObjectIdentity
ipMIBGroups = _IpMIBGroups_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 48, 2, 2)
)

# Managed Objects groups

ipGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 1)
)
ipGroup.setObjects(
      *(("IP-MIB", "ipForwarding"),
        ("IP-MIB", "ipDefaultTTL"),
        ("IP-MIB", "ipInReceives"),
        ("IP-MIB", "ipInHdrErrors"),
        ("IP-MIB", "ipInAddrErrors"),
        ("IP-MIB", "ipForwDatagrams"),
        ("IP-MIB", "ipInUnknownProtos"),
        ("IP-MIB", "ipInDiscards"),
        ("IP-MIB", "ipInDelivers"),
        ("IP-MIB", "ipOutRequests"),
        ("IP-MIB", "ipOutDiscards"),
        ("IP-MIB", "ipOutNoRoutes"),
        ("IP-MIB", "ipReasmTimeout"),
        ("IP-MIB", "ipReasmReqds"),
        ("IP-MIB", "ipReasmOKs"),
        ("IP-MIB", "ipReasmFails"),
        ("IP-MIB", "ipFragOKs"),
        ("IP-MIB", "ipFragFails"),
        ("IP-MIB", "ipFragCreates"),
        ("IP-MIB", "ipAdEntAddr"),
        ("IP-MIB", "ipAdEntIfIndex"),
        ("IP-MIB", "ipAdEntNetMask"),
        ("IP-MIB", "ipAdEntBcastAddr"),
        ("IP-MIB", "ipAdEntReasmMaxSize"),
        ("IP-MIB", "ipNetToMediaIfIndex"),
        ("IP-MIB", "ipNetToMediaPhysAddress"),
        ("IP-MIB", "ipNetToMediaNetAddress"),
        ("IP-MIB", "ipNetToMediaType"),
        ("IP-MIB", "ipRoutingDiscards"))
)
if mibBuilder.loadTexts:
    ipGroup.setStatus("deprecated")

icmpGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 2)
)
icmpGroup.setObjects(
      *(("IP-MIB", "icmpInMsgs"),
        ("IP-MIB", "icmpInErrors"),
        ("IP-MIB", "icmpInDestUnreachs"),
        ("IP-MIB", "icmpInTimeExcds"),
        ("IP-MIB", "icmpInParmProbs"),
        ("IP-MIB", "icmpInSrcQuenchs"),
        ("IP-MIB", "icmpInRedirects"),
        ("IP-MIB", "icmpInEchos"),
        ("IP-MIB", "icmpInEchoReps"),
        ("IP-MIB", "icmpInTimestamps"),
        ("IP-MIB", "icmpInTimestampReps"),
        ("IP-MIB", "icmpInAddrMasks"),
        ("IP-MIB", "icmpInAddrMaskReps"),
        ("IP-MIB", "icmpOutMsgs"),
        ("IP-MIB", "icmpOutErrors"),
        ("IP-MIB", "icmpOutDestUnreachs"),
        ("IP-MIB", "icmpOutTimeExcds"),
        ("IP-MIB", "icmpOutParmProbs"),
        ("IP-MIB", "icmpOutSrcQuenchs"),
        ("IP-MIB", "icmpOutRedirects"),
        ("IP-MIB", "icmpOutEchos"),
        ("IP-MIB", "icmpOutEchoReps"),
        ("IP-MIB", "icmpOutTimestamps"),
        ("IP-MIB", "icmpOutTimestampReps"),
        ("IP-MIB", "icmpOutAddrMasks"),
        ("IP-MIB", "icmpOutAddrMaskReps"))
)
if mibBuilder.loadTexts:
    icmpGroup.setStatus("deprecated")

ipv4GeneralGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 3)
)
ipv4GeneralGroup.setObjects(
      *(("IP-MIB", "ipForwarding"),
        ("IP-MIB", "ipDefaultTTL"),
        ("IP-MIB", "ipReasmTimeout"))
)
if mibBuilder.loadTexts:
    ipv4GeneralGroup.setStatus("current")

ipv4IfGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 4)
)
ipv4IfGroup.setObjects(
      *(("IP-MIB", "ipv4InterfaceReasmMaxSize"),
        ("IP-MIB", "ipv4InterfaceEnableStatus"),
        ("IP-MIB", "ipv4InterfaceRetransmitTime"))
)
if mibBuilder.loadTexts:
    ipv4IfGroup.setStatus("current")

ipv6GeneralGroup2 = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 5)
)
ipv6GeneralGroup2.setObjects(
      *(("IP-MIB", "ipv6IpForwarding"),
        ("IP-MIB", "ipv6IpDefaultHopLimit"))
)
if mibBuilder.loadTexts:
    ipv6GeneralGroup2.setStatus("current")

ipv6IfGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 6)
)
ipv6IfGroup.setObjects(
      *(("IP-MIB", "ipv6InterfaceReasmMaxSize"),
        ("IP-MIB", "ipv6InterfaceIdentifier"),
        ("IP-MIB", "ipv6InterfaceEnableStatus"),
        ("IP-MIB", "ipv6InterfaceReachableTime"),
        ("IP-MIB", "ipv6InterfaceRetransmitTime"),
        ("IP-MIB", "ipv6InterfaceForwarding"))
)
if mibBuilder.loadTexts:
    ipv6IfGroup.setStatus("current")

ipLastChangeGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 7)
)
ipLastChangeGroup.setObjects(
      *(("IP-MIB", "ipv4InterfaceTableLastChange"),
        ("IP-MIB", "ipv6InterfaceTableLastChange"),
        ("IP-MIB", "ipIfStatsTableLastChange"))
)
if mibBuilder.loadTexts:
    ipLastChangeGroup.setStatus("current")

ipSystemStatsGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 8)
)
ipSystemStatsGroup.setObjects(
      *(("IP-MIB", "ipSystemStatsInReceives"),
        ("IP-MIB", "ipSystemStatsInOctets"),
        ("IP-MIB", "ipSystemStatsInHdrErrors"),
        ("IP-MIB", "ipSystemStatsInNoRoutes"),
        ("IP-MIB", "ipSystemStatsInAddrErrors"),
        ("IP-MIB", "ipSystemStatsInUnknownProtos"),
        ("IP-MIB", "ipSystemStatsInTruncatedPkts"),
        ("IP-MIB", "ipSystemStatsInForwDatagrams"),
        ("IP-MIB", "ipSystemStatsReasmReqds"),
        ("IP-MIB", "ipSystemStatsReasmOKs"),
        ("IP-MIB", "ipSystemStatsReasmFails"),
        ("IP-MIB", "ipSystemStatsInDiscards"),
        ("IP-MIB", "ipSystemStatsInDelivers"),
        ("IP-MIB", "ipSystemStatsOutRequests"),
        ("IP-MIB", "ipSystemStatsOutNoRoutes"),
        ("IP-MIB", "ipSystemStatsOutForwDatagrams"),
        ("IP-MIB", "ipSystemStatsOutDiscards"),
        ("IP-MIB", "ipSystemStatsOutFragReqds"),
        ("IP-MIB", "ipSystemStatsOutFragOKs"),
        ("IP-MIB", "ipSystemStatsOutFragFails"),
        ("IP-MIB", "ipSystemStatsOutFragCreates"),
        ("IP-MIB", "ipSystemStatsOutTransmits"),
        ("IP-MIB", "ipSystemStatsOutOctets"),
        ("IP-MIB", "ipSystemStatsInMcastPkts"),
        ("IP-MIB", "ipSystemStatsInMcastOctets"),
        ("IP-MIB", "ipSystemStatsOutMcastPkts"),
        ("IP-MIB", "ipSystemStatsOutMcastOctets"),
        ("IP-MIB", "ipSystemStatsDiscontinuityTime"),
        ("IP-MIB", "ipSystemStatsRefreshRate"))
)
if mibBuilder.loadTexts:
    ipSystemStatsGroup.setStatus("current")

ipv4SystemStatsGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 9)
)
ipv4SystemStatsGroup.setObjects(
      *(("IP-MIB", "ipSystemStatsInBcastPkts"),
        ("IP-MIB", "ipSystemStatsOutBcastPkts"))
)
if mibBuilder.loadTexts:
    ipv4SystemStatsGroup.setStatus("current")

ipSystemStatsHCOctetGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 10)
)
ipSystemStatsHCOctetGroup.setObjects(
      *(("IP-MIB", "ipSystemStatsHCInOctets"),
        ("IP-MIB", "ipSystemStatsHCOutOctets"),
        ("IP-MIB", "ipSystemStatsHCInMcastOctets"),
        ("IP-MIB", "ipSystemStatsHCOutMcastOctets"))
)
if mibBuilder.loadTexts:
    ipSystemStatsHCOctetGroup.setStatus("current")

ipSystemStatsHCPacketGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 11)
)
ipSystemStatsHCPacketGroup.setObjects(
      *(("IP-MIB", "ipSystemStatsHCInReceives"),
        ("IP-MIB", "ipSystemStatsHCInForwDatagrams"),
        ("IP-MIB", "ipSystemStatsHCInDelivers"),
        ("IP-MIB", "ipSystemStatsHCOutRequests"),
        ("IP-MIB", "ipSystemStatsHCOutForwDatagrams"),
        ("IP-MIB", "ipSystemStatsHCOutTransmits"),
        ("IP-MIB", "ipSystemStatsHCInMcastPkts"),
        ("IP-MIB", "ipSystemStatsHCOutMcastPkts"))
)
if mibBuilder.loadTexts:
    ipSystemStatsHCPacketGroup.setStatus("current")

ipv4SystemStatsHCPacketGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 12)
)
ipv4SystemStatsHCPacketGroup.setObjects(
      *(("IP-MIB", "ipSystemStatsHCInBcastPkts"),
        ("IP-MIB", "ipSystemStatsHCOutBcastPkts"))
)
if mibBuilder.loadTexts:
    ipv4SystemStatsHCPacketGroup.setStatus("current")

ipIfStatsGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 13)
)
ipIfStatsGroup.setObjects(
      *(("IP-MIB", "ipIfStatsInReceives"),
        ("IP-MIB", "ipIfStatsInOctets"),
        ("IP-MIB", "ipIfStatsInHdrErrors"),
        ("IP-MIB", "ipIfStatsInNoRoutes"),
        ("IP-MIB", "ipIfStatsInAddrErrors"),
        ("IP-MIB", "ipIfStatsInUnknownProtos"),
        ("IP-MIB", "ipIfStatsInTruncatedPkts"),
        ("IP-MIB", "ipIfStatsInForwDatagrams"),
        ("IP-MIB", "ipIfStatsReasmReqds"),
        ("IP-MIB", "ipIfStatsReasmOKs"),
        ("IP-MIB", "ipIfStatsReasmFails"),
        ("IP-MIB", "ipIfStatsInDiscards"),
        ("IP-MIB", "ipIfStatsInDelivers"),
        ("IP-MIB", "ipIfStatsOutRequests"),
        ("IP-MIB", "ipIfStatsOutForwDatagrams"),
        ("IP-MIB", "ipIfStatsOutDiscards"),
        ("IP-MIB", "ipIfStatsOutFragReqds"),
        ("IP-MIB", "ipIfStatsOutFragOKs"),
        ("IP-MIB", "ipIfStatsOutFragFails"),
        ("IP-MIB", "ipIfStatsOutFragCreates"),
        ("IP-MIB", "ipIfStatsOutTransmits"),
        ("IP-MIB", "ipIfStatsOutOctets"),
        ("IP-MIB", "ipIfStatsInMcastPkts"),
        ("IP-MIB", "ipIfStatsInMcastOctets"),
        ("IP-MIB", "ipIfStatsOutMcastPkts"),
        ("IP-MIB", "ipIfStatsOutMcastOctets"),
        ("IP-MIB", "ipIfStatsDiscontinuityTime"),
        ("IP-MIB", "ipIfStatsRefreshRate"))
)
if mibBuilder.loadTexts:
    ipIfStatsGroup.setStatus("current")

ipv4IfStatsGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 14)
)
ipv4IfStatsGroup.setObjects(
      *(("IP-MIB", "ipIfStatsInBcastPkts"),
        ("IP-MIB", "ipIfStatsOutBcastPkts"))
)
if mibBuilder.loadTexts:
    ipv4IfStatsGroup.setStatus("current")

ipIfStatsHCOctetGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 15)
)
ipIfStatsHCOctetGroup.setObjects(
      *(("IP-MIB", "ipIfStatsHCInOctets"),
        ("IP-MIB", "ipIfStatsHCOutOctets"),
        ("IP-MIB", "ipIfStatsHCInMcastOctets"),
        ("IP-MIB", "ipIfStatsHCOutMcastOctets"))
)
if mibBuilder.loadTexts:
    ipIfStatsHCOctetGroup.setStatus("current")

ipIfStatsHCPacketGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 16)
)
ipIfStatsHCPacketGroup.setObjects(
      *(("IP-MIB", "ipIfStatsHCInReceives"),
        ("IP-MIB", "ipIfStatsHCInForwDatagrams"),
        ("IP-MIB", "ipIfStatsHCInDelivers"),
        ("IP-MIB", "ipIfStatsHCOutRequests"),
        ("IP-MIB", "ipIfStatsHCOutForwDatagrams"),
        ("IP-MIB", "ipIfStatsHCOutTransmits"),
        ("IP-MIB", "ipIfStatsHCInMcastPkts"),
        ("IP-MIB", "ipIfStatsHCOutMcastPkts"))
)
if mibBuilder.loadTexts:
    ipIfStatsHCPacketGroup.setStatus("current")

ipv4IfStatsHCPacketGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 17)
)
ipv4IfStatsHCPacketGroup.setObjects(
      *(("IP-MIB", "ipIfStatsHCInBcastPkts"),
        ("IP-MIB", "ipIfStatsHCOutBcastPkts"))
)
if mibBuilder.loadTexts:
    ipv4IfStatsHCPacketGroup.setStatus("current")

ipAddressPrefixGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 18)
)
ipAddressPrefixGroup.setObjects(
      *(("IP-MIB", "ipAddressPrefixOrigin"),
        ("IP-MIB", "ipAddressPrefixOnLinkFlag"),
        ("IP-MIB", "ipAddressPrefixAutonomousFlag"),
        ("IP-MIB", "ipAddressPrefixAdvPreferredLifetime"),
        ("IP-MIB", "ipAddressPrefixAdvValidLifetime"))
)
if mibBuilder.loadTexts:
    ipAddressPrefixGroup.setStatus("current")

ipAddressGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 19)
)
ipAddressGroup.setObjects(
      *(("IP-MIB", "ipAddressSpinLock"),
        ("IP-MIB", "ipAddressIfIndex"),
        ("IP-MIB", "ipAddressType"),
        ("IP-MIB", "ipAddressPrefix"),
        ("IP-MIB", "ipAddressOrigin"),
        ("IP-MIB", "ipAddressStatus"),
        ("IP-MIB", "ipAddressCreated"),
        ("IP-MIB", "ipAddressLastChanged"),
        ("IP-MIB", "ipAddressRowStatus"),
        ("IP-MIB", "ipAddressStorageType"))
)
if mibBuilder.loadTexts:
    ipAddressGroup.setStatus("current")

ipNetToPhysicalGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 20)
)
ipNetToPhysicalGroup.setObjects(
      *(("IP-MIB", "ipNetToPhysicalPhysAddress"),
        ("IP-MIB", "ipNetToPhysicalLastUpdated"),
        ("IP-MIB", "ipNetToPhysicalType"),
        ("IP-MIB", "ipNetToPhysicalState"),
        ("IP-MIB", "ipNetToPhysicalRowStatus"))
)
if mibBuilder.loadTexts:
    ipNetToPhysicalGroup.setStatus("current")

ipv6ScopeGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 21)
)
ipv6ScopeGroup.setObjects(
      *(("IP-MIB", "ipv6ScopeZoneIndexLinkLocal"),
        ("IP-MIB", "ipv6ScopeZoneIndex3"),
        ("IP-MIB", "ipv6ScopeZoneIndexAdminLocal"),
        ("IP-MIB", "ipv6ScopeZoneIndexSiteLocal"),
        ("IP-MIB", "ipv6ScopeZoneIndex6"),
        ("IP-MIB", "ipv6ScopeZoneIndex7"),
        ("IP-MIB", "ipv6ScopeZoneIndexOrganizationLocal"),
        ("IP-MIB", "ipv6ScopeZoneIndex9"),
        ("IP-MIB", "ipv6ScopeZoneIndexA"),
        ("IP-MIB", "ipv6ScopeZoneIndexB"),
        ("IP-MIB", "ipv6ScopeZoneIndexC"),
        ("IP-MIB", "ipv6ScopeZoneIndexD"))
)
if mibBuilder.loadTexts:
    ipv6ScopeGroup.setStatus("current")

ipDefaultRouterGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 22)
)
ipDefaultRouterGroup.setObjects(
      *(("IP-MIB", "ipDefaultRouterLifetime"),
        ("IP-MIB", "ipDefaultRouterPreference"))
)
if mibBuilder.loadTexts:
    ipDefaultRouterGroup.setStatus("current")

ipv6RouterAdvertGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 23)
)
ipv6RouterAdvertGroup.setObjects(
      *(("IP-MIB", "ipv6RouterAdvertSpinLock"),
        ("IP-MIB", "ipv6RouterAdvertSendAdverts"),
        ("IP-MIB", "ipv6RouterAdvertMaxInterval"),
        ("IP-MIB", "ipv6RouterAdvertMinInterval"),
        ("IP-MIB", "ipv6RouterAdvertManagedFlag"),
        ("IP-MIB", "ipv6RouterAdvertOtherConfigFlag"),
        ("IP-MIB", "ipv6RouterAdvertLinkMTU"),
        ("IP-MIB", "ipv6RouterAdvertReachableTime"),
        ("IP-MIB", "ipv6RouterAdvertRetransmitTime"),
        ("IP-MIB", "ipv6RouterAdvertCurHopLimit"),
        ("IP-MIB", "ipv6RouterAdvertDefaultLifetime"),
        ("IP-MIB", "ipv6RouterAdvertRowStatus"))
)
if mibBuilder.loadTexts:
    ipv6RouterAdvertGroup.setStatus("current")

icmpStatsGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 48, 2, 2, 24)
)
icmpStatsGroup.setObjects(
      *(("IP-MIB", "icmpStatsInMsgs"),
        ("IP-MIB", "icmpStatsInErrors"),
        ("IP-MIB", "icmpStatsOutMsgs"),
        ("IP-MIB", "icmpStatsOutErrors"),
        ("IP-MIB", "icmpMsgStatsInPkts"),
        ("IP-MIB", "icmpMsgStatsOutPkts"))
)
if mibBuilder.loadTexts:
    icmpStatsGroup.setStatus("current")


# Notification objects


# Notifications groups


# Agent capabilities


# Module compliance

ipMIBCompliance = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 48, 2, 1, 1)
)
if mibBuilder.loadTexts:
    ipMIBCompliance.setStatus(
        "deprecated"
    )

ipMIBCompliance2 = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 48, 2, 1, 2)
)
if mibBuilder.loadTexts:
    ipMIBCompliance2.setStatus(
        "current"
    )


# Export all MIB objects to the MIB builder

mibBuilder.exportSymbols(
    "IP-MIB",
    **{"IpAddressOriginTC": IpAddressOriginTC,
       "IpAddressStatusTC": IpAddressStatusTC,
       "IpAddressPrefixOriginTC": IpAddressPrefixOriginTC,
       "Ipv6AddressIfIdentifierTC": Ipv6AddressIfIdentifierTC,
       "ip": ip,
       "ipForwarding": ipForwarding,
       "ipDefaultTTL": ipDefaultTTL,
       "ipInReceives": ipInReceives,
       "ipInHdrErrors": ipInHdrErrors,
       "ipInAddrErrors": ipInAddrErrors,
       "ipForwDatagrams": ipForwDatagrams,
       "ipInUnknownProtos": ipInUnknownProtos,
       "ipInDiscards": ipInDiscards,
       "ipInDelivers": ipInDelivers,
       "ipOutRequests": ipOutRequests,
       "ipOutDiscards": ipOutDiscards,
       "ipOutNoRoutes": ipOutNoRoutes,
       "ipReasmTimeout": ipReasmTimeout,
       "ipReasmReqds": ipReasmReqds,
       "ipReasmOKs": ipReasmOKs,
       "ipReasmFails": ipReasmFails,
       "ipFragOKs": ipFragOKs,
       "ipFragFails": ipFragFails,
       "ipFragCreates": ipFragCreates,
       "ipAddrTable": ipAddrTable,
       "ipAddrEntry": ipAddrEntry,
       "ipAdEntAddr": ipAdEntAddr,
       "ipAdEntIfIndex": ipAdEntIfIndex,
       "ipAdEntNetMask": ipAdEntNetMask,
       "ipAdEntBcastAddr": ipAdEntBcastAddr,
       "ipAdEntReasmMaxSize": ipAdEntReasmMaxSize,
       "ipNetToMediaTable": ipNetToMediaTable,
       "ipNetToMediaEntry": ipNetToMediaEntry,
       "ipNetToMediaIfIndex": ipNetToMediaIfIndex,
       "ipNetToMediaPhysAddress": ipNetToMediaPhysAddress,
       "ipNetToMediaNetAddress": ipNetToMediaNetAddress,
       "ipNetToMediaType": ipNetToMediaType,
       "ipRoutingDiscards": ipRoutingDiscards,
       "ipv6IpForwarding": ipv6IpForwarding,
       "ipv6IpDefaultHopLimit": ipv6IpDefaultHopLimit,
       "ipv4InterfaceTableLastChange": ipv4InterfaceTableLastChange,
       "ipv4InterfaceTable": ipv4InterfaceTable,
       "ipv4InterfaceEntry": ipv4InterfaceEntry,
       "ipv4InterfaceIfIndex": ipv4InterfaceIfIndex,
       "ipv4InterfaceReasmMaxSize": ipv4InterfaceReasmMaxSize,
       "ipv4InterfaceEnableStatus": ipv4InterfaceEnableStatus,
       "ipv4InterfaceRetransmitTime": ipv4InterfaceRetransmitTime,
       "ipv6InterfaceTableLastChange": ipv6InterfaceTableLastChange,
       "ipv6InterfaceTable": ipv6InterfaceTable,
       "ipv6InterfaceEntry": ipv6InterfaceEntry,
       "ipv6InterfaceIfIndex": ipv6InterfaceIfIndex,
       "ipv6InterfaceReasmMaxSize": ipv6InterfaceReasmMaxSize,
       "ipv6InterfaceIdentifier": ipv6InterfaceIdentifier,
       "ipv6InterfaceEnableStatus": ipv6InterfaceEnableStatus,
       "ipv6InterfaceReachableTime": ipv6InterfaceReachableTime,
       "ipv6InterfaceRetransmitTime": ipv6InterfaceRetransmitTime,
       "ipv6InterfaceForwarding": ipv6InterfaceForwarding,
       "ipTrafficStats": ipTrafficStats,
       "ipSystemStatsTable": ipSystemStatsTable,
       "ipSystemStatsEntry": ipSystemStatsEntry,
       "ipSystemStatsIPVersion": ipSystemStatsIPVersion,
       "ipSystemStatsInReceives": ipSystemStatsInReceives,
       "ipSystemStatsHCInReceives": ipSystemStatsHCInReceives,
       "ipSystemStatsInOctets": ipSystemStatsInOctets,
       "ipSystemStatsHCInOctets": ipSystemStatsHCInOctets,
       "ipSystemStatsInHdrErrors": ipSystemStatsInHdrErrors,
       "ipSystemStatsInNoRoutes": ipSystemStatsInNoRoutes,
       "ipSystemStatsInAddrErrors": ipSystemStatsInAddrErrors,
       "ipSystemStatsInUnknownProtos": ipSystemStatsInUnknownProtos,
       "ipSystemStatsInTruncatedPkts": ipSystemStatsInTruncatedPkts,
       "ipSystemStatsInForwDatagrams": ipSystemStatsInForwDatagrams,
       "ipSystemStatsHCInForwDatagrams": ipSystemStatsHCInForwDatagrams,
       "ipSystemStatsReasmReqds": ipSystemStatsReasmReqds,
       "ipSystemStatsReasmOKs": ipSystemStatsReasmOKs,
       "ipSystemStatsReasmFails": ipSystemStatsReasmFails,
       "ipSystemStatsInDiscards": ipSystemStatsInDiscards,
       "ipSystemStatsInDelivers": ipSystemStatsInDelivers,
       "ipSystemStatsHCInDelivers": ipSystemStatsHCInDelivers,
       "ipSystemStatsOutRequests": ipSystemStatsOutRequests,
       "ipSystemStatsHCOutRequests": ipSystemStatsHCOutRequests,
       "ipSystemStatsOutNoRoutes": ipSystemStatsOutNoRoutes,
       "ipSystemStatsOutForwDatagrams": ipSystemStatsOutForwDatagrams,
       "ipSystemStatsHCOutForwDatagrams": ipSystemStatsHCOutForwDatagrams,
       "ipSystemStatsOutDiscards": ipSystemStatsOutDiscards,
       "ipSystemStatsOutFragReqds": ipSystemStatsOutFragReqds,
       "ipSystemStatsOutFragOKs": ipSystemStatsOutFragOKs,
       "ipSystemStatsOutFragFails": ipSystemStatsOutFragFails,
       "ipSystemStatsOutFragCreates": ipSystemStatsOutFragCreates,
       "ipSystemStatsOutTransmits": ipSystemStatsOutTransmits,
       "ipSystemStatsHCOutTransmits": ipSystemStatsHCOutTransmits,
       "ipSystemStatsOutOctets": ipSystemStatsOutOctets,
       "ipSystemStatsHCOutOctets": ipSystemStatsHCOutOctets,
       "ipSystemStatsInMcastPkts": ipSystemStatsInMcastPkts,
       "ipSystemStatsHCInMcastPkts": ipSystemStatsHCInMcastPkts,
       "ipSystemStatsInMcastOctets": ipSystemStatsInMcastOctets,
       "ipSystemStatsHCInMcastOctets": ipSystemStatsHCInMcastOctets,
       "ipSystemStatsOutMcastPkts": ipSystemStatsOutMcastPkts,
       "ipSystemStatsHCOutMcastPkts": ipSystemStatsHCOutMcastPkts,
       "ipSystemStatsOutMcastOctets": ipSystemStatsOutMcastOctets,
       "ipSystemStatsHCOutMcastOctets": ipSystemStatsHCOutMcastOctets,
       "ipSystemStatsInBcastPkts": ipSystemStatsInBcastPkts,
       "ipSystemStatsHCInBcastPkts": ipSystemStatsHCInBcastPkts,
       "ipSystemStatsOutBcastPkts": ipSystemStatsOutBcastPkts,
       "ipSystemStatsHCOutBcastPkts": ipSystemStatsHCOutBcastPkts,
       "ipSystemStatsDiscontinuityTime": ipSystemStatsDiscontinuityTime,
       "ipSystemStatsRefreshRate": ipSystemStatsRefreshRate,
       "ipIfStatsTableLastChange": ipIfStatsTableLastChange,
       "ipIfStatsTable": ipIfStatsTable,
       "ipIfStatsEntry": ipIfStatsEntry,
       "ipIfStatsIPVersion": ipIfStatsIPVersion,
       "ipIfStatsIfIndex": ipIfStatsIfIndex,
       "ipIfStatsInReceives": ipIfStatsInReceives,
       "ipIfStatsHCInReceives": ipIfStatsHCInReceives,
       "ipIfStatsInOctets": ipIfStatsInOctets,
       "ipIfStatsHCInOctets": ipIfStatsHCInOctets,
       "ipIfStatsInHdrErrors": ipIfStatsInHdrErrors,
       "ipIfStatsInNoRoutes": ipIfStatsInNoRoutes,
       "ipIfStatsInAddrErrors": ipIfStatsInAddrErrors,
       "ipIfStatsInUnknownProtos": ipIfStatsInUnknownProtos,
       "ipIfStatsInTruncatedPkts": ipIfStatsInTruncatedPkts,
       "ipIfStatsInForwDatagrams": ipIfStatsInForwDatagrams,
       "ipIfStatsHCInForwDatagrams": ipIfStatsHCInForwDatagrams,
       "ipIfStatsReasmReqds": ipIfStatsReasmReqds,
       "ipIfStatsReasmOKs": ipIfStatsReasmOKs,
       "ipIfStatsReasmFails": ipIfStatsReasmFails,
       "ipIfStatsInDiscards": ipIfStatsInDiscards,
       "ipIfStatsInDelivers": ipIfStatsInDelivers,
       "ipIfStatsHCInDelivers": ipIfStatsHCInDelivers,
       "ipIfStatsOutRequests": ipIfStatsOutRequests,
       "ipIfStatsHCOutRequests": ipIfStatsHCOutRequests,
       "ipIfStatsOutForwDatagrams": ipIfStatsOutForwDatagrams,
       "ipIfStatsHCOutForwDatagrams": ipIfStatsHCOutForwDatagrams,
       "ipIfStatsOutDiscards": ipIfStatsOutDiscards,
       "ipIfStatsOutFragReqds": ipIfStatsOutFragReqds,
       "ipIfStatsOutFragOKs": ipIfStatsOutFragOKs,
       "ipIfStatsOutFragFails": ipIfStatsOutFragFails,
       "ipIfStatsOutFragCreates": ipIfStatsOutFragCreates,
       "ipIfStatsOutTransmits": ipIfStatsOutTransmits,
       "ipIfStatsHCOutTransmits": ipIfStatsHCOutTransmits,
       "ipIfStatsOutOctets": ipIfStatsOutOctets,
       "ipIfStatsHCOutOctets": ipIfStatsHCOutOctets,
       "ipIfStatsInMcastPkts": ipIfStatsInMcastPkts,
       "ipIfStatsHCInMcastPkts": ipIfStatsHCInMcastPkts,
       "ipIfStatsInMcastOctets": ipIfStatsInMcastOctets,
       "ipIfStatsHCInMcastOctets": ipIfStatsHCInMcastOctets,
       "ipIfStatsOutMcastPkts": ipIfStatsOutMcastPkts,
       "ipIfStatsHCOutMcastPkts": ipIfStatsHCOutMcastPkts,
       "ipIfStatsOutMcastOctets": ipIfStatsOutMcastOctets,
       "ipIfStatsHCOutMcastOctets": ipIfStatsHCOutMcastOctets,
       "ipIfStatsInBcastPkts": ipIfStatsInBcastPkts,
       "ipIfStatsHCInBcastPkts": ipIfStatsHCInBcastPkts,
       "ipIfStatsOutBcastPkts": ipIfStatsOutBcastPkts,
       "ipIfStatsHCOutBcastPkts": ipIfStatsHCOutBcastPkts,
       "ipIfStatsDiscontinuityTime": ipIfStatsDiscontinuityTime,
       "ipIfStatsRefreshRate": ipIfStatsRefreshRate,
       "ipAddressPrefixTable": ipAddressPrefixTable,
       "ipAddressPrefixEntry": ipAddressPrefixEntry,
       "ipAddressPrefixIfIndex": ipAddressPrefixIfIndex,
       "ipAddressPrefixType": ipAddressPrefixType,
       "ipAddressPrefixPrefix": ipAddressPrefixPrefix,
       "ipAddressPrefixLength": ipAddressPrefixLength,
       "ipAddressPrefixOrigin": ipAddressPrefixOrigin,
       "ipAddressPrefixOnLinkFlag": ipAddressPrefixOnLinkFlag,
       "ipAddressPrefixAutonomousFlag": ipAddressPrefixAutonomousFlag,
       "ipAddressPrefixAdvPreferredLifetime": ipAddressPrefixAdvPreferredLifetime,
       "ipAddressPrefixAdvValidLifetime": ipAddressPrefixAdvValidLifetime,
       "ipAddressSpinLock": ipAddressSpinLock,
       "ipAddressTable": ipAddressTable,
       "ipAddressEntry": ipAddressEntry,
       "ipAddressAddrType": ipAddressAddrType,
       "ipAddressAddr": ipAddressAddr,
       "ipAddressIfIndex": ipAddressIfIndex,
       "ipAddressType": ipAddressType,
       "ipAddressPrefix": ipAddressPrefix,
       "ipAddressOrigin": ipAddressOrigin,
       "ipAddressStatus": ipAddressStatus,
       "ipAddressCreated": ipAddressCreated,
       "ipAddressLastChanged": ipAddressLastChanged,
       "ipAddressRowStatus": ipAddressRowStatus,
       "ipAddressStorageType": ipAddressStorageType,
       "ipNetToPhysicalTable": ipNetToPhysicalTable,
       "ipNetToPhysicalEntry": ipNetToPhysicalEntry,
       "ipNetToPhysicalIfIndex": ipNetToPhysicalIfIndex,
       "ipNetToPhysicalNetAddressType": ipNetToPhysicalNetAddressType,
       "ipNetToPhysicalNetAddress": ipNetToPhysicalNetAddress,
       "ipNetToPhysicalPhysAddress": ipNetToPhysicalPhysAddress,
       "ipNetToPhysicalLastUpdated": ipNetToPhysicalLastUpdated,
       "ipNetToPhysicalType": ipNetToPhysicalType,
       "ipNetToPhysicalState": ipNetToPhysicalState,
       "ipNetToPhysicalRowStatus": ipNetToPhysicalRowStatus,
       "ipv6ScopeZoneIndexTable": ipv6ScopeZoneIndexTable,
       "ipv6ScopeZoneIndexEntry": ipv6ScopeZoneIndexEntry,
       "ipv6ScopeZoneIndexIfIndex": ipv6ScopeZoneIndexIfIndex,
       "ipv6ScopeZoneIndexLinkLocal": ipv6ScopeZoneIndexLinkLocal,
       "ipv6ScopeZoneIndex3": ipv6ScopeZoneIndex3,
       "ipv6ScopeZoneIndexAdminLocal": ipv6ScopeZoneIndexAdminLocal,
       "ipv6ScopeZoneIndexSiteLocal": ipv6ScopeZoneIndexSiteLocal,
       "ipv6ScopeZoneIndex6": ipv6ScopeZoneIndex6,
       "ipv6ScopeZoneIndex7": ipv6ScopeZoneIndex7,
       "ipv6ScopeZoneIndexOrganizationLocal": ipv6ScopeZoneIndexOrganizationLocal,
       "ipv6ScopeZoneIndex9": ipv6ScopeZoneIndex9,
       "ipv6ScopeZoneIndexA": ipv6ScopeZoneIndexA,
       "ipv6ScopeZoneIndexB": ipv6ScopeZoneIndexB,
       "ipv6ScopeZoneIndexC": ipv6ScopeZoneIndexC,
       "ipv6ScopeZoneIndexD": ipv6ScopeZoneIndexD,
       "ipDefaultRouterTable": ipDefaultRouterTable,
       "ipDefaultRouterEntry": ipDefaultRouterEntry,
       "ipDefaultRouterAddressType": ipDefaultRouterAddressType,
       "ipDefaultRouterAddress": ipDefaultRouterAddress,
       "ipDefaultRouterIfIndex": ipDefaultRouterIfIndex,
       "ipDefaultRouterLifetime": ipDefaultRouterLifetime,
       "ipDefaultRouterPreference": ipDefaultRouterPreference,
       "ipv6RouterAdvertSpinLock": ipv6RouterAdvertSpinLock,
       "ipv6RouterAdvertTable": ipv6RouterAdvertTable,
       "ipv6RouterAdvertEntry": ipv6RouterAdvertEntry,
       "ipv6RouterAdvertIfIndex": ipv6RouterAdvertIfIndex,
       "ipv6RouterAdvertSendAdverts": ipv6RouterAdvertSendAdverts,
       "ipv6RouterAdvertMaxInterval": ipv6RouterAdvertMaxInterval,
       "ipv6RouterAdvertMinInterval": ipv6RouterAdvertMinInterval,
       "ipv6RouterAdvertManagedFlag": ipv6RouterAdvertManagedFlag,
       "ipv6RouterAdvertOtherConfigFlag": ipv6RouterAdvertOtherConfigFlag,
       "ipv6RouterAdvertLinkMTU": ipv6RouterAdvertLinkMTU,
       "ipv6RouterAdvertReachableTime": ipv6RouterAdvertReachableTime,
       "ipv6RouterAdvertRetransmitTime": ipv6RouterAdvertRetransmitTime,
       "ipv6RouterAdvertCurHopLimit": ipv6RouterAdvertCurHopLimit,
       "ipv6RouterAdvertDefaultLifetime": ipv6RouterAdvertDefaultLifetime,
       "ipv6RouterAdvertRowStatus": ipv6RouterAdvertRowStatus,
       "icmp": icmp,
       "icmpInMsgs": icmpInMsgs,
       "icmpInErrors": icmpInErrors,
       "icmpInDestUnreachs": icmpInDestUnreachs,
       "icmpInTimeExcds": icmpInTimeExcds,
       "icmpInParmProbs": icmpInParmProbs,
       "icmpInSrcQuenchs": icmpInSrcQuenchs,
       "icmpInRedirects": icmpInRedirects,
       "icmpInEchos": icmpInEchos,
       "icmpInEchoReps": icmpInEchoReps,
       "icmpInTimestamps": icmpInTimestamps,
       "icmpInTimestampReps": icmpInTimestampReps,
       "icmpInAddrMasks": icmpInAddrMasks,
       "icmpInAddrMaskReps": icmpInAddrMaskReps,
       "icmpOutMsgs": icmpOutMsgs,
       "icmpOutErrors": icmpOutErrors,
       "icmpOutDestUnreachs": icmpOutDestUnreachs,
       "icmpOutTimeExcds": icmpOutTimeExcds,
       "icmpOutParmProbs": icmpOutParmProbs,
       "icmpOutSrcQuenchs": icmpOutSrcQuenchs,
       "icmpOutRedirects": icmpOutRedirects,
       "icmpOutEchos": icmpOutEchos,
       "icmpOutEchoReps": icmpOutEchoReps,
       "icmpOutTimestamps": icmpOutTimestamps,
       "icmpOutTimestampReps": icmpOutTimestampReps,
       "icmpOutAddrMasks": icmpOutAddrMasks,
       "icmpOutAddrMaskReps": icmpOutAddrMaskReps,
       "icmpStatsTable": icmpStatsTable,
       "icmpStatsEntry": icmpStatsEntry,
       "icmpStatsIPVersion": icmpStatsIPVersion,
       "icmpStatsInMsgs": icmpStatsInMsgs,
       "icmpStatsInErrors": icmpStatsInErrors,
       "icmpStatsOutMsgs": icmpStatsOutMsgs,
       "icmpStatsOutErrors": icmpStatsOutErrors,
       "icmpMsgStatsTable": icmpMsgStatsTable,
       "icmpMsgStatsEntry": icmpMsgStatsEntry,
       "icmpMsgStatsIPVersion": icmpMsgStatsIPVersion,
       "icmpMsgStatsType": icmpMsgStatsType,
       "icmpMsgStatsInPkts": icmpMsgStatsInPkts,
       "icmpMsgStatsOutPkts": icmpMsgStatsOutPkts,
       "ipMIB": ipMIB,
       "ipMIBConformance": ipMIBConformance,
       "ipMIBCompliances": ipMIBCompliances,
       "ipMIBCompliance": ipMIBCompliance,
       "ipMIBCompliance2": ipMIBCompliance2,
       "ipMIBGroups": ipMIBGroups,
       "ipGroup": ipGroup,
       "icmpGroup": icmpGroup,
       "ipv4GeneralGroup": ipv4GeneralGroup,
       "ipv4IfGroup": ipv4IfGroup,
       "ipv6GeneralGroup2": ipv6GeneralGroup2,
       "ipv6IfGroup": ipv6IfGroup,
       "ipLastChangeGroup": ipLastChangeGroup,
       "ipSystemStatsGroup": ipSystemStatsGroup,
       "ipv4SystemStatsGroup": ipv4SystemStatsGroup,
       "ipSystemStatsHCOctetGroup": ipSystemStatsHCOctetGroup,
       "ipSystemStatsHCPacketGroup": ipSystemStatsHCPacketGroup,
       "ipv4SystemStatsHCPacketGroup": ipv4SystemStatsHCPacketGroup,
       "ipIfStatsGroup": ipIfStatsGroup,
       "ipv4IfStatsGroup": ipv4IfStatsGroup,
       "ipIfStatsHCOctetGroup": ipIfStatsHCOctetGroup,
       "ipIfStatsHCPacketGroup": ipIfStatsHCPacketGroup,
       "ipv4IfStatsHCPacketGroup": ipv4IfStatsHCPacketGroup,
       "ipAddressPrefixGroup": ipAddressPrefixGroup,
       "ipAddressGroup": ipAddressGroup,
       "ipNetToPhysicalGroup": ipNetToPhysicalGroup,
       "ipv6ScopeGroup": ipv6ScopeGroup,
       "ipDefaultRouterGroup": ipDefaultRouterGroup,
       "ipv6RouterAdvertGroup": ipv6RouterAdvertGroup,
       "icmpStatsGroup": icmpStatsGroup}
)
