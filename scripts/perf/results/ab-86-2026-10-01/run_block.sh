#!/usr/bin/env bash
# run_block.sh <topo> <prof> <rep> [extra scenario...]
# Runs the standard scenario order (s2csv, s2pmxml, s3) + optional extras for one
# block (one deployment). Assumes deployment + staging already done.
set -uo pipefail
TOPO="$1"; PROF="$2"; REP="$3"; shift 3
EXTRAS=("$@")
LOG=/tmp/opencode/v160-86/logs/blocks.log
for scen in s2csv s2pmxml s3 "${EXTRAS[@]}"; do
  echo "=== $(date -u +%FT%TZ) cell ${TOPO}-${scen}-${PROF}-rep${REP} START" >> "$LOG"
  /tmp/opencode/v160-86/run_cell.sh "$TOPO" "$PROF" "$scen" "$REP" >> "$LOG" 2>&1
  rc=$?
  echo "=== $(date -u +%FT%TZ) cell ${TOPO}-${scen}-${PROF}-rep${REP} END rc=$rc" >> "$LOG"
done
echo "BLOCK ${TOPO}-${PROF}-rep${REP} DONE" >> "$LOG"
