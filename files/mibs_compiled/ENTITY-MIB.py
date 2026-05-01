# SNMP MIB module (ENTITY-MIB) expressed in pysnmp data model.
#
# This Python module is designed to be imported and executed by the
# pysnmp library.
#
# See https://www.pysnmp.com/pysnmp for further information.
#
# Notes
# -----
# ASN.1 source file://files/mibs/ENTITY-MIB
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

(IANAPhysicalClass,) = mibBuilder.importSymbols(
    "IANA-ENTITY-MIB",
    "IANAPhysicalClass")

(SnmpAdminString,) = mibBuilder.importSymbols(
    "SNMP-FRAMEWORK-MIB",
    "SnmpAdminString")

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

(AutonomousType,
 DisplayString,
 TAddress,
 TDomain,
 TextualConvention,
 TruthValue,
 DateAndTime,
 TimeStamp,
 RowPointer) = mibBuilder.importSymbols(
    "SNMPv2-TC",
    "AutonomousType",
    "DisplayString",
    "TAddress",
    "TDomain",
    "TextualConvention",
    "TruthValue",
    "DateAndTime",
    "TimeStamp",
    "RowPointer")

(UUIDorZero,) = mibBuilder.importSymbols(
    "UUID-TC-MIB",
    "UUIDorZero")


# MODULE-IDENTITY

entityMIB = ModuleIdentity(
    (1, 3, 6, 1, 2, 1, 47)
)
entityMIB.setRevisions(
        ("2013-04-05 00:00",
         "2005-08-10 00:00",
         "1999-12-07 00:00",
         "1996-10-31 00:00")
)


# Types definitions


# TEXTUAL-CONVENTIONS



class PhysicalIndex(TextualConvention, Integer32):
    status = "current"
    displayHint = "d"
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(1, 2147483647),
    )



class PhysicalIndexOrZero(TextualConvention, Integer32):
    status = "current"
    displayHint = "d"
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 2147483647),
    )



class SnmpEngineIdOrNone(TextualConvention, OctetString):
    status = "current"
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 32),
    )



class PhysicalClass(TextualConvention, Integer32):
    status = "deprecated"
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
        *(("backplane", 4),
          ("chassis", 3),
          ("container", 5),
          ("cpu", 12),
          ("fan", 7),
          ("module", 9),
          ("other", 1),
          ("port", 10),
          ("powerSupply", 6),
          ("sensor", 8),
          ("stack", 11),
          ("unknown", 2))
    )



# MIB Managed Objects in the order of their OIDs

_EntityMIBObjects_ObjectIdentity = ObjectIdentity
entityMIBObjects = _EntityMIBObjects_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 1)
)
_EntityPhysical_ObjectIdentity = ObjectIdentity
entityPhysical = _EntityPhysical_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 1, 1)
)
_EntPhysicalTable_Object = MibTable
entPhysicalTable = _EntPhysicalTable_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1)
)
if mibBuilder.loadTexts:
    entPhysicalTable.setStatus("current")
_EntPhysicalEntry_Object = MibTableRow
entPhysicalEntry = _EntPhysicalEntry_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1)
)
entPhysicalEntry.setIndexNames(
    (0, "ENTITY-MIB", "entPhysicalIndex"),
)
if mibBuilder.loadTexts:
    entPhysicalEntry.setStatus("current")
_EntPhysicalIndex_Type = PhysicalIndex
_EntPhysicalIndex_Object = MibTableColumn
entPhysicalIndex = _EntPhysicalIndex_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 1),
    _EntPhysicalIndex_Type()
)
entPhysicalIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    entPhysicalIndex.setStatus("current")
_EntPhysicalDescr_Type = SnmpAdminString
_EntPhysicalDescr_Object = MibTableColumn
entPhysicalDescr = _EntPhysicalDescr_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 2),
    _EntPhysicalDescr_Type()
)
entPhysicalDescr.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalDescr.setStatus("current")
_EntPhysicalVendorType_Type = AutonomousType
_EntPhysicalVendorType_Object = MibTableColumn
entPhysicalVendorType = _EntPhysicalVendorType_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 3),
    _EntPhysicalVendorType_Type()
)
entPhysicalVendorType.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalVendorType.setStatus("current")
_EntPhysicalContainedIn_Type = PhysicalIndexOrZero
_EntPhysicalContainedIn_Object = MibTableColumn
entPhysicalContainedIn = _EntPhysicalContainedIn_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 4),
    _EntPhysicalContainedIn_Type()
)
entPhysicalContainedIn.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalContainedIn.setStatus("current")
_EntPhysicalClass_Type = IANAPhysicalClass
_EntPhysicalClass_Object = MibTableColumn
entPhysicalClass = _EntPhysicalClass_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 5),
    _EntPhysicalClass_Type()
)
entPhysicalClass.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalClass.setStatus("current")


