#!/usr/bin/env bash
# TRAM release gate — mandatory pre-release verification. See docs/release-gate.md
# for the full release process this gate enforces.
#
# Usage:
#   scripts/release-gate.sh                    full gate (all checks)
#   scripts/release-gate.sh --fast            skip the slow checks (pytest, UI build)
#   scripts/release-gate.sh --ci               CI mode: skip the "already tagged" check
#                                              (the CI gate runs on the release tag itself)
#   scripts/release-gate.sh --deploy-kind     after a green gate: deploy the current
#                                              tree to the kind dev cluster for manual
#                                              smoke testing (combines with --fast);
#                                              prints the NodePort UI URL when done
#
# Exits 0 only when every check passes.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FAST=0
CI=0
DEPLOY_KIND=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fast) FAST=1; shift ;;
    --ci) CI=1; shift ;;
    --deploy-kind) DEPLOY_KIND=1; shift ;;
    *) echo "unknown option: $1 (supported: --fast, --ci, --deploy-kind)"; exit 2 ;;
  esac
done

PASS=0
FAIL=0
ROWS=()

record() { # record <PASS|FAIL> <name> [detail]
  local status="$1" name="$2" detail="${3:-}"
  ROWS+=("$status"$'\t'"$name"$'\t'"$detail")
  if [ "$status" = "PASS" ]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); fi
}

need() { command -v "$1" >/dev/null 2>&1; }

echo "== TRAM release gate =="

