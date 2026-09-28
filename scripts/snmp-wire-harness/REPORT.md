# tsmi/tsmp migration smoke — v0.6.1 re-validation (2026-09-28)

Re-run of the migration feasibility harness against **trishul-snmp 0.6.1 + trishul-smi 0.5.2**
(follow-up to the v0.5.1 NO-GO for the USM HMAC tag truncation, filed upstream as #28).

Environment: fresh venv (Python 3.12.3), PyPI installs of `trishul-snmp[v3]==0.6.1`,
`trishul-smi==0.5.2`, `pysnmp 7.1.30`, `cryptography 50.0.1`. Reference agents:
pysnmp 7.1.30 entity-API agent (127.0.0.1:11163, 42 v3 users incl. Reeder AND Blumenthal
AES-192/256 variants), net-snmp snmpd 5.9.4 (matrix on :11162; CI-equivalent config on
:1161/:1162). Lib sources: git archive of tags v0.6.1 / v0.5.2, byte-identical to the
installed dists (verified by recursive diff).

## Verdict: GO for option C (full pysnmp/pysmi swap behind a feature flag)

The v0.5.1 blocker (#28, 12-byte HMAC truncation) is fixed and wire-verified end-to-end
against two independent, conformant agent implementations. One new, non-blocking defect
was found (3DES-EDE padding strictness) and DES-CBC remains formally dropped — both are
scope notes, not blockers.

## Check results

| # | Check | Verdict | Evidence (one line) |
|---|---|---|---|
| 1a | #28 tag lengths on wire | PASS | outgoing authParams: MD5/SHA-1=12, SHA-224=16, SHA-256=24, SHA-384=32, SHA-512=48 (RFC 3414 + RFC 7860) |
| 1b | v3 matrix vs pysnmp agent | PASS | GETs accepted: AES-128 6/6 auths; AES-192/256 vs Blumenthal users 12/12 (see 1e for Reeder nuance) |
| 1c | v3 matrix vs net-snmp snmpd | PASS | 10/10 non-DES combos OK: SHA-1/224/256/384/512 x AES-128/192/256 incl. SHA-224/AES-256 (extension combo, #30 fix) |
| 1d | standard SHA-2 traps inbound | PASS | pysnmp SHA-256/AES-128, SHA-384/AES-256, SHA-512/AES-128 traps all received + `decode_notification(user=)` decodes them |
| 1e | AES-192/256 variant nuance | note | vs pysnmp-DEFAULT (Reeder) users 7/12: the 5 misses (MD5/SHA-1 x AES-192/256, SHA-224/AES-256) are key-EXTENSION combos where pysnmp's default Cisco-style variant differs from tsmp's net-snmp-compatible blumenthal-04 derivation — tsmp matches net-snmp (verified green); use pysnmp's `*_BLUMENTHAL` users in TRAM test fixtures |
| 2 | DES-CBC (#29) | PASS (dropped) | formally dropped: every auth x DES attempt raises clean fail-fast `ProtocolError` ("cryptography no longer exposes single-DES"); enum retained; TRAM must reject `priv: DES` configs (already clean) |
| 3a | walk hardening (#25) | PASS | live quirk agents: duplicate rows deduped + walk continues; request-echo rejected as phantom; zero-progress terminates; EndOfMibView terminates (4/4) |
| 3b | tsmi 0.5.2 IR enrichment | PASS | IF-MIB.json schema stays 1.1; ifOperStatus.enums {up:1..lowerLayerDown:7} + constraints; units threading verified on synthetic UNITS-TEST-MIB ("kilometers per hour"); TRAM's raw corpus has no UNITS clauses so its IR correctly has none |
| 3c | tsmp 0.6.1 rendering | PASS | `tsnmp get --bundle` renders `IF-MIB::ifOperStatus.1 = up(1)` (additive; TRAM doesn't render) |
| 4a | lib test suites | PASS | tsmp 0.6.1: 744 passed, 0 skipped (31 snmpd-marked tests ran live vs CI-equivalent snmpd); tsmi 0.5.2: 660 passed |
| 4b | MIB corpus compile | PASS | 15 TRAM raw MIBs -> JSON + manifest in 0.4s (12 compiled, 3 cached), deps auto-resolved; resolve/lookup bidirectional incl. cross-module IF-MIB/IANAifType |
| 4c | SNMPv1 (#8 regression) | PASS | v1 GET/GETNEXT/WALK vs in-stack responder + real snmpd; v1 trap send/receive with full metadata; pysnmp v1 trap received; decode_notification on v1 Trap-PDU |
| 4d | v2c + cross-stack | PASS | v2c GET/WALK (bulk + getnext-loop), v2c trap; tsmp->pysnmp-agent GET/GETNEXT/WALK; pysnmp->tsmp-responder getCmd/nextCmd; v2c traps both directions |
| 4e | walk boundary | PASS | exact subtree walks, gap crossing, EndOfMibView past last OID, enterprise subtree, v1 walk (GETNEXT loop) |
| 4f | silent-drop (#9 regression) | PASS | dropped=4 with per-reason counts (UNDECODABLE_BER x2, NOT_NOTIFICATION, WRONG_COMMUNITY) + on_error x4; v3 listener garbage: dropped=2 |
| — | in-stack v3 trap regressions | PASS | SHA-256/AES-128, SHA-224/AES-192, SHA-512/3DES authPriv traps tsmp->tsmp |
| — | **v3 3DES-EDE wire interop** | **FAIL (new defect)** | see below |

## The new finding: 3DES-EDE padding interop

- Live: tsmp 3DES GETs vs pysnmp agent: 0/6 (RequestTimeoutError — responses
  undecryptable); pysnmp 3DES-auth'd trap -> tsmp listener: dropped UNDECODABLE_BER.
- Offline proof: keys match pysnmp byte-for-byte (SHA-224/256/384/512 verified);
  tsmp's `_decrypt_3des_ede` enforces strict PKCS7(64) padding, but
  draft-reeder-snmpv3-usm-3desede-00 5.1.1.2 says "The actual pad value is
  irrelevant" and 5.1.1.3 says "When decrypting, the padding is ignored."
  pysnmp's zero-padding (draft-legal) and RFC 3414-convention padding (zeros +
  last byte = length) are BOTH rejected by tsmp; tsmp's own PKCS7 ciphertext
  decrypts fine on pysnmp (pysnmp ignores the tail).
- Impact: tsmp cannot receive/decrypt 3DES from any sender that doesn't use
  PKCS7-shaped padding (i.e., every draft-compliant implementation; only 3DES
  users are affected — AES paths are unpadded CFB and all green).
- Recommended: file upstream (fix = ignore trailing pad per BER length instead
  of strict PKCS7 unpad). Niche protocol; does not block the swap.

## Regressions vs the 0.4.x / 0.5.1 assessments

None. Everything previously green stayed green; the previously-broken items are
fixed: #28 (tag lengths, wire-verified both agents), #30 (AES-192/256 derivation,
wire-verified vs net-snmp ground truth incl. the SHA-224/AES-256 extension combo),
#25 (walk quirks, live-verified 4/4), snmpd CI suite now 31 tests (ran live, 744
passed total), `cached` compile status (tsmi #15) confirmed.

## Scope notes for the TRAM swap (option C)

1. `priv: DES` configs: clean fail-fast — add config-time rejection + docs.
2. 3DES: works in-stack and vs PKCS7-senders only; file upstream, note in TRAM
   docs as known limitation until fixed (legacy profile, rare).
3. TRAM's own test fixtures that use pysnmp v3 USM with AES-192/256 + MD5/SHA-1/
   SHA-224 must register Blumenthal-variant users (`USM_PRIV_CFB192/256_AES_BLUMENTHAL`)
   or they will fail against tsmp (variant mismatch, not a tsmp bug).
4. Walk behavior change (better): duplicate rows deduped, echoes rejected —
   TRAM walk consumers on quirky agents get cleaner output.
5. Cosmetic (unchanged since 0.5.1): v1/v2c NotificationEvent.to_dict() carries
   no `snmp_version` (distinguish via `pdu_type == "trap"` + `generic_trap`).

## Artifacts

- Scripts: scripts/01..09 (01 lib suites, 02 MIB compile, 03 v1, 04 v3 crypto
  matrix + 3DES padding proof, 04b SHA-512 trap isolation, 05 regressions +
  cross-stack, 06 silent drop, 07 walk boundary, 08 walk quirks live, 09 IR
  enrichment + rendering), pysnmp_agent.py (42-user reference agent),
  pysnmp_traprecv.py, smokecommon.py
- Per-check JSON: results/ (37 records); logs/; snmpd configs + units-test MIB;
  compiled corpus in mibs-out/