class _EntPhysicalParentRelPos_Type(Integer32):
    """Custom type entPhysicalParentRelPos based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(-1, 2147483647),
    )


_EntPhysicalParentRelPos_Type.__name__ = "Integer32"
_EntPhysicalParentRelPos_Object = MibTableColumn
entPhysicalParentRelPos = _EntPhysicalParentRelPos_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 6),
    _EntPhysicalParentRelPos_Type()
)
entPhysicalParentRelPos.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalParentRelPos.setStatus("current")
_EntPhysicalName_Type = SnmpAdminString
_EntPhysicalName_Object = MibTableColumn
entPhysicalName = _EntPhysicalName_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 7),
    _EntPhysicalName_Type()
)
entPhysicalName.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalName.setStatus("current")
_EntPhysicalHardwareRev_Type = SnmpAdminString
_EntPhysicalHardwareRev_Object = MibTableColumn
entPhysicalHardwareRev = _EntPhysicalHardwareRev_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 8),
    _EntPhysicalHardwareRev_Type()
)
entPhysicalHardwareRev.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalHardwareRev.setStatus("current")
_EntPhysicalFirmwareRev_Type = SnmpAdminString
_EntPhysicalFirmwareRev_Object = MibTableColumn
entPhysicalFirmwareRev = _EntPhysicalFirmwareRev_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 9),
    _EntPhysicalFirmwareRev_Type()
)
entPhysicalFirmwareRev.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalFirmwareRev.setStatus("current")
_EntPhysicalSoftwareRev_Type = SnmpAdminString
_EntPhysicalSoftwareRev_Object = MibTableColumn
entPhysicalSoftwareRev = _EntPhysicalSoftwareRev_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 10),
    _EntPhysicalSoftwareRev_Type()
)
entPhysicalSoftwareRev.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalSoftwareRev.setStatus("current")


class _EntPhysicalSerialNum_Type(SnmpAdminString):
    """Custom type entPhysicalSerialNum based on SnmpAdminString"""
    subtypeSpec = SnmpAdminString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 32),
    )


_EntPhysicalSerialNum_Type.__name__ = "SnmpAdminString"
_EntPhysicalSerialNum_Object = MibTableColumn
entPhysicalSerialNum = _EntPhysicalSerialNum_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 11),
    _EntPhysicalSerialNum_Type()
)
entPhysicalSerialNum.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    entPhysicalSerialNum.setStatus("current")
_EntPhysicalMfgName_Type = SnmpAdminString
_EntPhysicalMfgName_Object = MibTableColumn
entPhysicalMfgName = _EntPhysicalMfgName_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 12),
    _EntPhysicalMfgName_Type()
)
entPhysicalMfgName.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalMfgName.setStatus("current")
_EntPhysicalModelName_Type = SnmpAdminString
_EntPhysicalModelName_Object = MibTableColumn
entPhysicalModelName = _EntPhysicalModelName_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 13),
    _EntPhysicalModelName_Type()
)
entPhysicalModelName.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalModelName.setStatus("current")


class _EntPhysicalAlias_Type(SnmpAdminString):
    """Custom type entPhysicalAlias based on SnmpAdminString"""
    subtypeSpec = SnmpAdminString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 32),
    )


_EntPhysicalAlias_Type.__name__ = "SnmpAdminString"
_EntPhysicalAlias_Object = MibTableColumn
entPhysicalAlias = _EntPhysicalAlias_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 14),
    _EntPhysicalAlias_Type()
)
entPhysicalAlias.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    entPhysicalAlias.setStatus("current")


class _EntPhysicalAssetID_Type(SnmpAdminString):
    """Custom type entPhysicalAssetID based on SnmpAdminString"""
    subtypeSpec = SnmpAdminString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 32),
    )


_EntPhysicalAssetID_Type.__name__ = "SnmpAdminString"
_EntPhysicalAssetID_Object = MibTableColumn
entPhysicalAssetID = _EntPhysicalAssetID_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 15),
    _EntPhysicalAssetID_Type()
)
entPhysicalAssetID.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    entPhysicalAssetID.setStatus("current")
_EntPhysicalIsFRU_Type = TruthValue
_EntPhysicalIsFRU_Object = MibTableColumn
entPhysicalIsFRU = _EntPhysicalIsFRU_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 16),
    _EntPhysicalIsFRU_Type()
)
entPhysicalIsFRU.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalIsFRU.setStatus("current")
_EntPhysicalMfgDate_Type = DateAndTime
_EntPhysicalMfgDate_Object = MibTableColumn
entPhysicalMfgDate = _EntPhysicalMfgDate_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 17),
    _EntPhysicalMfgDate_Type()
)
entPhysicalMfgDate.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalMfgDate.setStatus("current")
_EntPhysicalUris_Type = OctetString
_EntPhysicalUris_Object = MibTableColumn
entPhysicalUris = _EntPhysicalUris_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 18),
    _EntPhysicalUris_Type()
)
entPhysicalUris.setMaxAccess("read-write")
if mibBuilder.loadTexts:
    entPhysicalUris.setStatus("current")
_EntPhysicalUUID_Type = UUIDorZero
_EntPhysicalUUID_Object = MibTableColumn
entPhysicalUUID = _EntPhysicalUUID_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 19),
    _EntPhysicalUUID_Type()
)
entPhysicalUUID.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalUUID.setStatus("current")
_EntityLogical_ObjectIdentity = ObjectIdentity
entityLogical = _EntityLogical_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 1, 2)
)
_EntLogicalTable_Object = MibTable
entLogicalTable = _EntLogicalTable_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1)
)
if mibBuilder.loadTexts:
    entLogicalTable.setStatus("current")
_EntLogicalEntry_Object = MibTableRow
entLogicalEntry = _EntLogicalEntry_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1, 1)
)
entLogicalEntry.setIndexNames(
    (0, "ENTITY-MIB", "entLogicalIndex"),
)
if mibBuilder.loadTexts:
    entLogicalEntry.setStatus("current")


class _EntLogicalIndex_Type(Integer32):
    """Custom type entLogicalIndex based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(1, 2147483647),
    )


