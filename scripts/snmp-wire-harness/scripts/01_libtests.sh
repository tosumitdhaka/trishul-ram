#!/usr/bin/env bash
# Check 1: run both libraries' own pytest suites at the installed versions.
# Source trees exported via `git archive v0.5.1` from the local checkouts and
# verified byte-identical to the venv-installed packages.
set -u
BASE=/tmp/opencode/tsmi-smoke-v06
PY=$BASE/venv/bin/python
RESULTS=$BASE/results
mkdir -p "$RESULTS"

run_suite() {
    local dir="$1" tag="$2" out="$3"
    (cd "$dir" && $PY -m pytest tests -q > "$out" 2>&1)
    echo $? > "$out.rc"
    tail -3 "$out"
}

echo "=== trishul-snmp 0.6.1 own suite ==="
run_suite "$BASE/src/trishul-snmp" tsmp "$RESULTS/libtests-trishul-snmp.txt"
SNMP_RC=$(cat "$RESULTS/libtests-trishul-snmp.txt.rc")

echo "=== trishul-smi 0.5.2 own suite ==="
run_suite "$BASE/src/trishul-smi" tsmi "$RESULTS/libtests-trishul-smi.txt"
SMI_RC=$(cat "$RESULTS/libtests-trishul-smi.txt.rc")

SNMP_TAIL=$(tail -1 "$RESULTS/libtests-trishul-snmp.txt")
SMI_TAIL=$(tail -1 "$RESULTS/libtests-trishul-smi.txt")

python3 - "$SNMP_RC" "$SMI_RC" "$SNMP_TAIL" "$SMI_TAIL" <<'EOF'
import json, sys
snmp_rc, smi_rc, snmp_tail, smi_tail = sys.argv[1:5]
ok = snmp_rc == "0" and smi_rc == "0"
ev = f"tsmp 0.6.1: rc={snmp_rc} {snmp_tail.strip()} | tsmi 0.5.2: rc={smi_rc} {smi_tail.strip()}"
print(f"[{'PASS' if ok else 'FAIL'}] lib-test-suites: {ev}")
with open("/tmp/opencode/tsmi-smoke-v06/results/lib-test-suites.json", "w") as fh:
    json.dump({"check": "lib-test-suites", "ok": ok, "evidence": ev}, fh, indent=2)
EOF
