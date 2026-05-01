# SNMP MIB module (IF-MIB) expressed in pysnmp data model.
#
# This Python module is designed to be imported and executed by the
# pysnmp library.
#
# See https://www.pysnmp.com/pysnmp for further information.
#
# Notes
# -----
# ASN.1 source file://files/mibs/IF-MIB
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

(IANAifType,) = mibBuilder.importSymbols(
    "IANAifType-MIB",
    "IANAifType")

(ModuleCompliance,
 ObjectGroup,
 NotificationGroup) = mibBuilder.importSymbols(
    "SNMPv2-CONF",
    "ModuleCompliance",
    "ObjectGroup",
    "NotificationGroup")

(snmpTraps,) = mibBuilder.importSymbols(
    "SNMPv2-MIB",
    "snmpTraps")

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

(AutonomousType,
 DisplayString,
 TextualConvention,
 TruthValue,
 RowStatus,
 TimeStamp,
 TestAndIncr,
 PhysAddress) = mibBuilder.importSymbols(
    "SNMPv2-TC",
    "AutonomousType",
    "DisplayString",
    "TextualConvention",
    "TruthValue",
    "RowStatus",
    "TimeStamp",
    "TestAndIncr",
    "PhysAddress")


# MODULE-IDENTITY

ifMIB = ModuleIdentity(
    (1, 3, 6, 1, 2, 1, 31)
)
ifMIB.setRevisions(
        ("2000-06-14 00:00",
         "1996-02-28 21:55",
         "1993-11-08 21:55")
)


# Types definitions


# TEXTUAL-CONVENTIONS



class OwnerString(TextualConvention, OctetString):
    status = "deprecated"
    displayHint = "255a"
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 255),
    )



class InterfaceIndex(TextualConvention, Integer32):
    status = "current"
    displayHint = "d"
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(1, 2147483647),
    )



class InterfaceIndexOrZero(TextualConvention, Integer32):
    status = "current"
    displayHint = "d"
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 2147483647),
    )



# MIB Managed Objects in the order of their OIDs

_Interfaces_ObjectIdentity = ObjectIdentity
interfaces = _Interfaces_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 2)
)
_IfNumber_Type = Integer32
_IfNumber_Object = MibScalar
ifNumber = _IfNumber_Object(
    (1, 3, 6, 1, 2, 1, 2, 1),
    _IfNumber_Type()
)
ifNumber.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifNumber.setStatus("current")
_IfTable_Object = MibTable
ifTable = _IfTable_Object(
    (1, 3, 6, 1, 2, 1, 2, 2)
)
if mibBuilder.loadTexts:
    ifTable.setStatus("current")
_IfEntry_Object = MibTableRow
ifEntry = _IfEntry_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1)
)
ifEntry.setIndexNames(
    (0, "IF-MIB", "ifIndex"),
)
if mibBuilder.loadTexts:
    ifEntry.setStatus("current")
_IfIndex_Type = InterfaceIndex
_IfIndex_Object = MibTableColumn
ifIndex = _IfIndex_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 1),
    _IfIndex_Type()
)
ifIndex.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifIndex.setStatus("current")


class _IfDescr_Type(DisplayString):
    """Custom type ifDescr based on DisplayString"""
    subtypeSpec = DisplayString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 255),
    )


_IfDescr_Type.__name__ = "DisplayString"
_IfDescr_Object = MibTableColumn
ifDescr = _IfDescr_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 2),
    _IfDescr_Type()
)
ifDescr.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifDescr.setStatus("current")
_IfType_Type = IANAifType
_IfType_Object = MibTableColumn
ifType = _IfType_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 3),
    _IfType_Type()
)
ifType.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifType.setStatus("current")
_IfMtu_Type = Integer32
_IfMtu_Object = MibTableColumn
ifMtu = _IfMtu_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 4),
    _IfMtu_Type()
)
ifMtu.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifMtu.setStatus("current")
_IfSpeed_Type = Gauge32
_IfSpeed_Object = MibTableColumn
ifSpeed = _IfSpeed_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 5),
    _IfSpeed_Type()
)
ifSpeed.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifSpeed.setStatus("current")
_IfPhysAddress_Type = PhysAddress
_IfPhysAddress_Object = MibTableColumn
ifPhysAddress = _IfPhysAddress_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 6),
    _IfPhysAddress_Type()
)
ifPhysAddress.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifPhysAddress.setStatus("current")