_EntLogicalIndex_Type.__name__ = "Integer32"
_EntLogicalIndex_Object = MibTableColumn
entLogicalIndex = _EntLogicalIndex_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1, 1, 1),
    _EntLogicalIndex_Type()
)
entLogicalIndex.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    entLogicalIndex.setStatus("current")
_EntLogicalDescr_Type = SnmpAdminString
_EntLogicalDescr_Object = MibTableColumn
entLogicalDescr = _EntLogicalDescr_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1, 1, 2),
    _EntLogicalDescr_Type()
)
entLogicalDescr.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entLogicalDescr.setStatus("current")
_EntLogicalType_Type = AutonomousType
_EntLogicalType_Object = MibTableColumn
entLogicalType = _EntLogicalType_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1, 1, 3),
    _EntLogicalType_Type()
)
entLogicalType.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entLogicalType.setStatus("current")


class _EntLogicalCommunity_Type(OctetString):
    """Custom type entLogicalCommunity based on OctetString"""
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 255),
    )


_EntLogicalCommunity_Type.__name__ = "OctetString"
_EntLogicalCommunity_Object = MibTableColumn
entLogicalCommunity = _EntLogicalCommunity_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1, 1, 4),
    _EntLogicalCommunity_Type()
)
entLogicalCommunity.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entLogicalCommunity.setStatus("deprecated")
_EntLogicalTAddress_Type = TAddress
_EntLogicalTAddress_Object = MibTableColumn
entLogicalTAddress = _EntLogicalTAddress_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1, 1, 5),
    _EntLogicalTAddress_Type()
)
entLogicalTAddress.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entLogicalTAddress.setStatus("current")
_EntLogicalTDomain_Type = TDomain
_EntLogicalTDomain_Object = MibTableColumn
entLogicalTDomain = _EntLogicalTDomain_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1, 1, 6),
    _EntLogicalTDomain_Type()
)
entLogicalTDomain.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entLogicalTDomain.setStatus("current")
_EntLogicalContextEngineID_Type = SnmpEngineIdOrNone
_EntLogicalContextEngineID_Object = MibTableColumn
entLogicalContextEngineID = _EntLogicalContextEngineID_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1, 1, 7),
    _EntLogicalContextEngineID_Type()
)
entLogicalContextEngineID.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entLogicalContextEngineID.setStatus("current")
_EntLogicalContextName_Type = SnmpAdminString
_EntLogicalContextName_Object = MibTableColumn
entLogicalContextName = _EntLogicalContextName_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 2, 1, 1, 8),
    _EntLogicalContextName_Type()
)
entLogicalContextName.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entLogicalContextName.setStatus("current")
_EntityMapping_ObjectIdentity = ObjectIdentity
entityMapping = _EntityMapping_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 1, 3)
)
_EntLPMappingTable_Object = MibTable
entLPMappingTable = _EntLPMappingTable_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 1)
)
if mibBuilder.loadTexts:
    entLPMappingTable.setStatus("current")
