# ClickHouse outage and publication evidence

Date: 2026-10-13. Server: ClickHouse **24.10.2.80**. Fixture:
`tram-v183-clickhouse-test`, native endpoint `127.0.0.1:19083`.
Only this isolated fixture was stopped or started. Existing deployed TRAM and
kind services were not accessed.

## Seven-case live profile

The final `tests/unit/test_clickhouse_live_broker.py` profile passed **seven
cases in each of three repetitions**: 0.59 s, 0.59 s and 0.64 s. Each test uses
a unique table and checks exact `(id, val)` output contents.

| Test | Fault or behavior actually exercised | Confirmed outcome |
|---|---|---|
| `test_live_durable_commit_confirm_before_barrier` | Real server, buffered write then commit | No server rows before commit; exact two rows after confirmed receipt |
| `test_live_batch_flush_on_stop_false_retains_buffer` | Close with final flush disabled | Rows retained in the same sink instance; later commit publishes exact contents |
| `test_live_close_flushes_when_batch_flush_on_stop_true` | Real close/drain insertion | Exact remaining row published before close returns |
| `test_live_rejected_insert_retained_and_retry_commits_contents` | **Real server rejection** for a nonexistent table | Failed commit retains rows; observed latch and corrected-table retry produce exact contents |
| `test_live_lost_insert_response_retains_uncertain_rows` | Real successful insertion followed by an **injected client error** before caller confirmation | Rows remain owned despite server insertion; retry demonstrates the documented duplicate window with two identical server rows |
| `test_live_foreground_rejection_retry_admits_input_once` | **Real server rejection**, then corrected-table retry of the same admitted write | Exact single row, without appending the retried payload twice |
| `test_live_close_rejection_retains_buffer_for_commit_recovery` | **Real server rejection** during close | Closed sink retains rows; subsequent commit on that same instance recovers exact contents |

The lost-response case does not drop packets or remove a response from the
native protocol socket. It proves uncertainty handling against real inserted
contents using a controlled failure at the client boundary.

## Actual server outage campaign

The [guarded runner](../../../../../scripts/verification/clickhouse_outage_replay.py)
verified two additional scenarios **three times each**. Before every Docker
operation it checks the exact fixture name. Credentials are read from
`TRAM_TEST_CLICKHOUSE_URL`; the native endpoint is fixed to the isolated port.

1. `real-outage-buffer-restart-commit`: stop the real server; commit must fail
   and retain both exact rows. Restart the server and confirm the table is
   still empty. Observe the previous error latch, then commit successfully and
   read back exactly the retained rows.
2. `real-outage-threaded-source-replay`: stop the real server during a
   `thread_workers=2` pipeline using the actual local source and ClickHouse
   sink. The failed run keeps the source bytes unchanged, does not move the
   input and does not mark it processed; the failed sink retains both rows.
   After restarting the server, fresh source/sink instances replay the input.
   Only the successful output barrier allows move and `mark_processed`.
   Confirm four exact table rows: the previous two direct-commit rows plus the
   two replay rows, with no unexpected identities or duplicates.

The server was stopped and started six times and restored to running state.
Each repetition created and dropped its own table.

Raw results:

- [Complete campaign log](campaign.log)
- [Repetition 1](repetition-1.json)
- [Repetition 2](repetition-2.json)
- [Repetition 3](repetition-3.json)
- [Combined machine-readable results](summary.json)

[The initial runner attempt](initial-runner-attempt.log) stopped after a
standalone harness omitted serializer registration. Its cleanup restarted the
fixture. It is preserved separately and is not counted among the three
successful campaign repetitions.

Reproduce from the repository root using a Python environment with project,
manager and ClickHouse dependencies and the authorized isolated fixture:

```bash
PYTHONPATH=. python scripts/verification/clickhouse_outage_replay.py
TRAM_TEST_CLICKHOUSE_NATIVE_PORT=19083 python -m pytest \
  tests/unit/test_clickhouse_live_broker.py -q -o log_cli=false
```

Set `TRAM_TEST_CLICKHOUSE_URL` in the environment before these commands.

## Evidence limits and pending scope

This campaign proves actual **ClickHouse server process restart** and source
replay through rebuilt connector instances. Replay ran in the same Python
process; it does not claim a TRAM worker-agent process restart, worker-journal
recovery or restart of an existing deployment. Physical network response loss
remains distinct from the controlled client-boundary uncertainty test.

No external fixture blocker remains for this ClickHouse campaign. H183-05's
original PGW ASN.1 `thread_workers=2` throughput/RSS benchmark remains blocked
on the matching corpus and pinned resource conditions. Neither these JSON
records nor the SFTP publication tests close the historical <900 MiB PGW
target. Required scenarios lacking evidence remain pending validation rather
than being described as blocked by those PGW inputs.