class _IfAdminStatus_Type(Integer32):
    """Custom type ifAdminStatus based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2,
              3)
        )
    )
    namedValues = NamedValues(
        *(("down", 2),
          ("testing", 3),
          ("up", 1))
    )


_IfAdminStatus_Type.__name__ = "Integer32"
_IfAdminStatus_Object = MibTableColumn
ifAdminStatus = _IfAdminStatus_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 7),
    _IfAdminStatus_Type()
)
ifAdminStatus.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ifAdminStatus.setStatus("current")


class _IfOperStatus_Type(Integer32):
    """Custom type ifOperStatus based on Integer32"""
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
        *(("dormant", 5),
          ("down", 2),
          ("lowerLayerDown", 7),
          ("notPresent", 6),
          ("testing", 3),
          ("unknown", 4),
          ("up", 1))
    )


_IfOperStatus_Type.__name__ = "Integer32"
_IfOperStatus_Object = MibTableColumn
ifOperStatus = _IfOperStatus_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 8),
    _IfOperStatus_Type()
)
ifOperStatus.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifOperStatus.setStatus("current")
_IfLastChange_Type = TimeTicks
_IfLastChange_Object = MibTableColumn
ifLastChange = _IfLastChange_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 9),
    _IfLastChange_Type()
)
ifLastChange.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifLastChange.setStatus("current")
_IfInOctets_Type = Counter32
_IfInOctets_Object = MibTableColumn
ifInOctets = _IfInOctets_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 10),
    _IfInOctets_Type()
)
ifInOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifInOctets.setStatus("current")
_IfInUcastPkts_Type = Counter32
_IfInUcastPkts_Object = MibTableColumn
ifInUcastPkts = _IfInUcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 11),
    _IfInUcastPkts_Type()
)
ifInUcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifInUcastPkts.setStatus("current")
_IfInNUcastPkts_Type = Counter32
_IfInNUcastPkts_Object = MibTableColumn
ifInNUcastPkts = _IfInNUcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 12),
    _IfInNUcastPkts_Type()
)
ifInNUcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifInNUcastPkts.setStatus("deprecated")
_IfInDiscards_Type = Counter32
_IfInDiscards_Object = MibTableColumn
ifInDiscards = _IfInDiscards_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 13),
    _IfInDiscards_Type()
)
ifInDiscards.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifInDiscards.setStatus("current")
_IfInErrors_Type = Counter32
_IfInErrors_Object = MibTableColumn
ifInErrors = _IfInErrors_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 14),
    _IfInErrors_Type()
)
ifInErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifInErrors.setStatus("current")
_IfInUnknownProtos_Type = Counter32
_IfInUnknownProtos_Object = MibTableColumn
ifInUnknownProtos = _IfInUnknownProtos_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 15),
    _IfInUnknownProtos_Type()
)
ifInUnknownProtos.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifInUnknownProtos.setStatus("current")
_IfOutOctets_Type = Counter32
_IfOutOctets_Object = MibTableColumn
ifOutOctets = _IfOutOctets_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 16),
    _IfOutOctets_Type()
)
ifOutOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifOutOctets.setStatus("current")
_IfOutUcastPkts_Type = Counter32
_IfOutUcastPkts_Object = MibTableColumn
ifOutUcastPkts = _IfOutUcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 17),
    _IfOutUcastPkts_Type()
)
ifOutUcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifOutUcastPkts.setStatus("current")
_IfOutNUcastPkts_Type = Counter32
_IfOutNUcastPkts_Object = MibTableColumn
ifOutNUcastPkts = _IfOutNUcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 18),
    _IfOutNUcastPkts_Type()
)
ifOutNUcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifOutNUcastPkts.setStatus("deprecated")
_IfOutDiscards_Type = Counter32
_IfOutDiscards_Object = MibTableColumn
ifOutDiscards = _IfOutDiscards_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 19),
    _IfOutDiscards_Type()
)
ifOutDiscards.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifOutDiscards.setStatus("current")
_IfOutErrors_Type = Counter32
_IfOutErrors_Object = MibTableColumn
ifOutErrors = _IfOutErrors_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 20),
    _IfOutErrors_Type()
)
ifOutErrors.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifOutErrors.setStatus("current")
_IfOutQLen_Type = Gauge32
_IfOutQLen_Object = MibTableColumn
ifOutQLen = _IfOutQLen_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 21),
    _IfOutQLen_Type()
)
ifOutQLen.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifOutQLen.setStatus("deprecated")
_IfSpecific_Type = ObjectIdentifier
_IfSpecific_Object = MibTableColumn
ifSpecific = _IfSpecific_Object(
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 22),
    _IfSpecific_Type()
)
ifSpecific.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifSpecific.setStatus("deprecated")
_IfMIBObjects_ObjectIdentity = ObjectIdentity
ifMIBObjects = _IfMIBObjects_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 31, 1)
)
_IfXTable_Object = MibTable
ifXTable = _IfXTable_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1)
)
if mibBuilder.loadTexts:
    ifXTable.setStatus("current")
_IfXEntry_Object = MibTableRow
ifXEntry = _IfXEntry_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1)
)
ifEntry.registerAugmentions(
    ("IF-MIB",
     "ifXEntry")
)
ifXEntry.setIndexNames(*ifEntry.getIndexNames())
if mibBuilder.loadTexts:
    ifXEntry.setStatus("current")
_IfName_Type = DisplayString
_IfName_Object = MibTableColumn
ifName = _IfName_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 1),
    _IfName_Type()
)
ifName.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifName.setStatus("current")
_IfInMulticastPkts_Type = Counter32
_IfInMulticastPkts_Object = MibTableColumn
ifInMulticastPkts = _IfInMulticastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 2),
    _IfInMulticastPkts_Type()
)
ifInMulticastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifInMulticastPkts.setStatus("current")
_IfInBroadcastPkts_Type = Counter32
_IfInBroadcastPkts_Object = MibTableColumn
ifInBroadcastPkts = _IfInBroadcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 3),
    _IfInBroadcastPkts_Type()
)
ifInBroadcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifInBroadcastPkts.setStatus("current")
_IfOutMulticastPkts_Type = Counter32
_IfOutMulticastPkts_Object = MibTableColumn
ifOutMulticastPkts = _IfOutMulticastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 4),
    _IfOutMulticastPkts_Type()
)
ifOutMulticastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifOutMulticastPkts.setStatus("current")
_IfOutBroadcastPkts_Type = Counter32
_IfOutBroadcastPkts_Object = MibTableColumn
ifOutBroadcastPkts = _IfOutBroadcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 5),
    _IfOutBroadcastPkts_Type()
)
ifOutBroadcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifOutBroadcastPkts.setStatus("current")
_IfHCInOctets_Type = Counter64
_IfHCInOctets_Object = MibTableColumn
ifHCInOctets = _IfHCInOctets_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 6),
    _IfHCInOctets_Type()
)
ifHCInOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifHCInOctets.setStatus("current")
_IfHCInUcastPkts_Type = Counter64
_IfHCInUcastPkts_Object = MibTableColumn
ifHCInUcastPkts = _IfHCInUcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 7),
    _IfHCInUcastPkts_Type()
)
ifHCInUcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifHCInUcastPkts.setStatus("current")
_IfHCInMulticastPkts_Type = Counter64
_IfHCInMulticastPkts_Object = MibTableColumn
ifHCInMulticastPkts = _IfHCInMulticastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 8),
    _IfHCInMulticastPkts_Type()
)
ifHCInMulticastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifHCInMulticastPkts.setStatus("current")
_IfHCInBroadcastPkts_Type = Counter64
_IfHCInBroadcastPkts_Object = MibTableColumn
ifHCInBroadcastPkts = _IfHCInBroadcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 9),
    _IfHCInBroadcastPkts_Type()
)
ifHCInBroadcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifHCInBroadcastPkts.setStatus("current")
_IfHCOutOctets_Type = Counter64
_IfHCOutOctets_Object = MibTableColumn
ifHCOutOctets = _IfHCOutOctets_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 10),
    _IfHCOutOctets_Type()
)
ifHCOutOctets.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifHCOutOctets.setStatus("current")
_IfHCOutUcastPkts_Type = Counter64
_IfHCOutUcastPkts_Object = MibTableColumn
ifHCOutUcastPkts = _IfHCOutUcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 11),
    _IfHCOutUcastPkts_Type()
)
ifHCOutUcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifHCOutUcastPkts.setStatus("current")
_IfHCOutMulticastPkts_Type = Counter64
_IfHCOutMulticastPkts_Object = MibTableColumn
ifHCOutMulticastPkts = _IfHCOutMulticastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 12),
    _IfHCOutMulticastPkts_Type()
)
ifHCOutMulticastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifHCOutMulticastPkts.setStatus("current")
_IfHCOutBroadcastPkts_Type = Counter64
_IfHCOutBroadcastPkts_Object = MibTableColumn
ifHCOutBroadcastPkts = _IfHCOutBroadcastPkts_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 13),
    _IfHCOutBroadcastPkts_Type()
)
ifHCOutBroadcastPkts.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifHCOutBroadcastPkts.setStatus("current")


class _IfLinkUpDownTrapEnable_Type(Integer32):
    """Custom type ifLinkUpDownTrapEnable based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2)
        )
    )
    namedValues = NamedValues(
        *(("disabled", 2),
          ("enabled", 1))
    )