_EntLPMappingEntry_Object = MibTableRow
entLPMappingEntry = _EntLPMappingEntry_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 1, 1)
)
entLPMappingEntry.setIndexNames(
    (0, "ENTITY-MIB", "entLogicalIndex"),
    (0, "ENTITY-MIB", "entLPPhysicalIndex"),
)
if mibBuilder.loadTexts:
    entLPMappingEntry.setStatus("current")
_EntLPPhysicalIndex_Type = PhysicalIndex
_EntLPPhysicalIndex_Object = MibTableColumn
entLPPhysicalIndex = _EntLPPhysicalIndex_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 1, 1, 1),
    _EntLPPhysicalIndex_Type()
)
entLPPhysicalIndex.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entLPPhysicalIndex.setStatus("current")
_EntAliasMappingTable_Object = MibTable
entAliasMappingTable = _EntAliasMappingTable_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 2)
)
if mibBuilder.loadTexts:
    entAliasMappingTable.setStatus("current")
_EntAliasMappingEntry_Object = MibTableRow
entAliasMappingEntry = _EntAliasMappingEntry_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 2, 1)
)
entAliasMappingEntry.setIndexNames(
    (0, "ENTITY-MIB", "entPhysicalIndex"),
    (0, "ENTITY-MIB", "entAliasLogicalIndexOrZero"),
)
if mibBuilder.loadTexts:
    entAliasMappingEntry.setStatus("current")


class _EntAliasLogicalIndexOrZero_Type(Integer32):
    """Custom type entAliasLogicalIndexOrZero based on Integer32"""
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 2147483647),
    )


_EntAliasLogicalIndexOrZero_Type.__name__ = "Integer32"
_EntAliasLogicalIndexOrZero_Object = MibTableColumn
entAliasLogicalIndexOrZero = _EntAliasLogicalIndexOrZero_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 2, 1, 1),
    _EntAliasLogicalIndexOrZero_Type()
)
entAliasLogicalIndexOrZero.setMaxAccess("not-accessible")
if mibBuilder.loadTexts:
    entAliasLogicalIndexOrZero.setStatus("current")
