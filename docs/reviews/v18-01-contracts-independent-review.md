# V18-01 contracts — independent review and disposition

Date: 2026-10-08. Baseline: v1.7.0 / `8e128dd`.
Scope: [frozen contracts document](../plans/v1.8.0-v18-01-contracts.md) for
v1.8.0 Waves 1–5, against the [implementation
plan](../plans/v1.8.0-reliability-performance-plan.md) and the [source
review](worker-pipeline-reliability-performance-2026-10-08.md).

One independent reviewer read the plan, the contracts document, and the
v1.7.0 implementation without consulting any prior review record or the
authoring history, and completed a full state×event enumeration, a citation
verification pass, and a plan-conformance map. No files were changed and no
tests were run; this is design review evidence.

## Verdict

**Approve with amendments.** All forty-plus file:line citations verified
against the v1.7.0 code with zero refutations. The dialect claims (SQLite
`BEGIN IMMEDIATE` rowcount authority, PostgreSQL `RETURNING`-on-UPDATE,
`INSERT … ON CONFLICT DO NOTHING`) are valid for the named engines, and the
Pydantic-v2 default `extra='ignore'` compatibility claims hold against the
actual `RunRequest`/`StopRequest`/`RunCompletePayload` models. The HMAC token
field set is unambiguously encodable and the key-rotation overlap (max TTL +
skew = 605 s) is consistent with the GC-wait rule. The exit-gate oracle ("no
undefined unknown/retry transition") was not met as delivered: the state
machine had reachable-but-undefined transitions, one retry path contradicted
plan F, and three frozen-schema fragments did not implement their own prose.
All amendments below are incorporated into the contracts document; none
required architectural rework.

## Blocking findings and dispositions

| ID | Finding | Incorporated disposition |
|---|---|---|
| C1 | HTTP 503 admission refusal was mapped to terminal `failed/dispatch_rejected`, contradicting plan F (healthy-but-full is not worker failure); 410 revoked was mislabeled `failed` | `dispatching → terminal` split: authoritative rejection (4xx/5xx non-410/503) → `failed/dispatch_rejected`; 410 → `aborted/revoked_before_acceptance`; 503 → attempt terminal with a capacity reason, run intent left unresolved, redispatch as a new attempt or queue re-entry per the one-pending-manual-run policy |
| C2 | `claimed` × operator cancellation before dispatch and `dispatching` × revocation in flight were reachable but undefined — exactly the R6/R9 windows | Two rows added: `cancelled_before_dispatch` (guard: `dispatch_sent_at IS NULL`) and `revoked_before_acceptance` (worker-side tombstone still enforced; delayed POST cannot start) |
| C3 | Attempt-state transitions were described as "CHECK-enforced", which a SQL CHECK cannot do, and no transition statement was frozen | Frozen conditional-UPDATE statement added (`WHERE attempt_id AND run_id AND state = :from AND generation`; rowcount is the authority); wording corrected to conditional-UPDATE enforcement |
| C4 | `delivery_checkpoints UNIQUE(pipeline_name, source_unit)` cannot hold an advancing Kafka frontier; the write path was unspecified | `frontier_seq` comparable-scalar column added; frozen monotonic-guard upsert (`ON CONFLICT … DO UPDATE … WHERE :seq > frontier_seq`); an approved reset deletes the pipeline's checkpoint rows; generation/attempt identity columns advance on each committed advance |
| C5 | The `transform_state` "CAS" compared generation but not revision, violating plan C's stale-writer rejection | `AND revision = :base_revision` added to the frozen CAS |
| C6 | `admission_reservations` lacked `generation`/`slot_id`, making the 409 conflicting-identity check unenforceable | Both columns added to the journal schema |
| C7 | MySQL is a documented v1.7 deployment option (`docs/deployment.md`, `db.py` mysql branches) but was silently absent from the frozen dialect contract; the frozen DDL is invalid on MySQL | Frozen disposition: v1.8.0 supports SQLite and PostgreSQL only; a MySQL `TRAM_DB_URL` fails closed at startup with an explicit unsupported-dialect message; `docs/deployment.md` migration note; no third-dialect DDL |
| C8 | Config freeze incomplete: `TRAM_WORKER_LEGACY_ADMIT` normative but unfrozen; the standalone ephemeral opt-in unnamed; webhook byte/concurrency bounds and error-sample caps missing | Rows added: `TRAM_WORKER_LEGACY_ADMIT=auto`, `TRAM_STANDALONE_EPHEMERAL_MODE=false`, `TRAM_WEBHOOK_QUEUE_MAX_BYTES`/`_MAX_CONCURRENT_READS` (16 MiB / 32), `TRAM_ERROR_SAMPLE_CAP` (1000) |
| C9 | Legacy-mode `/agent/stop` was unspecified, breaking the v1.7-manager rollback window | A legacy-shaped stop (no `attempt_id`/`authorization`) under legacy admit falls back to today's run_id-keyed `stop_event` semantics |

## Additional recommendations incorporated

| # | Recommendation | Disposition |
|---|---|---|
| 1 | Define `unknown` × recovered-active-evidence observation | Row added: diagnostics only, no state change until a terminal resolution |
| 2 | Retitle confirmation tiers — two cells pending the V18-02 audit | Heading corrected to "assignments frozen; … pending the V18-02 audit" |
| 3 | Split journal retention into two env names | `TRAM_WORKER_JOURNAL_AUDIT_RETENTION_S` / `_REPLAY_RETENTION_S` |
| 4 | Index `run_intents(expires_at)` | `idx_ri_expires` added — queue expiry runs independently of liveness scans |
| 5 | Add a source-unit lifecycle diagram | Mermaid added: pending → committed → acked with the four dispositions and the uncertain branches |
| 6 | Restate the journal/PVC-loss rule in the contracts document | Added to the watermark/GC paragraph |
| 7 | Carry over the stateful-broadcast restriction | Added to the Kafka/replay text |
| 8 | Freeze the one-deadline parameterization | `TRAM_DRAIN_TIMEOUT_S` is the single source for plan E's one monotonic deadline; no independent second timeout |
| 9 | Guard-row lifecycle for defunct stream placements | Retention prunes stale guard rows only when they hold no active attempt and their placement group no longer exists |
| 10 | Clarify the actor on `dispatching → running` | The ledger row advances on the manager's 202 acceptance or status snapshot; the worker never writes the ledger |

The reviewer's completed state×event grid also flagged `dispatching` ×
manager crash → boot adoption as defined in the migration narrative but
absent from the table; a row was added.

## Disposition D1 — tier-table amendment

**Disposition D1 (2026-10-08, release/v1.8.0): section 6 tier table
amended.** The frozen table assigned `fsynced_local` to ftp, s3, gcs, and
azure_blob "(post V18-02 manifest+fsync)"; that assignment is inapplicable —
object stores have atomic server-confirmed PUTs with no rename/fsync
semantics, and FTP has no client-observable fsync and server-dependent rename
atomicity — so assigning it would have violated invariant 4 (truthful
results) and the table's own tier definitions. Amended assignment:
s3/gcs/azure_blob → `remote_durable` (per-write server-confirmed PUT, trivial
commit barrier, replay_safe=False); ftp → `remote_accepted` (synchronous STOR
completion reply, durability not asserted, non-atomic publication documented
in the receipt). The wave-1 file-publication implementation, which left the
four sinks undeclared rather than claim false durability, is correct as-is;
the follow-up is the mechanical capability declarations on the four sinks,
with FTP temp-name+RNFR/RNTO staging an optional hardening that cannot reach
`fsynced_local` in any case. The sftp cell gains the landed code's documented
limitation note, and the Kafka/AMQP cells move from pending to confirmed per
the completed V18-02 audit. No strict-validation change is required — strict
requires tier-declared sinks, not a minimum tier.

## Disposition D2 — handshake direction and secret minting

**Disposition D2 (2026-10-08, release/v1.8.0): section 5 handshake row
amended.** The frozen row specified worker→manager registration with the
manager replying with the session secret. The implementation (both sides
landed and integration-tested green) instead has the manager POST to the
worker's `/agent/handshake`, and the worker mints and returns the secret. The
amendment adopts the implemented shape: the worker controls its own admission
keys (it is the party that must validate them), the manager holds no
pre-shared secret, and rotation keeps the previous secret valid for max TTL +
skew (605 s) on both sides. The manager registers the returned secret and
mints authorizations with it; the worker validates against its stored
current/previous pair. No security property is weakened: the endpoint is
machine-authenticated on the existing internal API-key channel, and the
session secret never travels outside that channel.

## Disposition D3 — lifecycle_operations op_kind boot_adopt

**Disposition D3 (2026-10-08, release/v1.8.0): section 3
`lifecycle_operations` domain extended with `boot_adopt`.** The frozen comment
domain (stop|restart|update|delete|drain|force_release) had no kind for
boot-adoption resolutions, so the implementation initially recorded them as
`stop` with a detail marker. The domain is a schema comment (no CHECK
constraint) and the table is new in v1.8 — nothing is deployed to migrate —
so the domain gains `boot_adopt`, the five boot-adoption recording sites use
it, and real stop operations keep `stop`.

## Disposition D4 — cross-epoch retired-token replay boundary (accepted)

**Disposition D4 (2026-10-08, release/v1.8.0): known accepted boundary in
section 4's watermark rules.** A token GC'd in an OLD epoch and replayed in a
NEW epoch (after a trusted-time recovery) at a clock still inside the token's
validity window is not covered by the new epoch's watermark. Surviving
old-epoch rows are refused by the epoch check, and GC only deletes rows whose
authorizations expired plus skew, so the window requires: trusted-time
recovery + GC of the old row + replay inside the residual validity. Retaining
and consulting old-epoch watermarks was considered and rejected: it would
over-reject fresh tokens after a legitimate recovery. Accepted as documented;
the worker server carries the boundary note at the validation seam.

## Verification boundary

Documentation inspection and code citation verification only. No application
tests, benchmarks, fault campaign, migration rehearsal, or release gate was
run. Implementation and release sign-off require a later review of the full
code diff and the mandatory release gate.