_IfLinkUpDownTrapEnable_Type.__name__ = "Integer32"
_IfLinkUpDownTrapEnable_Object = MibTableColumn
ifLinkUpDownTrapEnable = _IfLinkUpDownTrapEnable_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 14),
    _IfLinkUpDownTrapEnable_Type()
)
ifLinkUpDownTrapEnable.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ifLinkUpDownTrapEnable.setStatus("current")
_IfHighSpeed_Type = Gauge32
_IfHighSpeed_Object = MibTableColumn
ifHighSpeed = _IfHighSpeed_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 15),
    _IfHighSpeed_Type()
)
ifHighSpeed.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifHighSpeed.setStatus("current")
_IfPromiscuousMode_Type = TruthValue
_IfPromiscuousMode_Object = MibTableColumn
ifPromiscuousMode = _IfPromiscuousMode_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 16),
    _IfPromiscuousMode_Type()
)
ifPromiscuousMode.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ifPromiscuousMode.setStatus("current")
_IfConnectorPresent_Type = TruthValue
_IfConnectorPresent_Object = MibTableColumn
ifConnectorPresent = _IfConnectorPresent_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 17),
    _IfConnectorPresent_Type()
)
ifConnectorPresent.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifConnectorPresent.setStatus("current")


class _IfAlias_Type(DisplayString):
    """Custom type ifAlias based on DisplayString"""
    subtypeSpec = DisplayString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 64),
    )


