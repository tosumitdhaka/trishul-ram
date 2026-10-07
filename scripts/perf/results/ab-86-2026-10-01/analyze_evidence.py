import glob, json, os, re
from datetime import datetime

def parse_cpu(path):
    pods = {}
    cur = None
    for line in open(path):
        m = re.match(r"^== (\S+) ==", line.strip())
        if m:
            cur = m.group(1)
        elif line.startswith("usage_usec") and cur:
            pods[cur] = int(line.split()[1])
    return pods

def iso(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))

rows = []
for d in sorted(glob.glob("/tmp/opencode/v160-86/results/ab-*")):
    run_id = os.path.basename(d)
    try:
        before, after = parse_cpu(f"{d}/cpu_stat_before.txt"), parse_cpu(f"{d}/cpu_stat_after.txt")
        rows_json = json.load(open(f"{d}/run-rows.json"))
        t0 = iso(open(f"{d}/t0.txt").read().strip())
        mine = [r for r in rows_json if r.get("started_at") and iso(r["started_at"]) >= t0]
        r = mine[-1]
        dur = (iso(r["finished_at"]) - iso(r["started_at"])).total_seconds()
        exec_pod = None
        for p in after:
            if p in before:
                delta_ms = (after[p] - before[p]) / 1000.0
                # executing pod: CPU delta roughly proportional to work; also check log
                logf = f"{d}/podlogs/{p}.log"
                ran = os.path.exists(logf) and "Batch run started" in open(logf, errors="replace").read()
                if ran:
                    exec_pod = p
                    cpu_avg = delta_ms / 1000.0 / dur if dur else 0
                    rows.append((run_id, exec_pod, dur, cpu_avg, delta_ms))
    except Exception as e:
        rows.append((run_id, f"ERR {e}", 0, 0, 0))

print(f"{'run_id':34s} {'exec_pod':24s} {'dur_s':>6s} {'avg_cpu_cores':>13s}")
for r in rows:
    print(f"{r[0]:34s} {str(r[1]):24s} {r[2]:6.2f} {r[3]:13.2f}")
