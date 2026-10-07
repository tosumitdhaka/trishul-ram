import yaml

BENCH = "/home/dhaka/trishul/trishul-ram/scripts/perf/infra/values-bench.yaml"
SINGLE = "/home/dhaka/trishul/trishul-ram/scripts/perf/infra/values-single.yaml"
RES = {"M": "/home/dhaka/trishul/trishul-ram/scripts/perf/infra/values-res-M.yaml",
       "H": "/home/dhaka/trishul/trishul-ram/scripts/perf/infra/values-res-H.yaml"}
out = "/tmp/opencode/v160-86/values"

def deep_merge(base, overlay):
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base

def load(p):
    with open(p) as f:
        return yaml.safe_load(f) or {}

# mgr+worker cells: bench (topology + env) + res overlay + worker callback URL fix.
# --reuse-values carries env.TRAM_MANAGER_URL=localhost:8765 from the old
# single release; the chart appends .Values.env AFTER its built-in worker
# TRAM_MANAGER_URL (last wins), so we must pin the correct manager service URL.
for prof in ("M", "H"):
    doc = load(BENCH)
    deep_merge(doc, load(RES[prof]))
    doc["env"]["TRAM_MANAGER_URL"] = "http://trishul-ram:8765"
    with open(f"{out}/mw-{prof}.yaml", "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    print(f"mw-{prof}: env keys={len(doc['env'])} TRAM_MANAGER_URL={doc['env']['TRAM_MANAGER_URL']}")

# single cells: bench env + single topology + res overlay + local manager URL
for prof in ("M", "H"):
    doc = load(BENCH)
    deep_merge(doc, load(SINGLE))
    deep_merge(doc, load(RES[prof]))
    doc["env"]["TRAM_MANAGER_URL"] = "http://localhost:8765"
    with open(f"{out}/single-{prof}.yaml", "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    print(f"single-{prof}: env keys={len(doc['env'])} TRAM_MANAGER_URL={doc['env']['TRAM_MANAGER_URL']}")