_IfAlias_Type.__name__ = "DisplayString"
_IfAlias_Object = MibTableColumn
ifAlias = _IfAlias_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 18),
    _IfAlias_Type()
)
ifAlias.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ifAlias.setStatus("current")
_IfCounterDiscontinuityTime_Type = TimeStamp
_IfCounterDiscontinuityTime_Object = MibTableColumn
ifCounterDiscontinuityTime = _IfCounterDiscontinuityTime_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 19),
    _IfCounterDiscontinuityTime_Type()
)
ifCounterDiscontinuityTime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifCounterDiscontinuityTime.setStatus("current")
_IfStackTable_Object = MibTable
ifStackTable = _IfStackTable_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 2)
)
if mibBuilder.loadTexts:
    ifStackTable.setStatus("current")
_IfStackEntry_Object = MibTableRow
ifStackEntry = _IfStackEntry_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 2, 1)
)
ifStackEntry.setIndexNames(
    (0, "IF-MIB", "ifStackHigherLayer"),
    (0, "IF-MIB", "ifStackLowerLayer"),
)
if mibBuilder.loadTexts:
    ifStackEntry.setStatus("current")
_IfStackHigherLayer_Type = InterfaceIndexOrZero
_IfStackHigherLayer_Object = MibTableColumn
ifStackHigherLayer = _IfStackHigherLayer_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 2, 1, 1),
    _IfStackHigherLayer_Type()
)
ifStackHigherLayer.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ifStackHigherLayer.setStatus("current")
_IfStackLowerLayer_Type = InterfaceIndexOrZero
_IfStackLowerLayer_Object = MibTableColumn
ifStackLowerLayer = _IfStackLowerLayer_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 2, 1, 2),
    _IfStackLowerLayer_Type()
)
ifStackLowerLayer.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ifStackLowerLayer.setStatus("current")
_IfStackStatus_Type = RowStatus
_IfStackStatus_Object = MibTableColumn
ifStackStatus = _IfStackStatus_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 2, 1, 3),
    _IfStackStatus_Type()
)
ifStackStatus.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ifStackStatus.setStatus("current")
_IfTestTable_Object = MibTable
ifTestTable = _IfTestTable_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 3)
)
if mibBuilder.loadTexts:
    ifTestTable.setStatus("deprecated")
_IfTestEntry_Object = MibTableRow
ifTestEntry = _IfTestEntry_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 3, 1)
)
ifEntry.registerAugmentions(
    ("IF-MIB",
     "ifTestEntry")
)
ifTestEntry.setIndexNames(*ifEntry.getIndexNames())
if mibBuilder.loadTexts:
    ifTestEntry.setStatus("deprecated")
_IfTestId_Type = TestAndIncr
_IfTestId_Object = MibTableColumn
ifTestId = _IfTestId_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 3, 1, 1),
    _IfTestId_Type()
)
ifTestId.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ifTestId.setStatus("deprecated")