_EntAliasMappingIdentifier_Type = RowPointer
_EntAliasMappingIdentifier_Object = MibTableColumn
entAliasMappingIdentifier = _EntAliasMappingIdentifier_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 2, 1, 2),
    _EntAliasMappingIdentifier_Type()
)
entAliasMappingIdentifier.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entAliasMappingIdentifier.setStatus("current")
_EntPhysicalContainsTable_Object = MibTable
entPhysicalContainsTable = _EntPhysicalContainsTable_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 3)
)
if mibBuilder.loadTexts:
    entPhysicalContainsTable.setStatus("current")
_EntPhysicalContainsEntry_Object = MibTableRow
entPhysicalContainsEntry = _EntPhysicalContainsEntry_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 3, 1)
)
entPhysicalContainsEntry.setIndexNames(
    (0, "ENTITY-MIB", "entPhysicalIndex"),
    (0, "ENTITY-MIB", "entPhysicalChildIndex"),
)
if mibBuilder.loadTexts:
    entPhysicalContainsEntry.setStatus("current")
_EntPhysicalChildIndex_Type = PhysicalIndex
_EntPhysicalChildIndex_Object = MibTableColumn
entPhysicalChildIndex = _EntPhysicalChildIndex_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 3, 3, 1, 1),
    _EntPhysicalChildIndex_Type()
)
entPhysicalChildIndex.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entPhysicalChildIndex.setStatus("current")
_EntityGeneral_ObjectIdentity = ObjectIdentity
entityGeneral = _EntityGeneral_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 1, 4)
)
_EntLastChangeTime_Type = TimeStamp
_EntLastChangeTime_Object = MibScalar
entLastChangeTime = _EntLastChangeTime_Object(
    (1, 3, 6, 1, 2, 1, 47, 1, 4, 1),
    _EntLastChangeTime_Type()
)
entLastChangeTime.setMaxAccess("read-only")
if mibBuilder.loadTexts:
    entLastChangeTime.setStatus("current")
_EntityMIBTraps_ObjectIdentity = ObjectIdentity
entityMIBTraps = _EntityMIBTraps_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 2)
)
_EntityMIBTrapPrefix_ObjectIdentity = ObjectIdentity
entityMIBTrapPrefix = _EntityMIBTrapPrefix_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 2, 0)
)
_EntityConformance_ObjectIdentity = ObjectIdentity
entityConformance = _EntityConformance_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 3)
)
_EntityCompliances_ObjectIdentity = ObjectIdentity
entityCompliances = _EntityCompliances_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 3, 1)
)
_EntityGroups_ObjectIdentity = ObjectIdentity
entityGroups = _EntityGroups_ObjectIdentity(
    (1, 3, 6, 1, 2, 1, 47, 3, 2)
)

# Managed Objects groups

entityPhysicalGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 1)
)
entityPhysicalGroup.setObjects(
      *(("ENTITY-MIB", "entPhysicalDescr"),
        ("ENTITY-MIB", "entPhysicalVendorType"),
        ("ENTITY-MIB", "entPhysicalContainedIn"),
        ("ENTITY-MIB", "entPhysicalClass"),
        ("ENTITY-MIB", "entPhysicalParentRelPos"),
        ("ENTITY-MIB", "entPhysicalName"))
)
if mibBuilder.loadTexts:
    entityPhysicalGroup.setStatus("current")

entityLogicalGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 2)
)
entityLogicalGroup.setObjects(
      *(("ENTITY-MIB", "entLogicalDescr"),
        ("ENTITY-MIB", "entLogicalType"),
        ("ENTITY-MIB", "entLogicalCommunity"),
        ("ENTITY-MIB", "entLogicalTAddress"),
        ("ENTITY-MIB", "entLogicalTDomain"))
)
if mibBuilder.loadTexts:
    entityLogicalGroup.setStatus("deprecated")

entityMappingGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 3)
)
entityMappingGroup.setObjects(
      *(("ENTITY-MIB", "entLPPhysicalIndex"),
        ("ENTITY-MIB", "entAliasMappingIdentifier"),
        ("ENTITY-MIB", "entPhysicalChildIndex"))
)
if mibBuilder.loadTexts:
    entityMappingGroup.setStatus("current")

entityGeneralGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 4)
)
entityGeneralGroup.setObjects(
    ("ENTITY-MIB", "entLastChangeTime")
)
if mibBuilder.loadTexts:
    entityGeneralGroup.setStatus("current")

