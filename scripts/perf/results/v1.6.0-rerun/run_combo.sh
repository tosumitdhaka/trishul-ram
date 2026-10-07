#!/usr/bin/env bash
# run_combo.sh <topo> <profile> <scen1,scen2,...> — run all cells (2 reps each)
# for one deployed topology×profile combo, detached-safe. Per-cell failures are
# logged and the run continues.
set -u
TOPO="$1"; PROF="$2"; SCENS="$3"
ROOT=/tmp/opencode/v160-rerun
LOG=$ROOT/logs/combo-$TOPO-$PROF.log
PY=/home/dhaka/trishul/trishul-ram/.venv/bin/python
IFS=',' read -ra ARR <<< "$SCENS"
echo "=== COMBO $TOPO-$PROF start $(date -u +%FT%TZ) scens=${ARR[*]}" >> "$LOG"
for rep in 1 2; do
  for scen in "${ARR[@]}"; do
    echo "--- $(date -u +%FT%TZ) cell $TOPO-$scen-$PROF-rep$rep" >> "$LOG"
    "$PY" "$ROOT/drivers/run_cell.py" "$TOPO" run "$scen" "$PROF" "$rep" >> "$LOG" 2>&1
    echo "--- $(date -u +%FT%TZ) rc=$?" >> "$LOG"
  done
done
echo "=== COMBO $TOPO-$PROF DONE $(date -u +%FT%TZ)" >> "$LOG"
