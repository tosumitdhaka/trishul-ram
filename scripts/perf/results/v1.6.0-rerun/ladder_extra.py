import sys, json, time
sys.path.insert(0, "/tmp/opencode/v160-rerun")
from ladder import run_step, parse_loadgens, RESULTS, ROOT, PERF, PY
from pathlib import Path
for rate, k in ((7200, 12), (9600, 16)):
    per = rate // k
    dur = max(15, min(180, 120000 // rate))
    run_id = f"sat-s6-M-extra-{rate}mps"
    lines = ["sleep 5"]
    for j in range(1, k + 1):
        lines.append(
            f"{PY} {PERF}/generators/kafka_loadgen.py --brokers 172.19.0.2:30094 "
            f"--topic perf-cdr --rate {per} --duration {dur} "
            f"--payload-file /tmp/opencode/perf-b/batches/flat/batch_000001.jsonl &")
    lines.append("wait")
    script = ROOT / f"lg-s6-extra-{rate}.sh"
    script.write_text("\n".join(lines) + "\n")
    log_text, rc, rows = run_step("s6-kafka-local", "s6_kafka_local.yaml", run_id,
                                  f"bash {script}", 15, dur, dur + 30)
    lgs = parse_loadgens(log_text, "kafka_loadgen")
    produced = sum(l.get("sent", 0) for l in lgs)
    from run_cell import summarize
    hist = summarize(rows)
    print(f"[{run_id}] dur={dur}s produced={produced} consumed={hist['records_in']} "
          f"out={hist['records_out']} skipped={hist['records_skipped']} "
          f"rate={produced/dur:.0f}/s nodes={','.join(hist['nodes'])}", flush=True)