entityPhysical2Group = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 6)
)
entityPhysical2Group.setObjects(
      *(("ENTITY-MIB", "entPhysicalHardwareRev"),
        ("ENTITY-MIB", "entPhysicalFirmwareRev"),
        ("ENTITY-MIB", "entPhysicalSoftwareRev"),
        ("ENTITY-MIB", "entPhysicalSerialNum"),
        ("ENTITY-MIB", "entPhysicalMfgName"),
        ("ENTITY-MIB", "entPhysicalModelName"),
        ("ENTITY-MIB", "entPhysicalAlias"),
        ("ENTITY-MIB", "entPhysicalAssetID"),
        ("ENTITY-MIB", "entPhysicalIsFRU"))
)
if mibBuilder.loadTexts:
    entityPhysical2Group.setStatus("current")

entityLogical2Group = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 7)
)
entityLogical2Group.setObjects(
      *(("ENTITY-MIB", "entLogicalDescr"),
        ("ENTITY-MIB", "entLogicalType"),
        ("ENTITY-MIB", "entLogicalTAddress"),
        ("ENTITY-MIB", "entLogicalTDomain"),
        ("ENTITY-MIB", "entLogicalContextEngineID"),
        ("ENTITY-MIB", "entLogicalContextName"))
)
if mibBuilder.loadTexts:
    entityLogical2Group.setStatus("current")

entityPhysical3Group = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 8)
)
entityPhysical3Group.setObjects(
      *(("ENTITY-MIB", "entPhysicalMfgDate"),
        ("ENTITY-MIB", "entPhysicalUris"))
)
if mibBuilder.loadTexts:
    entityPhysical3Group.setStatus("current")

entityPhysical4Group = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 9)
)
entityPhysical4Group.setObjects(
    ("ENTITY-MIB", "entPhysicalUUID")
)
if mibBuilder.loadTexts:
    entityPhysical4Group.setStatus("current")

entityPhysicalCRGroup = ObjectGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 10)
)
entityPhysicalCRGroup.setObjects(
      *(("ENTITY-MIB", "entPhysicalClass"),
        ("ENTITY-MIB", "entPhysicalName"))
)
if mibBuilder.loadTexts:
    entityPhysicalCRGroup.setStatus("current")


# Notification objects

entConfigChange = NotificationType(
    (1, 3, 6, 1, 2, 1, 47, 2, 0, 1)
)
if mibBuilder.loadTexts:
    entConfigChange.setStatus(
        "current"
    )


# Notifications groups

entityNotificationsGroup = NotificationGroup(
    (1, 3, 6, 1, 2, 1, 47, 3, 2, 5)
)
entityNotificationsGroup.setObjects(
    ("ENTITY-MIB", "entConfigChange")
)
if mibBuilder.loadTexts:
    entityNotificationsGroup.setStatus(
        "current"
    )


# Agent capabilities


# Module compliance

entityCompliance = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 47, 3, 1, 1)
)
if mibBuilder.loadTexts:
    entityCompliance.setStatus(
        "deprecated"
    )

entity2Compliance = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 47, 3, 1, 2)
)
if mibBuilder.loadTexts:
    entity2Compliance.setStatus(
        "deprecated"
    )

entity3Compliance = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 47, 3, 1, 3)
)
if mibBuilder.loadTexts:
    entity3Compliance.setStatus(
        "deprecated"
    )

entity4Compliance = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 47, 3, 1, 4)
)
if mibBuilder.loadTexts:
    entity4Compliance.setStatus(
        "current"
    )

entity4CRCompliance = ModuleCompliance(
    (1, 3, 6, 1, 2, 1, 47, 3, 1, 5)
)
if mibBuilder.loadTexts:
    entity4CRCompliance.setStatus(
        "current"
    )


# Export all MIB objects to the MIB builder

