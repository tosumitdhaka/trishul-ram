import yaml
for prof in ("M", "H"):
    for topo in ("mw",):
        p = f"/tmp/opencode/v160-86/values/{topo}-{prof}.yaml"
        with open(p) as f:
            doc = yaml.safe_load(f)
        # drop any appended duplicate env override
        if not doc.get("env", {}).get("PERF_REST_URL"):
            raise SystemExit(f"env block missing PERF_REST_URL in {p} - abort")
        doc["env"]["TRAM_MANAGER_URL"] = "http://trishul-ram:8765"
        with open(p, "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False)
        print(p, "->", doc["env"]["TRAM_MANAGER_URL"], "| env keys:", len(doc["env"]))
