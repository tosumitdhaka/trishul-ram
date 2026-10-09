# Feasibility: Formal tsnmp default flip and legacy pysnmp/pysmi stack deprecation/removal

**Date:** 2026-10-09
**Status:** Feasibility assessment only — one standalone document, no issues filed, no
implementation, no dependency changes. Product owner decides next steps.
**Question:** Now that the parallel tsnmp stack (`TRAM_SNMP_STACK=trishul`, shipped v1.5.0
behind a flag per GH #72) is wire-validated and perf-competitive, what is the mechanics,
risk, sequencing, and effort to (1) formally make it the default, and (2) deprecate and
delete the legacy pysnmp/pysmi stack?

**Verdict up front:** **GO for a default flip in v1.9.0, removal in v1.10.0.** Every hard
blocker from the original assessment is closed and gh-verified at the pinned versions
(trishul-snmp 0.6.2 / trishul-smi 0.5.3), the wire harness returned GO twice on those pins,
the flag-on stack is byte-identical to legacy on the measured surfaces, and it matches or
beats legacy performance everywhere (31× GET, 1.26× walk-1000, e2e parity). The connector
code was explicitly written as flag-split branches designed for wholesale deletion
("the flag-off branch is deleted wholesale after the flag period" — `source.py:3-8`),
so removal is subtractive, not a rewrite. The recommended window: flip the default in
**v1.9.0** (not v1.8.x — that release's scope is frozen on the R1–R16 reliability program
and a default flip inside it would confound attribution), keep `TRAM_SNMP_STACK=legacy`
as a documented escape hatch for exactly one minor release, and delete the legacy stack
plus the `pysnmp`/`pyasn1`/`pysmi-lextudio` dependencies in **v1.10.0**.

---

## 1. Evidence base

Findings are labeled:

- **[code]** — source inspection of `tram/` on `release/v1.8.0` (commit `d07398e`).
- **[doc]** — `docs/snmp-polling-performance.md` (2026-10-08), `docs/ideas/
  trishul-smi-snmp-migration-feasibility.md` (the v1 assessment + v0.5.1 NO-GO and v0.6.1
  GO addenda), `docs/deployment.md`, `docs/changelog.md`.
- **[harness]** — `scripts/snmp-wire-harness/` (README + `REPORT.md` GO 2026-09-28 at
  tsnmp 0.6.1/tsmi 0.5.2, `REPORT-v1.5.1.md` GO 2026-09-29 at 0.6.2/0.5.3, 38 per-check
  JSONs under `results/`).

Upstream state, verified against the pins [doc]:

| Issue | Resolution | Covered by pin |
|---|---|---|
| trishul-snmp #8 (SNMPv1 polling + trap) | closed, wire-verified | 0.6.2 ✅ |
| trishul-snmp #9 (silent listener drops) | closed — drop counters, verified live | 0.6.2 ✅ |
| trishul-snmp #10 (USM crypto breadth) | closed (SHA-224/384/512, AES-192/256, 3DES) | 0.6.2 ✅ |
| trishul-snmp #11 / #29 (DES) | resolved by formal drop, fail-fast | 0.6.2 ✅ (TRAM rejects `DES` at validation, `models/pipeline.py:184-216`) |
| trishul-snmp #28 (SHA-2 HMAC truncation) | closed — RFC 7860 tag lengths, wire-proven | 0.6.2 ✅ |
| trishul-snmp #31 (3DES-EDE padding) | wire-fixed | 0.6.2 ✅ (v1.5.1 re-enabled 3DES) |
| trishul-smi #14 (.py formatter posture) | closed — frozen formatter, JSON is the maintained path | 0.5.3 ✅ (motivates `.py` corpus retirement, §4) |
| trishul-smi #15 (`cached` status) | closed — emitted, harness-verified | 0.5.3 ✅ |
| trishul-smi #16 (bundle compat policy) | closed | 0.5.3 ✅ |

Performance and byte-identity [doc — `docs/snmp-polling-performance.md`, 2026-10-08]:

| Metric | legacy (pysnmp) | trishul (tsnmp) | Delta |
|---|---:|---:|---|
| GET poll, loopback (median) | 93.9 ms | 3.0 ms | **31× faster** |
| 100-row walk, loopback | 8.2 s | 9.5 s | ~16 % slower (within noise) |
| 1000-row walk, loopback | 1270.7 s | 1009.9 s | **1.26× faster** |
| kind e2e GET / walk runs | 0.23–14.1 s | 0.18–13.3 s | parity to −6 % |
| Output bytes | — | — | **identical across all cells** |

## 2. Current-state inventory [code]

The dual-stack surface, exactly as it stands on `release/v1.8.0`:

| Surface | Location | Notes |
|---|---|---|
| Flag reader | `tram/core/config.py:26-51` (`_env_snmp_stack`), `:612` (AppConfig field), `:687` (from_env) | Default `legacy`; invalid values fail loud (`ValueError`). The flip is the single default string at `:36`. |
| Poll + trap source | `tram/connectors/snmp/source.py` (1377 L) | Legacy: `_read_raw_udp` (175-223), `_decode_trap` (411-429), `_call_snmp_api` (71-82), `_build_auth` (826-839), legacy `_do_get`/`_do_walk`/probe (1127-1231, 756-801). tsnmp: listener bridge (225-385), managers (580-641), value renderers (344-364, 552-578). |
| Trap sink | `tram/connectors/snmp/sink.py` (483 L) | Legacy `_send_trap` (361-432) + `_build_var_binds` (105-174); tsnmp `_send_trap_tsnmp` (271-359) incl. RFC 2576 §3.2 v1 Trap-PDU parity (313-354). |
| MIB utils | `tram/connectors/snmp/mib_utils.py` (634 L) | Legacy: pysnmp MIB view (444-480), `build_v3_auth` (209-282), `get_hlapi_asyncio` + 6.x/7.x + camelCase shims (366-441). tsnmp: `_TsmiBundleView` (91-155), USM builders (285-363). Resolve helpers duck-type on `mibBuilder` vs `lookup` (532, 598). |
| MIB compiler | `tram/core/mib_compiler.py` (487 L) | Dual backend in `compile_mibs` (240-318); pysmi branch ≈ lines 271-318 + `_CachingReader` (412-446); tsmi branch `_compile_mibs_trishul` (321-393) + `_TsmiCachingReader` (466-487). |
| MIB API | `tram/api/routers/mibs.py` | Dual-format scan (110-143); `GET /api/mibs/{name}` `format=auto\|py\|json`, **auto prefers `.py`** (445-446); `stack` echoed in upload/download responses. |
| Worker asset sync | `tram/agent/assets.py:161-202` | Pulls **both** formats per referenced MIB; `is_mib_bundle_content` guard protects against a pre-v1.5.0 manager serving `.py` for `?format=json` (C7). |
| Mismatch guard | `tram/api/routers/internal.py:124-153`; `tram/agent/server.py:264-272, 813, 1104-1134` | Worker reports `snmp_stack` in stats; manager warns once per worker on mismatch (no startup failure — deliberate, for rolling upgrades). |
| Corpus | `files/mibs_compiled/` | **Dual-format: 10 `.py` modules + 15 JSON bundles + `manifest.json`/`oid_index.json`. The JSON set is a strict superset** (INET-ADDRESS-MIB, SNMP-FRAMEWORK-MIB, SNMPv2-CONF/SMI/TC are JSON-only). `.py` files were generated by pysmi-1.4.3 [doc]; the tsmi backend writes JSON only (`formats=["json"]`). |
| Pins | `pyproject.toml:64-69` (`snmp` = pysnmp + pyasn1 + trishul-smi==0.5.3 + trishul-snmp[v3]==0.6.2), `:128` (`mib` = pysmi-lextudio), `:141` (dev → `tram[snmp]`), `:201` (`all` → `tram[mib]`) | Both stacks ship in every image profile that carries `snmp`. |
| Images | `Dockerfile.manager` (`mib` + `snmp` extras), `Dockerfile.worker` (`snmp`), `Dockerfile` (both) | All three `COPY files/mibs_compiled/ /mibs/` + raw sources to `/mib-sources`. |
| Docs/config | `.env.example:390`, `helm/values.yaml:243` (both commented), `docs/deployment.md:57, 114-125` | Flag documented as default-legacy with the both-stacks-must-be-installed note. |
| Tests | `tests/unit/`: `test_snmp_connectors.py` (1811 L, 108 tests — the legacy suite incl. the `sys.modules` partial-mock surface, order-dependence fixed in v1.5.1 by an autouse fixture at lines 19-59), `test_snmp_tsnmp_paths.py` (52 tests), `test_snmp_wire_cross_stack.py` (6 parametrized wire tests vs in-process **pysnmp reference peers**), `test_snmp_decode_equivalence.py` (7), `test_mib_utils.py` (15), `test_mib_compiler.py` (10, pysmi), `test_mib_compiler_trishul.py` (15), `test_mibs_dual_format.py` (11) | ≈ 4,400 lines of SNMP test surface. |

Known constraints verified in code, not just history:

- **latin-1 render spots** — `_tsnmp_val_to_legacy_str` (`source.py:344-364`) and
  `_tsnmp_val_to_str` (`source.py:552-578`) deliberately reproduce pysnmp's ugly
  `str()` forms (OctetString/Opaque → latin-1, IpAddress → 4 raw chars). Both docstrings
  name them "post-swap cleanup candidates". Equivalence is the contract today.
- **v1 Trap-PDU parity** — RFC 2576 §3.2 mapping is implemented and pinned by
  `test_v1_send_trap_wire_enterprise_specific` (cross-stack suite) [code/harness].
- **pysnmp v3 receiver engine-ID pre-registration** — a pysnmp *test-harness* constraint
  (`test_snmp_wire_cross_stack.py:203-233` pre-seeds the USM cache from
  `build_tsnmp_local_engine`), not a tsnmp defect; tsnmp listeners answer USM discovery
  probes (`source.py:258-263`, `mib_utils.py:346-363`).
- **Walk silent truncation on request timeout** — open, **orthogonal, and present on
  both stacks** (`source.py:1200` legacy `break`, tsnmp walk inside 0.6.2; documented in
  `docs/snmp-polling-performance.md` Caveat 2). It is not a flip blocker and should not be
  coupled to the flip (§5).

## 3. Phase 1 — formal replacement: flip `TRAM_SNMP_STACK` default legacy→trishul

### 3.1 Mechanics

The flip itself is one line: the default in `_env_snmp_stack` (`config.py:36`) plus the
mirrored literal in the `AppConfig.snmp_stack` field default (`:612`) and doc-text
updates (`.env.example`, `helm/values.yaml` comment, `docs/deployment.md` table + the
"SNMP library stack" section, `config.py` docstrings, connector module docstrings that say
"the default ``legacy`` path"). Everything else already exists: the tsnmp wire paths,
compile backend, bundle resolve layer, worker stats reporting, and the mismatch guard all
read the same flag.

**Escape hatch:** `TRAM_SNMP_STACK=legacy` remains a valid, documented setting for exactly
one minor release (v1.9.x). It keeps working because both stacks stay installed during
that window (`tram[snmp]` still pins pysnmp/pyasn1 — unchanged in the flip release).

### 3.2 What breaks / changes for existing deployments

1. **Deployments that never set the flag change stack on upgrade.** This is the entire
   point, and the mitigations are already in place: outputs are byte-identical on the
   measured surfaces [doc], the changelog entry must lead with the flip, and
   `docs/deployment.md` must carry a prominent upgrade note ("roll back by setting
   `TRAM_SNMP_STACK=legacy` explicitly; supported through v1.9.x"). The invalid-value
   fail-loud behavior means a typo'd value still refuses to start rather than flipping.
2. **Mixed-fleet rolling upgrade (v1.8 workers ↔ v1.9 manager).** Unchanged mechanics:
   the manager's once-per-worker WARNING fires on mismatch [code: `internal.py:124-153`],
   both stacks are installed in both images during the window, and worker MIB sync pulls
   both formats [code: `assets.py:179`]. Either upgrade order works; the post-upgrade
   check is "no stack-mismatch warnings in manager logs".
3. **MIB corpus compatibility — nothing required.** The manager's PVC keeps serving
   whatever it has; new compiles under the flipped default produce JSON bundles. `auto`
   still prefers `.py` when both exist (fine — workers request formats explicitly). The
   one visible difference: MIBs *newly uploaded* to a flipped manager exist only as JSON,
   so a `legacy`-pinned worker in a mixed fleet cannot resolve them — the mismatch
   warning is exactly this scenario; the deployment note covers it.
4. **CI / test suite.** Bounded: the SNMP suites pin or pop `TRAM_SNMP_STACK` per test
   [code], and the v1.5.1 autouse fixture asserts no leakage. Tests that *pop* the env to
   exercise "default" legacy (e.g. `test_snmp_decode_equivalence.py:184`,
   `test_snmp_wire_cross_stack.py:371`) must be flipped to explicit
   `setenv("TRAM_SNMP_STACK", "legacy")` in the flip PR — otherwise they silently start
   exercising the trishul path. That is a mechanical, auditable diff.
5. **Perf profile.** Net positive or neutral [doc]: GET-heavy pipelines gain up to 31× at
   the operation level (mostly absorbed into the ~0.2 s e2e floor), walks are parity to
   1.26× faster. The 100-row loopback walk regression (8.2→9.5 s) is within noise and
   agent-bound.

### 3.3 Validation gates for the flip release (all must be green)

- Full backend suite (`pytest tests/ -q`) with the new default, plus an explicit
  `TRAM_SNMP_STACK=legacy` full-suite pass (escape-hatch proof).
- Wire-harness re-run on the exact release pins (standing rule: any SNMP-touching release
  when pins move — the v0.5.1 lesson that claimed-fixed ≠ wire-working [harness]).
- kind e2e: the `docs/snmp-polling-performance.md` matrix re-run with the default env
  (no `--set env.TRAM_SNMP_STACK`), byte-comparing outputs against the recorded
  legacy/trishul artifacts.
- Existing parity suites green: `test_snmp_decode_equivalence.py`,
  `test_snmp_wire_cross_stack.py` (tsnmp↔pysnmp both directions),
  `test_mibs_dual_format.py`.
- Release gate's 11 checks (ruff, coverage floor, UI build, browser smoke, example
  pipelines, Helm lint/template, docs-sync) — the flip touches docs and Helm comments.

### 3.4 Effort

**2–3 dev-days + validation.** The code change is one default; the work is test pinning
(§3.2.4), docs/changelog, and the two e2e validation runs.

## 4. Phase 2 — deprecation and removal (the release after the flip)

Executed in v1.10.0, one full minor release after the flip (escape-hatch window =
v1.9.x). Sequence within the release (one PR series, order matters only for review
clarity — it ships atomically):

### 4.1 Code deletions

| Delete | Where | Size |
|---|---|---|
| Legacy trap receiver | `source.py`: `_read_raw_udp`, `_decode_trap`, `_call_snmp_api`, the `_snmp_stack` branch dispatches | ~180 L |
| Legacy poll wire | `source.py`: `_build_auth`, legacy `_do_get`/`_do_walk`, legacy `test_connection` probe, `_snmp_val_to_str`, `read()`'s `get_hlapi_asyncio` gate | ~350 L |
| Legacy sink | `sink.py`: `_send_trap` legacy branch, `_build_var_binds` | ~220 L |
| HLAPI shims + pysnmp view | `mib_utils.py`: `get_hlapi_asyncio`, `create_udp_transport_target`, `hlapi_get/next/send_notification`, `close_snmp_engine`, `_resolve_hlapi_callable`, `_get/_set_mib_sources`, `_load_mib_module`, `build_mib_view`, `_cached_mib_view`, `build_v3_auth` + the `_AUTH/_PRIV_PROTO_NAMES` tables, the `mibBuilder` branches in `resolve_oid_structured`/`symbolic_to_oid` | ~350 L |
| pysmi compiler backend | `mib_compiler.py`: the legacy branch of `compile_mibs` (271-318) + `_CachingReader` (412-446); `compile_mibs` loses the `stack` dispatch | ~120 L |
| `.py` serving | `mibs.py`: `?format=py`, `auto`→JSON, `.py` scanning in `_scan_compiled_entries`, py/json dual candidate loop | ~60 L |
| Dual-format sync | `assets.py`: the `("py", "json")` loop → JSON only; the `is_mib_bundle_content` mixed-stack guard can stay (harmless, still guards old managers) or go | ~15 L |
| Flag reader | `config.py`: remove `legacy` from the accepted tuple — `TRAM_SNMP_STACK=legacy` becomes a loud startup error naming the removal release (better than silently ignoring it); optionally accept-and-warn for one release, then delete the env var in v1.11.0 | ~10 L |
| Mismatch guard | `internal.py` / `agent/server.py`: the per-worker WARNING becomes dead logic once only one stack exists; keep the `snmp_stack` stats field (cheap, and it flags mixed-version fleets) but drop the warn path, or leave as-is for one release | ~30 L |

Net: roughly **1,300 lines deleted** from the four core files — subtractive, as designed.

### 4.2 The compiled-corpus question: retire the `.py` format

**Recommendation: retire it.** Rationale: (a) the JSON bundle set is already a strict
superset (15 vs 10 modules) [code: `files/mibs_compiled/`]; (b) tsmi's `.py` formatter is
the frozen, best-effort path (trishul-smi #14) — keeping `.py` means keeping a frozen
formatter's output in the product; (c) after removal no code path reads `.py`.
Concretely: delete the 10 `.py` files (+ `__pycache__`) from `files/mibs_compiled/`,
keep raw ASN.1 sources (`files/mibs/` → `/mib-sources`) untouched.

**The one real risk — operator PVCs holding pre-v1.5.0 `.py`-only artifacts** (vendor MIBs
uploaded to a legacy manager before the flag existed): after removal those MIBs lose
symbolic resolution until recompiled. Mitigation, all cheap: raw sources were persisted
for every upload/download since v1.0.x (`persist_mib_source`, `mib_compiler.py:189-197`),
so the fix is "re-upload or hit `POST /api/mibs/download` for the module" — the removal
changelog and deployment doc must say exactly that. A one-line `tram mib compile --all`
style migration note (recompiling everything in the source store) is a nice-to-have, not
a gate. Workers are unaffected: their `/mibs` comes from the image and `/data/mibs` from
the manager, both JSON after the removal.

### 4.3 Dependencies and images

- `pyproject.toml`: `snmp` extra drops `pysnmp` and `pyasn1` (keeps
  `trishul-smi==<pin>` + `trishul-snmp[v3]==<pin>`); the `mib` extra (`pysmi-lextudio`)
  is **deleted entirely** and removed from `all`; `dev` keeps `tram[snmp]`.
- **Keep pysnmp as a dev/test-only dependency** (e.g. inside `dev`, or a dedicated
  `testdeps` list): `test_snmp_wire_cross_stack.py` uses in-process pysnmp agents and
  trap receivers as the reference peer, and the wire harness needs a pysnmp peer by
  design [harness]. Dropping it from production while keeping it as the interop oracle
  is the strongest possible regression net for the now-sole stack — the v0.5.1 HMAC bug
  was caught by exactly this cross-agent discipline.
- Images: `Dockerfile.manager`/`Dockerfile` lose the `mib` extra; all three keep `snmp`.
  Worker image shrinks by pysnmp+pyasn1+pysmi (~few MB; the real win is the dependency
  surface, not size). `Dockerfile` comment blocks mentioning "pysmi MIB compilation"
  updated; `COPY files/mibs_compiled/` stays (now JSON-only).
- UI: `tram/ui/src/pages/mibs.html` drops the `.py`/dual-format column (formats list
  becomes single-value); CLI docstrings in `cli/main.py` that say "compiled .py files"
  reworded.

### 4.4 Test-surface migration

| Suite | Disposition |
|---|---|
| `test_snmp_connectors.py` (108 tests) | **Splits three ways:** (1) the `sys.modules`-mocked pysnmp wire tests die (~the mock surface at lines 106-1504); (2) stack-agnostic tests (classify/grouping/patterns/`_group_by_index`/`_classify_bindings`/config parsing) migrate unchanged — they never touch the wire; (3) legacy-behavior tests that assert pysnmp `str()` forms either die with the legacy renderer or convert to pin the tsnmp renderer's byte-equal output (which is the same string). The v1.5.1 order-dependence fixture goes with the mocks. |
| `test_mib_utils.py` | Legacy `MibViewController` tests die or migrate to `_TsmiBundleView` equivalents (most already exist in `test_snmp_tsnmp_paths.py`); the duck-type dispatch in the resolve helpers simplifies to bundle-only. |
| `test_mib_compiler.py` (pysmi) | Dies; `test_mib_compiler_trishul.py` (15 tests) is the survivor and absorbs any still-relevant cases (naming, source persistence, delete). |
| `test_snmp_tsnmp_paths.py`, `test_mibs_dual_format.py`, `test_snmp_decode_equivalence.py` | Stay; `test_mibs_dual_format.py` shrinks to its JSON-only assertions; decode-equivalence keeps running legacy-vs-tsnmp **as long as pysnmp remains a dev dep** (another argument for §4.3). |
| `test_snmp_wire_cross_stack.py` | Stays — this is the pysnmp-peer oracle. |

Hazard to respect during migration: the mocked tests' import-order sensitivity is
*fixed but load-bearing* (the autouse fixture pins `pysnmp.hlapi` submodule state);
deleting that file removes the fixture, so any *surviving* test that imports
`get_hlapi_asyncio`'s module graph must be checked — simplest is that nothing survives
which needs it, which the deletion list in §4.1 guarantees.

### 4.5 Validation gates for the removal release

- Full suite green with pysnmp absent from the runtime env (install without the
  pysnmp production extra — proves no deferred import resurrects it); then the full
  suite again with the dev/test pysnmp present (wire oracle).
- Wire harness re-run (unchanged rule).
- kind e2e smoke: register + run an snmp_poll and snmp_trap pipeline on the default
  stack against the synthetic responder; MIB upload → compile → worker sync → resolve
  round-trip on a fresh PVC (exercises the JSON-only corpus path).
- Release gate 11 checks; `tram plugins` / `tram version` sanity; Helm lint/template.
- Docs-sync: deployment table, `.env.example`, Helm values, `docs/snmp-polling-performance.md`
  gets a "legacy stack removed in v1.10.0" note rather than a rewrite (it is dated evidence).

### 4.6 Effort

**4–6 dev-days + validation**: ~1,300 L of code deletion, ~600-900 L of test
deletion/migration, pyproject/image/UI/CLI edits, the corpus `.py` retirement, and the
docs pass. No new code paths — the highest-risk item is the test migration, not the
deletion.

## 5. Risk register

1. **Byte-identity regressions become default-visible at flip.** Today a tsnmp renderer
   divergence is only seen by opted-in deployments; after the flip every deployment sees
   it. Mitigation: the decode-equivalence and cross-stack suites pin the renderer byte
   contract, and the kind e2e byte-compare is a flip gate (§3.3). Residual risk is
   low — the parity suites have been in-tree since v1.5.0 and survived the v1.5.1 pin bump.
2. **The latin-1 / IpAddress rendering spots.** `source.py:344-364, 552-578` reproduce
   pysnmp's `str()` forms deliberately. **Recommendation: do NOT clean them up in the
   flip release** — that would make the flip non-byte-identical and confound attribution
   (the same reason the flip doesn't ride in v1.8.0). Schedule the rendering cleanup
   (hex/pretty forms) as its own documented behavior change after the removal lands
   (v1.10+), with a golden-corpus diff like the v1.6.1 timestamp fast-path did.
3. **USM interop matrix vs pysnmp peers.** Validated on the wire twice at the pins
   (full SHA × AES/3DES matrix accepted by a pysnmp 7.1.30 agent and net-snmp snmpd;
   v1.5.1 addendum) [harness]. Standing exposure: pins must not move without a harness
   re-run — already repo rule. The pysnmp receiver engine-ID pre-registration quirk is
   test-harness-side and stays solved in the fixtures (`test_snmp_wire_cross_stack.py:203`).
4. **Test-mock migration hazards.** Covered in §4.4: the order-dependence fixture dies
   with the mocked surface; the audit point is "no survivor imports the shim graph".
   The flip-phase hazard (tests popping the env var to mean "legacy") is the §3.2.4 diff.
5. **Dual-format corpus retirement (`.py`) risk.** §4.2: operator PVCs with legacy-only
   `.py` artifacts; recovery is recompile-from-source, which the source store makes
   always possible. Contained by the changelog/deployment note.
6. **tsnmp walk behavior differences.** tsnmp's walk hardening (dedupe, echo-reject,
   zero-progress) is a behavior *improvement* vs the legacy loop but is a difference;
   it shipped in the v1.5.0 flag path and is covered by tsnmp-path tests + the walk
   boundary harness checks [harness]. `bulk=False` mirrors legacy's GETNEXT loop — no
   GETBULK semantics change rides in through the back door.
7. **Walk silent truncation (open, both stacks).** Not a flip blocker (identical
   behavior on both stacks — the flip changes nothing about it), and should be fixed as
   its own item (fail/partial-report the run) in the v1.9.0 or v1.10.0 train, not inside
   the flip PR — a run-visible behavior change and a stack swap in the same diff would
   make field reports ambiguous.
8. **Single-owner Beta libraries.** Unchanged from the original assessment's risk
   register: bus factor is real but the owner is TRAM's owner, exact pins are wire-locked,
   and the escape hatch plus the JSON corpus give a recovery path for one full release.

## 6. Release sequencing and effort summary

| Phase | Release | Content | Effort | Gate |
|---|---|---|---|---|
| 0 — preconditions | now | none (evidence complete; this doc) | 0 | — |
| 1 — flip | **v1.9.0** | default `trishul`; `legacy` = documented escape hatch; test env pinning; docs/changelog | 2–3 d | §3.3 |
| 1a — optional parallel | v1.9.x | walk-timeout fix as its own PR (orthogonal) | 1–2 d | its own tests |
| 2 — removal | **v1.10.0** | delete legacy branches + shims + pysmi backend; `.py` serving + corpus retirement; drop pysnmp/pyasn1 from `snmp`, delete `mib` extra; test migration; `legacy` value → loud startup error | 4–6 d | §4.5 |
| 3 — optional cleanup | v1.10+ | latin-1/IpAddress rendering modernization (behavior change, golden-corpus diff) | 1–2 d | its own tests |

**Why not v1.8.x:** v1.8.0's scope is frozen and explicitly "no finding deferred" on the
R1–R16 reliability program (`docs/plans/v1.8.0-reliability-performance-plan.md`); a default
stack flip inside it would confound a reliability release's attribution, and a patch
release must never change a default. **Why one-minor-window:** the project's own flag
convention (v1.5.0 → flag period → delete "wholesale after the flag period") plus the
`TRAM_WORKER_LEGACY_ADMIT` precedent of a rollback bridge lasting a bounded number of
releases; one full minor (v1.9.x) gives every deployment at least one upgrade cycle with
a working `legacy` setting, and the fail-loud flag reader guarantees nobody silently
limps past the window.

## 7. Go/no-go

**GO — flip the default to `trishul` in v1.9.0; remove the legacy stack and the
pysnmp/pyasn1/pysmi dependencies in v1.10.0.**

Decisive evidence:

1. **Every upstream blocker is closed and wire-verified at the shipped pins** (§1 table;
   trishul-snmp #8/#9/#10/#11/#28/#29/#31, trishul-smi #14/#15/#16 — all covered by
   trishul-snmp 0.6.2 / trishul-smi 0.5.3, the versions already in `pyproject.toml`).
2. **The wire harness returned GO twice on these pins** (2026-09-28 and the v1.5.1
   re-run), including the full v3 crypto matrix against pysnmp and net-snmp peers, v1
   traps, walk boundaries, and corpus compile [harness].
3. **Byte-identical outputs and parity-or-better performance** [doc]: 31× GET, 1.26×
   walk-1000, e2e parity, identical payload bytes across every measured cell — the
   perf doc was written *as* deprecation evidence.
4. **The removal is subtractive by design** [code]: the v1.5.0 flag-split deliberately
   isolated the legacy branches ("deleted wholesale after the flag period") — ~1,300 L
   of deletion with no new architecture.
5. **Operational safety nets exist**: the escape hatch (one release), the fail-loud flag
   reader, the mixed-fleet mismatch warning, dual-format worker sync, and the
   recompile-from-source recovery for legacy `.py` artifacts.

Conditions attached to the GO:

- The wire harness must be re-run green on the exact release-candidate pins before each
  of the two releases (the repo's standing SNMP rule).
- The flip PR must not bundle the latin-1 rendering cleanup, the walk-timeout fix, or
  any other behavior change (attribution discipline).
- `pysnmp` stays as a dev/test-only dependency after removal so the cross-stack oracle
  suites keep running.

## 8. Open questions for the product owner

- Confirm the escape-hatch window length (one minor release recommended; two if any
  field deployment is known to be pysnmp-peer-sensitive in a way the harness didn't cover).
- Confirm `.py` corpus retirement at removal (recommended) vs keeping dual-format serving
  indefinitely (costs: frozen tsmi `.py` formatter dependency, `.py` scan/serve code).
- Confirm pysnmp-as-test-dependency (recommended) vs moving all cross-stack validation
  out-of-tree into the wire harness only.
- Whether the `snmp_stack` stats field and mismatch warning survive removal as a
  version-skew signal (recommended: keep the field, drop the warn).