mibBuilder.exportSymbols(
    "ENTITY-MIB",
    **{"PhysicalIndex": PhysicalIndex,
       "PhysicalIndexOrZero": PhysicalIndexOrZero,
       "SnmpEngineIdOrNone": SnmpEngineIdOrNone,
       "PhysicalClass": PhysicalClass,
       "entityMIB": entityMIB,
       "entityMIBObjects": entityMIBObjects,
       "entityPhysical": entityPhysical,
       "entPhysicalTable": entPhysicalTable,
       "entPhysicalEntry": entPhysicalEntry,
       "entPhysicalIndex": entPhysicalIndex,
       "entPhysicalDescr": entPhysicalDescr,
       "entPhysicalVendorType": entPhysicalVendorType,
       "entPhysicalContainedIn": entPhysicalContainedIn,
       "entPhysicalClass": entPhysicalClass,
       "entPhysicalParentRelPos": entPhysicalParentRelPos,
       "entPhysicalName": entPhysicalName,
       "entPhysicalHardwareRev": entPhysicalHardwareRev,
       "entPhysicalFirmwareRev": entPhysicalFirmwareRev,
       "entPhysicalSoftwareRev": entPhysicalSoftwareRev,
       "entPhysicalSerialNum": entPhysicalSerialNum,
       "entPhysicalMfgName": entPhysicalMfgName,
       "entPhysicalModelName": entPhysicalModelName,
       "entPhysicalAlias": entPhysicalAlias,
       "entPhysicalAssetID": entPhysicalAssetID,
       "entPhysicalIsFRU": entPhysicalIsFRU,
       "entPhysicalMfgDate": entPhysicalMfgDate,
       "entPhysicalUris": entPhysicalUris,
       "entPhysicalUUID": entPhysicalUUID,
       "entityLogical": entityLogical,
       "entLogicalTable": entLogicalTable,
       "entLogicalEntry": entLogicalEntry,
       "entLogicalIndex": entLogicalIndex,
       "entLogicalDescr": entLogicalDescr,
       "entLogicalType": entLogicalType,
       "entLogicalCommunity": entLogicalCommunity,
       "entLogicalTAddress": entLogicalTAddress,
       "entLogicalTDomain": entLogicalTDomain,
       "entLogicalContextEngineID": entLogicalContextEngineID,
       "entLogicalContextName": entLogicalContextName,
       "entityMapping": entityMapping,
       "entLPMappingTable": entLPMappingTable,
       "entLPMappingEntry": entLPMappingEntry,
       "entLPPhysicalIndex": entLPPhysicalIndex,
       "entAliasMappingTable": entAliasMappingTable,
       "entAliasMappingEntry": entAliasMappingEntry,
       "entAliasLogicalIndexOrZero": entAliasLogicalIndexOrZero,
       "entAliasMappingIdentifier": entAliasMappingIdentifier,
       "entPhysicalContainsTable": entPhysicalContainsTable,
       "entPhysicalContainsEntry": entPhysicalContainsEntry,
       "entPhysicalChildIndex": entPhysicalChildIndex,
       "entityGeneral": entityGeneral,
       "entLastChangeTime": entLastChangeTime,
       "entityMIBTraps": entityMIBTraps,
       "entityMIBTrapPrefix": entityMIBTrapPrefix,
       "entConfigChange": entConfigChange,
       "entityConformance": entityConformance,
       "entityCompliances": entityCompliances,
       "entityCompliance": entityCompliance,
       "entity2Compliance": entity2Compliance,
       "entity3Compliance": entity3Compliance,
       "entity4Compliance": entity4Compliance,
       "entity4CRCompliance": entity4CRCompliance,
       "entityGroups": entityGroups,
       "entityPhysicalGroup": entityPhysicalGroup,
       "entityLogicalGroup": entityLogicalGroup,
       "entityMappingGroup": entityMappingGroup,
       "entityGeneralGroup": entityGeneralGroup,
       "entityNotificationsGroup": entityNotificationsGroup,
       "entityPhysical2Group": entityPhysical2Group,
       "entityLogical2Group": entityLogical2Group,
       "entityPhysical3Group": entityPhysical3Group,
       "entityPhysical4Group": entityPhysical4Group,
       "entityPhysicalCRGroup": entityPhysicalCRGroup}
)
