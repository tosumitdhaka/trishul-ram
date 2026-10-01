import json, sys, glob, os
from datetime import datetime

def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

for d in sorted(glob.glob("/tmp/opencode/v160-86/results/ab-*")):
    run_id = os.path.basename(d)
    try:
        rows = json.load(open(f"{d}/run-rows.json"))
        t0 = open(f"{d}/t0.txt").read().strip()
        t0dt = iso(t0)
        mine = [r for r in rows if r.get("started_at") and iso(r["started_at"]) >= t0dt]
        if not mine:
            print(f"{run_id}: NO ROWS after t0={t0}")
            continue
        r = mine[-1]
        dur = (iso(r["finished_at"]) - iso(r["started_at"])).total_seconds()
        thr = r["records_in"] / dur if dur else 0
        print(f"{run_id}: status={r.get('status')} in={r['records_in']} out={r['records_out']} "
              f"dur={dur:.2f}s thr={thr:,.0f} rec/s errors={len(r.get('errors') or [])}")
    except Exception as e:
        print(f"{run_id}: ERROR {e}")
