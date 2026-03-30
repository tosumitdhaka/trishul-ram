# TRAM v1.1.2 Roadmap

---

## Bug: API-registered pipelines not visible across cluster nodes

### Problem
`POST /api/pipelines` registers a pipeline in-memory on the single pod that received
the HTTP request. Other pods never discover it. When the NodePort load-balances a
subsequent request to a different pod, the pipeline is invisible — appears missing from
the UI, shows wrong active count, and cannot be triggered or monitored from those pods.

Pipelines loaded from `TRAM_PIPELINE_DIR` (Helm ConfigMap) work correctly because every
pod reads the filesystem directory at startup. API-registered pipelines have no equivalent
broadcast mechanism today.

### Files to change

**`tram/persistence/db.py`**
- New table `registered_pipelines (name TEXT PK, yaml_text TEXT, created_at, updated_at, deleted BOOL DEFAULT FALSE)`
- Schema migration via existing `_add_column_if_missing` pattern — safe on existing DBs
- Methods: `save_pipeline(name, yaml_text)`, `delete_pipeline(name)`, `get_all_pipelines() → list[tuple[str,str]]`

**`tram/api/routers/pipelines.py`**
- `POST /api/pipelines` → also `db.save_pipeline(name, yaml_text)`
- `PUT /api/pipelines/{name}` → also `db.save_pipeline(name, updated_yaml)`
- `DELETE /api/pipelines/{name}` → also `db.delete_pipeline(name)` (soft delete)

**`tram/scheduler/scheduler.py`**
- On startup: after `TRAM_PIPELINE_DIR` load, call `_load_from_db()` — registers any DB pipeline not already loaded from filesystem (filesystem wins on name collision)
- New background thread `_sync_from_db()` polling DB every `TRAM_PIPELINE_SYNC_INTERVAL` seconds — registers newly added pipelines, deregisters deleted ones; all pods converge without restart
- Ownership/rebalance logic unchanged — non-owning pods register pipeline (visible in API) but do not execute it

**`tram/core/config.py`**
- Add `TRAM_PIPELINE_SYNC_INTERVAL: int = 30` — DB poll interval in seconds

### Behaviour after fix
- `POST /api/pipelines` on any pod → written to shared PostgreSQL → all pods pick it up within `TRAM_PIPELINE_SYNC_INTERVAL` seconds
- Pipeline visible on all pods regardless of which pod the UI hits
- Consistent ownership: hash decides which pod runs it, all pods report correct status
- `TRAM_PIPELINE_DIR` (ConfigMap) pipelines load at startup as before; DB-registered pipelines layer on top
- SQLite (standalone, single pod): DB persistence still works, sync loop is a no-op in practice

---

## ASN.1 Serializer (`type: asn1`)

### Background
Ericsson PM statsfiles (3GPP TS 32.401) and other telecom data are distributed as
BER-encoded ASN.1 binary files. There is no native TRAM deserializer for this format today;
the only path is an external script (`ericsson_pm_parser_v2.py`).

### Design
Same pattern as the existing `protobuf` serializer — user provides a standard `.asn` schema
file and a top-level type name:

```yaml
serializer_in:
  type: asn1
  schema_file: /data/schemas/3gpp_32401.asn   # uploaded via TRAM schema UI
  message_class: FileContent                   # top-level ASN.1 type to decode
  encoding: ber                                # ber | der | per | uper | xer | jer  (default: ber)
```

### Implementation
- **Library**: `asn1tools>=0.167` — compiles standard `.asn` files, decodes BER/DER/PER/XER/JER/OER
- **Schema compile**: once in `__init__` via `asn1tools.compile_files([schema_file], encoding)`;
  cached for lifetime of serializer instance (same caching pattern as protobuf)
- **Multi-file schemas**: `schema_file` can be a directory — all `.asn` files compiled together
  (same as protobuf's multi-file proto packages)
- **Output**: `asn1tools` returns native Python dicts/lists; a small `_to_json_safe()` helper
  converts `datetime` objects to ISO strings and CHOICE 2-tuples to `{"type": x, "value": y}`
- **New optional extra**: `tram[asn1]` = `asn1tools>=0.167`
- **Schema upload**: add `.asn` to the accepted extensions in `POST /api/schemas/upload`
  (one-line change alongside `.proto`, `.avsc`, `.xsd`, etc.)
- **Model**: `Asn1SerializerConfig` in `tram/models/pipeline.py` with `schema_file`,
  `message_class`, `encoding` fields

### Feasibility confirmed (2026-03-30)
- `asn1tools` compiled a 3GPP TS 32.401 schema (12 types, ~40 lines) that decodes all
  11 sample Ericsson PM statsfiles (`C*` core and `G*` HLR/vHLR variants) without errors
- Both BER and DER encoding paths verified
- Multi-file `compile_files([f1, f2])` works correctly (imports resolved across files)
- `GeneralizedTime` auto-parsed to `datetime`; CHOICE decoded as `(type_name, value)` tuple

### Sample schema for Ericsson 3GPP PM files
To be shipped as `docs/schemas/3gpp_32401.asn` (reference schema, uploadable via UI):

```asn1
GPP-PM-32401 DEFINITIONS IMPLICIT TAGS ::= BEGIN
  FileContent ::= SEQUENCE {
    fileHeader  [0] FileHeader OPTIONAL,
    measData    [1] MeasData,
    endTime     [2] GeneralizedTime OPTIONAL
  }
  FileHeader ::= SEQUENCE {
    fileFormatVersion   [0] GraphicString OPTIONAL,
    senderName          [1] GraphicString OPTIONAL,
    senderType          [2] GraphicString OPTIONAL,
    vendorName          [3] GraphicString OPTIONAL,
    collectionBeginTime [4] GeneralizedTime OPTIONAL
  }
  MeasData     ::= SEQUENCE OF MeasCollection
  MeasCollection ::= SEQUENCE {
    managedElement [0] ManagedElement OPTIONAL,
    measInfoList   [1] SEQUENCE OF MeasInfo
  }
  ManagedElement ::= SEQUENCE {
    swVersion [0] GraphicString OPTIONAL,
    localDn   [1] GraphicString OPTIONAL
  }
  MeasInfo ::= SEQUENCE {
    measTimeStamp     [0] GeneralizedTime OPTIONAL,
    granularityPeriod [1] INTEGER OPTIONAL,
    measTypes         [2] SEQUENCE OF GraphicString,
    measValues        [3] SEQUENCE OF MeasValue
  }
  MeasValue ::= SEQUENCE {
    measObjLdn  [0] GraphicString,
    measResults [1] SEQUENCE OF MeasResult,
    suspect     [2] BOOLEAN OPTIONAL
  }
  MeasResult ::= CHOICE {
    iVal   [0] INTEGER,
    rVal   [1] REAL,
    isNull [2] NULL
  }
END
```

### Scope
- Deserialize only (`serializer_in`) — encode path (ASN.1 sink output) deferred to a later release
- No schema-less / positional fallback — schema file is required (same as protobuf)
- The `ericsson_pm_parser_v2.py` custom JSON schema approach is **not** used — standard
  `.asn` files are the input, making the serializer vendor-agnostic