class _IfTestStatus_Type(Integer32):
    """Custom type ifTestStatus based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2)
        )
    )
    namedValues = NamedValues(
        *(("inUse", 2),
          ("notInUse", 1))
    )


_IfTestStatus_Type.__name__ = "Integer32"
_IfTestStatus_Object = MibTableColumn
ifTestStatus = _IfTestStatus_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 3, 1, 2),
    _IfTestStatus_Type()
)
ifTestStatus.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ifTestStatus.setStatus("deprecated")
_IfTestType_Type = AutonomousType
_IfTestType_Object = MibTableColumn
ifTestType = _IfTestType_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 3, 1, 3),
    _IfTestType_Type()
)
ifTestType.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ifTestType.setStatus("deprecated")


class _IfTestResult_Type(Integer32):
    """Custom type ifTestResult based on Integer32"""
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
        *(("aborted", 6),
          ("failed", 7),
          ("inProgress", 3),
          ("none", 1),
          ("notSupported", 4),
          ("success", 2),
          ("unAbleToRun", 5))
    )


_IfTestResult_Type.__name__ = "Integer32"
_IfTestResult_Object = MibTableColumn
ifTestResult = _IfTestResult_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 3, 1, 4),
    _IfTestResult_Type()
)
ifTestResult.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifTestResult.setStatus("deprecated")
_IfTestCode_Type = ObjectIdentifier
_IfTestCode_Object = MibTableColumn
ifTestCode = _IfTestCode_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 3, 1, 5),
    _IfTestCode_Type()
)
ifTestCode.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifTestCode.setStatus("deprecated")
_IfTestOwner_Type = OwnerString
_IfTestOwner_Object = MibTableColumn
ifTestOwner = _IfTestOwner_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 3, 1, 6),
    _IfTestOwner_Type()
)
ifTestOwner.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    ifTestOwner.setStatus("deprecated")
_IfRcvAddressTable_Object = MibTable
ifRcvAddressTable = _IfRcvAddressTable_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 4)
)
if mibBuilder.loadTexts:
    ifRcvAddressTable.setStatus("current")
_IfRcvAddressEntry_Object = MibTableRow
ifRcvAddressEntry = _IfRcvAddressEntry_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 4, 1)
)
ifRcvAddressEntry.setIndexNames(
    (0, "IF-MIB", "ifIndex"),
    (0, "IF-MIB", "ifRcvAddressAddress"),
)
if mibBuilder.loadTexts:
    ifRcvAddressEntry.setStatus("current")
_IfRcvAddressAddress_Type = PhysAddress
_IfRcvAddressAddress_Object = MibTableColumn
ifRcvAddressAddress = _IfRcvAddressAddress_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 4, 1, 1),
    _IfRcvAddressAddress_Type()
)
ifRcvAddressAddress.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    ifRcvAddressAddress.setStatus("current")
_IfRcvAddressStatus_Type = RowStatus
_IfRcvAddressStatus_Object = MibTableColumn
ifRcvAddressStatus = _IfRcvAddressStatus_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 4, 1, 2),
    _IfRcvAddressStatus_Type()
)
ifRcvAddressStatus.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ifRcvAddressStatus.setStatus("current")


class _IfRcvAddressType_Type(Integer32):
    """Custom type ifRcvAddressType based on Integer32"""
    defaultValue = 2

    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(
            *(1,
              2,
              3)
        )
    )
    namedValues = NamedValues(
        *(("nonVolatile", 3),
          ("other", 1),
          ("volatile", 2))
    )


_IfRcvAddressType_Type.__name__ = "Integer32"
_IfRcvAddressType_Object = MibTableColumn
ifRcvAddressType = _IfRcvAddressType_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 4, 1, 3),
    _IfRcvAddressType_Type()
)
ifRcvAddressType.setMaxAccess("read-create")
if mibBuilder.loadTexts:
    ifRcvAddressType.setStatus("current")
_IfTableLastChange_Type = TimeTicks
_IfTableLastChange_Object = MibScalar
ifTableLastChange = _IfTableLastChange_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 5),
    _IfTableLastChange_Type()
)
ifTableLastChange.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifTableLastChange.setStatus("current")
_IfStackLastChange_Type = TimeTicks
_IfStackLastChange_Object = MibScalar
ifStackLastChange = _IfStackLastChange_Object(
    (1, 3, 6, 1, 2, 1, 31, 1, 6),
    _IfStackLastChange_Type()
)
ifStackLastChange.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    ifStackLastChange.setStatus("current")
_IfConformance_ObjectIdentity = ObjectIdentity
ifConformance = _IfConformance_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 31, 2)
)
_IfGroups_ObjectIdentity = ObjectIdentity
ifGroups = _IfGroups_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 31, 2, 1)
)
_IfCompliances_ObjectIdentity = ObjectIdentity
ifCompliances = _IfCompliances_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 31, 2, 2)
)

# Managed Objects groups

ifGeneralGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 1)
)
ifGeneralGroup.setObjects(
      *(("IF-MIB", "ifDescr"),
        ("IF-MIB", "ifType"),
        ("IF-MIB", "ifSpeed"),
        ("IF-MIB", "ifPhysAddress"),
        ("IF-MIB", "ifAdminStatus"),
        ("IF-MIB", "ifOperStatus"),
        ("IF-MIB", "ifLastChange"),
        ("IF-MIB", "ifLinkUpDownTrapEnable"),
        ("IF-MIB", "ifConnectorPresent"),
        ("IF-MIB", "ifHighSpeed"),
        ("IF-MIB", "ifName"))
)
if mibBuilder.loadTexts:
    ifGeneralGroup.setStatus("deprecated")

ifFixedLengthGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 2)
)
ifFixedLengthGroup.setObjects(
      *(("IF-MIB", "ifInOctets"),
        ("IF-MIB", "ifOutOctets"),
        ("IF-MIB", "ifInUnknownProtos"),
        ("IF-MIB", "ifInErrors"),
        ("IF-MIB", "ifOutErrors"))
)
if mibBuilder.loadTexts:
    ifFixedLengthGroup.setStatus("current")

ifHCFixedLengthGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 3)
)
ifHCFixedLengthGroup.setObjects(
      *(("IF-MIB", "ifHCInOctets"),
        ("IF-MIB", "ifHCOutOctets"),
        ("IF-MIB", "ifInOctets"),
        ("IF-MIB", "ifOutOctets"),
        ("IF-MIB", "ifInUnknownProtos"),
        ("IF-MIB", "ifInErrors"),
        ("IF-MIB", "ifOutErrors"))
)
if mibBuilder.loadTexts:
    ifHCFixedLengthGroup.setStatus("current")

ifPacketGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 4)
)
ifPacketGroup.setObjects(
      *(("IF-MIB", "ifInOctets"),
        ("IF-MIB", "ifOutOctets"),
        ("IF-MIB", "ifInUnknownProtos"),
        ("IF-MIB", "ifInErrors"),
        ("IF-MIB", "ifOutErrors"),
        ("IF-MIB", "ifMtu"),
        ("IF-MIB", "ifInUcastPkts"),
        ("IF-MIB", "ifInMulticastPkts"),
        ("IF-MIB", "ifInBroadcastPkts"),
        ("IF-MIB", "ifInDiscards"),
        ("IF-MIB", "ifOutUcastPkts"),
        ("IF-MIB", "ifOutMulticastPkts"),
        ("IF-MIB", "ifOutBroadcastPkts"),
        ("IF-MIB", "ifOutDiscards"),
        ("IF-MIB", "ifPromiscuousMode"))
)
if mibBuilder.loadTexts:
    ifPacketGroup.setStatus("current")

ifHCPacketGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 5)
)
ifHCPacketGroup.setObjects(
      *(("IF-MIB", "ifHCInOctets"),
        ("IF-MIB", "ifHCOutOctets"),
        ("IF-MIB", "ifInOctets"),
        ("IF-MIB", "ifOutOctets"),
        ("IF-MIB", "ifInUnknownProtos"),
        ("IF-MIB", "ifInErrors"),
        ("IF-MIB", "ifOutErrors"),
        ("IF-MIB", "ifMtu"),
        ("IF-MIB", "ifInUcastPkts"),
        ("IF-MIB", "ifInMulticastPkts"),
        ("IF-MIB", "ifInBroadcastPkts"),
        ("IF-MIB", "ifInDiscards"),
        ("IF-MIB", "ifOutUcastPkts"),
        ("IF-MIB", "ifOutMulticastPkts"),
        ("IF-MIB", "ifOutBroadcastPkts"),
        ("IF-MIB", "ifOutDiscards"),
        ("IF-MIB", "ifPromiscuousMode"))
)
if mibBuilder.loadTexts:
    ifHCPacketGroup.setStatus("current")

ifVHCPacketGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 6)
)
ifVHCPacketGroup.setObjects(
      *(("IF-MIB", "ifHCInUcastPkts"),
        ("IF-MIB", "ifHCInMulticastPkts"),
        ("IF-MIB", "ifHCInBroadcastPkts"),
        ("IF-MIB", "ifHCOutUcastPkts"),
        ("IF-MIB", "ifHCOutMulticastPkts"),
        ("IF-MIB", "ifHCOutBroadcastPkts"),
        ("IF-MIB", "ifHCInOctets"),
        ("IF-MIB", "ifHCOutOctets"),
        ("IF-MIB", "ifInOctets"),
        ("IF-MIB", "ifOutOctets"),
        ("IF-MIB", "ifInUnknownProtos"),
        ("IF-MIB", "ifInErrors"),
        ("IF-MIB", "ifOutErrors"),
        ("IF-MIB", "ifMtu"),
        ("IF-MIB", "ifInUcastPkts"),
        ("IF-MIB", "ifInMulticastPkts"),
        ("IF-MIB", "ifInBroadcastPkts"),
        ("IF-MIB", "ifInDiscards"),
        ("IF-MIB", "ifOutUcastPkts"),
        ("IF-MIB", "ifOutMulticastPkts"),
        ("IF-MIB", "ifOutBroadcastPkts"),
        ("IF-MIB", "ifOutDiscards"),
        ("IF-MIB", "ifPromiscuousMode"))
)
if mibBuilder.loadTexts:
    ifVHCPacketGroup.setStatus("current")

ifRcvAddressGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 7)
)
ifRcvAddressGroup.setObjects(
      *(("IF-MIB", "ifRcvAddressStatus"),
        ("IF-MIB", "ifRcvAddressType"))
)
if mibBuilder.loadTexts:
    ifRcvAddressGroup.setStatus("current")

ifTestGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 8)
)
ifTestGroup.setObjects(
      *(("IF-MIB", "ifTestId"),
        ("IF-MIB", "ifTestStatus"),
        ("IF-MIB", "ifTestType"),
        ("IF-MIB", "ifTestResult"),
        ("IF-MIB", "ifTestCode"),
        ("IF-MIB", "ifTestOwner"))
)
if mibBuilder.loadTexts:
    ifTestGroup.setStatus("deprecated")

ifStackGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 9)
)
ifStackGroup.setObjects(
    ("IF-MIB", "ifStackStatus")
)
if mibBuilder.loadTexts:
    ifStackGroup.setStatus("deprecated")

ifGeneralInformationGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 10)
)
ifGeneralInformationGroup.setObjects(
      *(("IF-MIB", "ifIndex"),
        ("IF-MIB", "ifDescr"),
        ("IF-MIB", "ifType"),
        ("IF-MIB", "ifSpeed"),
        ("IF-MIB", "ifPhysAddress"),
        ("IF-MIB", "ifAdminStatus"),
        ("IF-MIB", "ifOperStatus"),
        ("IF-MIB", "ifLastChange"),
        ("IF-MIB", "ifLinkUpDownTrapEnable"),
        ("IF-MIB", "ifConnectorPresent"),
        ("IF-MIB", "ifHighSpeed"),
        ("IF-MIB", "ifName"),
        ("IF-MIB", "ifNumber"),
        ("IF-MIB", "ifAlias"),
        ("IF-MIB", "ifTableLastChange"))
)
if mibBuilder.loadTexts:
    ifGeneralInformationGroup.setStatus("current")

ifStackGroup2 = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 11)
)
ifStackGroup2.setObjects(
      *(("IF-MIB", "ifStackStatus"),
        ("IF-MIB", "ifStackLastChange"))
)
if mibBuilder.loadTexts:
    ifStackGroup2.setStatus("current")

ifOldObjectsGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 12)
)
ifOldObjectsGroup.setObjects(
      *(("IF-MIB", "ifInNUcastPkts"),
        ("IF-MIB", "ifOutNUcastPkts"),
        ("IF-MIB", "ifOutQLen"),
        ("IF-MIB", "ifSpecific"))
)
if mibBuilder.loadTexts:
    ifOldObjectsGroup.setStatus("deprecated")

ifCounterDiscontinuityGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 13)
)
ifCounterDiscontinuityGroup.setObjects(
    ("IF-MIB", "ifCounterDiscontinuityTime")
)
if mibBuilder.loadTexts:
    ifCounterDiscontinuityGroup.setStatus("current")


# Notification objects

linkDown = NotificationType(
    (1, 3, 6, 1, 6, 3, 1, 1, 5, 3)
)
linkDown.setObjects(
      *(("IF-MIB", "ifIndex"),
        ("IF-MIB", "ifAdminStatus"),
        ("IF-MIB", "ifOperStatus"))
)
if mibBuilder.loadTexts:
    linkDown.setStatus(
        "current"
    )

linkUp = NotificationType(
    (1, 3, 6, 1, 6, 3, 1, 1, 5, 4)
)
linkUp.setObjects(
      *(("IF-MIB", "ifIndex"),
        ("IF-MIB", "ifAdminStatus"),
        ("IF-MIB", "ifOperStatus"))
)
if mibBuilder.loadTexts:
    linkUp.setStatus(
        "current"
    )


# Notifications groups

linkUpDownNotificationsGroup = NotificationGroup(
    (1, 3, 6, 1, 2, 1, 31, 2, 1, 14)
)
linkUpDownNotificationsGroup.setObjects(
      *(("IF-MIB", "linkUp"),
        ("IF-MIB", "linkDown"))
)
if mibBuilder.loadTexts:
    linkUpDownNotificationsGroup.setStatus(
        "current"
    )


# Agent capabilities


# Module compliance

ifCompliance = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 31, 2, 2, 1)
)
if mibBuilder.loadTexts:
    ifCompliance.setStatus(
        "deprecated"
    )

ifCompliance2 = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 31, 2, 2, 2)
)
if mibBuilder.loadTexts:
    ifCompliance2.setStatus(
        "deprecated"
    )

ifCompliance3 = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 31, 2, 2, 3)
)
if mibBuilder.loadTexts:
    ifCompliance3.setStatus(
        "current"
    )


# Export all MIB objects to the MIB builder

mibBuilder.exportSymbols(
    "IF-MIB",
    **{"OwnerString": OwnerString,
       "InterfaceIndex": InterfaceIndex,
       "InterfaceIndexOrZero": InterfaceIndexOrZero,
       "interfaces": interfaces,
       "ifNumber": ifNumber,
       "ifTable": ifTable,
       "ifEntry": ifEntry,
       "ifIndex": ifIndex,
       "ifDescr": ifDescr,
       "ifType": ifType,
       "ifMtu": ifMtu,
       "ifSpeed": ifSpeed,
       "ifPhysAddress": ifPhysAddress,
       "ifAdminStatus": ifAdminStatus,
       "ifOperStatus": ifOperStatus,
       "ifLastChange": ifLastChange,
       "ifInOctets": ifInOctets,
       "ifInUcastPkts": ifInUcastPkts,
       "ifInNUcastPkts": ifInNUcastPkts,
       "ifInDiscards": ifInDiscards,
       "ifInErrors": ifInErrors,
       "ifInUnknownProtos": ifInUnknownProtos,
       "ifOutOctets": ifOutOctets,
       "ifOutUcastPkts": ifOutUcastPkts,
       "ifOutNUcastPkts": ifOutNUcastPkts,
       "ifOutDiscards": ifOutDiscards,
       "ifOutErrors": ifOutErrors,
       "ifOutQLen": ifOutQLen,
       "ifSpecific": ifSpecific,
       "ifMIB": ifMIB,
       "ifMIBObjects": ifMIBObjects,
       "ifXTable": ifXTable,
       "ifXEntry": ifXEntry,
       "ifName": ifName,
       "ifInMulticastPkts": ifInMulticastPkts,
       "ifInBroadcastPkts": ifInBroadcastPkts,
       "ifOutMulticastPkts": ifOutMulticastPkts,
       "ifOutBroadcastPkts": ifOutBroadcastPkts,
       "ifHCInOctets": ifHCInOctets,
       "ifHCInUcastPkts": ifHCInUcastPkts,
       "ifHCInMulticastPkts": ifHCInMulticastPkts,
       "ifHCInBroadcastPkts": ifHCInBroadcastPkts,
       "ifHCOutOctets": ifHCOutOctets,
       "ifHCOutUcastPkts": ifHCOutUcastPkts,
       "ifHCOutMulticastPkts": ifHCOutMulticastPkts,
       "ifHCOutBroadcastPkts": ifHCOutBroadcastPkts,
       "ifLinkUpDownTrapEnable": ifLinkUpDownTrapEnable,
       "ifHighSpeed": ifHighSpeed,
       "ifPromiscuousMode": ifPromiscuousMode,
       "ifConnectorPresent": ifConnectorPresent,
       "ifAlias": ifAlias,
       "ifCounterDiscontinuityTime": ifCounterDiscontinuityTime,
       "ifStackTable": ifStackTable,
       "ifStackEntry": ifStackEntry,
       "ifStackHigherLayer": ifStackHigherLayer,
       "ifStackLowerLayer": ifStackLowerLayer,
       "ifStackStatus": ifStackStatus,
       "ifTestTable": ifTestTable,
       "ifTestEntry": ifTestEntry,
       "ifTestId": ifTestId,
       "ifTestStatus": ifTestStatus,
       "ifTestType": ifTestType,
       "ifTestResult": ifTestResult,
       "ifTestCode": ifTestCode,
       "ifTestOwner": ifTestOwner,
       "ifRcvAddressTable": ifRcvAddressTable,
       "ifRcvAddressEntry": ifRcvAddressEntry,
       "ifRcvAddressAddress": ifRcvAddressAddress,
       "ifRcvAddressStatus": ifRcvAddressStatus,
       "ifRcvAddressType": ifRcvAddressType,
       "ifTableLastChange": ifTableLastChange,
       "ifStackLastChange": ifStackLastChange,
       "ifConformance": ifConformance,
       "ifGroups": ifGroups,
       "ifGeneralGroup": ifGeneralGroup,
       "ifFixedLengthGroup": ifFixedLengthGroup,
       "ifHCFixedLengthGroup": ifHCFixedLengthGroup,
       "ifPacketGroup": ifPacketGroup,
       "ifHCPacketGroup": ifHCPacketGroup,
       "ifVHCPacketGroup": ifVHCPacketGroup,
       "ifRcvAddressGroup": ifRcvAddressGroup,
       "ifTestGroup": ifTestGroup,
       "ifStackGroup": ifStackGroup,
       "ifGeneralInformationGroup": ifGeneralInformationGroup,
       "ifStackGroup2": ifStackGroup2,
       "ifOldObjectsGroup": ifOldObjectsGroup,
       "ifCounterDiscontinuityGroup": ifCounterDiscontinuityGroup,
       "linkUpDownNotificationsGroup": linkUpDownNotificationsGroup,
       "ifCompliances": ifCompliances,
       "ifCompliance": ifCompliance,
       "ifCompliance2": ifCompliance2,
       "ifCompliance3": ifCompliance3,
       "linkDown": linkDown,
       "linkUp": linkUp}
)
