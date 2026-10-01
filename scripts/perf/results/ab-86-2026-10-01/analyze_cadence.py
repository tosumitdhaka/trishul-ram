import glob, json, os, re
from datetime import datetime

def iso(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))

# all mock log lines with method + epoch
events = []
for line in open("/tmp/opencode/v160-86/logs/mock-rest.log", errors="replace"):
    m = re.match(r"REQ /(\S+).* t=([0-9.]+)", line)
    if m:
        events.append((m.group(1), float(m.group(2))))

for d in sorted(glob.glob("/tmp/opencode/v160-86/results/ab-*-s3*-M-*")) + sorted(glob.glob("/tmp/opencode/v160-86/results/ab-*-s3*-H-*")):
    run_id = os.path.basename(d)
    try:
        rows = json.load(open(f"{d}/run-rows.json"))
        t0 = iso(open(f"{d}/t0.txt").read().strip())
        mine = [r for r in rows if r.get("started_at") and iso(r["started_at"]) >= t0]
        r = mine[-1]
        start, end = iso(r["started_at"]).timestamp(), iso(r["finished_at"]).timestamp()
        pages = [t for meth, t in events if meth == "records" and start - 0.5 <= t <= end + 0.5]
        posts = [t for meth, t in events if meth == "collect" and start - 0.5 <= t <= end + 0.5]
        if len(pages) < 2:
            print(f"{run_id}: pages={len(pages)} (window mismatch)")
            continue
        gaps = sorted(b - a for a, b in zip(pages, pages[1:]))
        p50, p95 = gaps[len(gaps)//2], gaps[int(len(gaps)*0.95)]
        print(f"{run_id}: pages={len(pages)} posts={len(posts)} span={pages[-1]-pages[0]:.1f}s "
              f"p50gap={p50*1000:6.1f}ms p95gap={p95*1000:6.1f}ms maxgap={max(gaps)*1000:7.1f}ms")
    except Exception as e:
        print(f"{run_id}: ERR {e}")