VERSION="$(grep -oP '(?<=^version = ").*(?=")' pyproject.toml)"
echo "version under gate: ${VERSION:-<not found in pyproject.toml>}"

# --- preflight ---------------------------------------------------------------

# 1. clean working tree
if [ -z "$(git status --porcelain)" ]; then
  record PASS "clean working tree"
else
  record FAIL "clean working tree" "commit or stash $(git status --porcelain | wc -l) change(s) first"
fi

# 2. version consistency across pyproject, Helm chart, and UI package
CHART_VERSION="$(grep -oP '(?<=^version: ).+' helm/Chart.yaml | tr -d '"')"
CHART_APPVERSION="$(grep -oP '(?<=^appVersion: ").+(?=")' helm/Chart.yaml)"
UI_VERSION="$(grep -oP '(?<=^  "version": ").*(?=")' tram/ui/package.json)"
mismatch=""
[ "$CHART_VERSION" = "$VERSION" ] || mismatch="helm/Chart.yaml version=$CHART_VERSION"
[ "$CHART_APPVERSION" = "$VERSION" ] || mismatch="${mismatch:+$mismatch; }helm/Chart.yaml appVersion=$CHART_APPVERSION"
[ "$UI_VERSION" = "$VERSION" ] || mismatch="${mismatch:+$mismatch; }tram/ui/package.json=$UI_VERSION"
if [ -z "$mismatch" ]; then
  record PASS "version consistency (pyproject/chart/ui)"
else
  record FAIL "version consistency (pyproject/chart/ui)" "$mismatch"
fi

# 3. version not already tagged (meaningless on the CI tag itself)
if [ "$CI" -eq 1 ]; then
  record PASS "version not already tagged (skipped: --ci runs on the tag)"
elif [ -n "$(git tag -l "v$VERSION")" ]; then
  record FAIL "version not already tagged" "v$VERSION exists — bump the version before gating"
else
  record PASS "version not already tagged"
fi

# 4. changelog section for the version being released
if grep -qF "## [$VERSION]" docs/changelog.md; then
  record PASS "changelog entry for $VERSION"
else
  record FAIL "changelog entry" "docs/changelog.md has no '## [$VERSION]' section — rename [Unreleased] first"
fi

# --- checks ------------------------------------------------------------------

# 5. lint
if ruff check . >/dev/null 2>&1; then
  record PASS "ruff check"
else
  record FAIL "ruff check" "run: ruff check ."
fi

# 6. full test suite + coverage floor (slow)
if [ "$FAST" -eq 1 ]; then
  record PASS "pytest + coverage floor (skipped: --fast)"
else
  if pytest tests/unit/ tests/integration/ -q --tb=short --cov=tram \
      --cov-report=term-missing --cov-fail-under=75 >/tmp/tram-gate-pytest.log 2>&1; then
    record PASS "pytest + coverage floor (75%)"
  else
    record FAIL "pytest + coverage floor (75%)" \
      "$(tail -n 5 /tmp/tram-gate-pytest.log | tr '\n' ' ' | cut -c1-160)"
  fi
fi

# 7. UI build (slow)
if [ "$FAST" -eq 1 ]; then
  record PASS "UI build (skipped: --fast)"
elif ! need npm; then
  record FAIL "UI build" "npm not found"
else
  if (cd tram/ui && npm ci --no-audit --no-fund >/tmp/tram-gate-npm.log 2>&1 \
      && npm run build >>/tmp/tram-gate-npm.log 2>&1); then
    record PASS "UI build (vite)"
  else
    record FAIL "UI build (vite)" "$(tail -n 3 /tmp/tram-gate-npm.log | tr '\n' ' ' | cut -c1-160)"
  fi
fi

# 8. every example pipeline validates. The console script is invoked with
#    PYTHONPATH=$ROOT so it works from a plain checkout even without an
#    editable install (a stale wrapper on PATH would otherwise report every
#    file as failing with no usable error).
if ! need tram; then
  record FAIL "pipeline examples validate" "tram CLI not found — pip install -e '.[dev,all]'"
elif ! PYTHONPATH="$ROOT" tram version >/dev/null 2>&1; then
  record FAIL "pipeline examples validate" "tram CLI on PATH is not functional — reinstall: pip install -e '.[dev,all]'"
else
  bad=""
  for f in pipelines/*.yaml; do
    PYTHONPATH="$ROOT" tram validate "$f" >/dev/null 2>&1 || bad="$bad $f"
  done
  if [ -z "$bad" ]; then
    record PASS "pipeline examples validate ($(ls pipelines/*.yaml | wc -l) files)"
  else
    record FAIL "pipeline examples validate" "failing:$bad"
  fi
fi

# 9. Helm chart lints and renders (from a temp copy so the working tree stays
#    clean — dependency update fetches the postgresql subchart)
if ! need helm; then
  record FAIL "helm lint + template" "helm not found"
else
  HELM_TMP="$(mktemp -d)"
  if cp -r helm "$HELM_TMP/chart" 2>/dev/null \
      && helm dependency update "$HELM_TMP/chart" >"$HELM_TMP/dep.log" 2>&1 \
      && helm lint "$HELM_TMP/chart" >"$HELM_TMP/lint.log" 2>&1 \
      && helm template gate-check "$HELM_TMP/chart" >/dev/null 2>"$HELM_TMP/tmpl.log"; then
    record PASS "helm lint + template"
  else
    detail="$(tail -qn 3 "$HELM_TMP"/dep.log "$HELM_TMP"/lint.log "$HELM_TMP"/tmpl.log 2>/dev/null | tr '\n' ' ' | cut -c1-160)"
    record FAIL "helm lint + template" "$detail"
  fi
  rm -rf "$HELM_TMP"
fi

# 10. docs-sync: every TRAM_* environment variable referenced in the Python
#     source must be documented in .env.example (best-effort static scan)
missing_env=""
while IFS= read -r var; do
  grep -qE "^#?[[:space:]]*${var}=" .env.example || missing_env="$missing_env $var"
done < <(grep -rhoP 'TRAM_[A-Z0-9_]+' tram --include='*.py' | sort -u)
if [ -z "$missing_env" ]; then
  record PASS "docs-sync: env vars documented in .env.example"
else
  record FAIL "docs-sync: env vars documented in .env.example" "undocumented:$missing_env"
fi

# --- summary -----------------------------------------------------------------

echo
echo "----------------------------- gate summary -----------------------------"
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r status name detail <<<"$row"
  if [ -n "$detail" ]; then
    printf '  [%s] %-42s %s\n' "$status" "$name" "$detail"
  else
    printf '  [%s] %s\n' "$status" "$name"
  fi
done
echo "------------------------------------------------------------------------"
echo "passed: $PASS   failed: $FAIL"

if [ "$FAIL" -gt 0 ]; then
  echo "GATE RED — resolve every FAIL above before tagging. See docs/release-gate.md."
  [ "$DEPLOY_KIND" -eq 1 ] && echo "--deploy-kind: skipping deploy (gate is red)."
  exit 1
fi
echo "GATE GREEN — ready to tag v$VERSION."

# --- optional: deploy the current tree to kind for manual smoke testing --------
# Builds the images from the working tree, loads them into the kind dev
# cluster, upgrades the Helm release, then prints the manager UI's NodePort
# URL (the kind cluster maps NodePorts directly to host ports — no
# port-forward needed).
if [ "$DEPLOY_KIND" -eq 1 ]; then
  echo
  echo "== manual validation deploy (--deploy-kind) =="
  for cmd in docker kind kubectl helm; do
    if ! need "$cmd"; then
      echo "--deploy-kind: missing required command: $cmd" >&2
      exit 1
    fi
  done
  echo "Deploying the current tree to kind (build, load, helm upgrade)…"
  "$ROOT/scripts/deploy-kind-tram-dev.sh"
  NS="${NAMESPACE:-trishul-ram}"
  REL="${RELEASE_NAME:-trishul-ram}"
  NODE_PORT="$(kubectl -n "$NS" get svc "$REL" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)"
  echo
  if [ -n "$NODE_PORT" ]; then
    echo "UI (NodePort):  http://localhost:${NODE_PORT}"
  else
    echo "UI:             http://localhost:30001 (NodePort not found — check: kubectl -n $NS get svc $REL)"
  fi
  echo "Note:           hard-refresh (Ctrl-Shift-R) — a cached SPA from an older"
  echo "                deployment shows a stale version and empty pages."
  echo
  echo "Manual smoke checklist (docs/release-gate.md §Manual validation):"
  echo "  - every confirm modal executes its action (Stop, Reload, rollback,"
  echo "    delete pipeline/MIB/schema/alert)"
  echo "  - deep links survive refresh; Back/Forward work"
  echo "  - dashboard '+ New' opens a blank editor; Save never overwrites"
fi
