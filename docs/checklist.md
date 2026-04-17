# Development & Release Checklist

This checklist ensures consistency across code changes, documentation, and releases.

## Pre-Commit Checklist (All Changes)

### Code Quality
- [ ] Run `ruff check .` — no new lint errors
- [ ] Run `ruff check --fix .` — auto-fix formatting issues
- [ ] Run unit tests: `pytest tests/unit/ -q`
- [ ] Run integration tests: `pytest tests/integration/ -q`
- [ ] Coverage check: `pytest tests/ --cov=tram --cov-report=term-missing --cov-fail-under=60`
- [ ] No secrets in code (API keys, passwords, tokens)
- [ ] No hardcoded paths (use environment variables)

### New Connector Checklist
- [ ] Config model added to `tram/models/pipeline.py`
- [ ] Class decorated with `@register_source()` or `@register_sink()`
- [ ] Import added to `tram/connectors/__init__.py`
- [ ] Optional dependency added to `pyproject.toml` (if needed)
- [ ] Entry added to plugin table in `README.md`
- [ ] Test file created in `tests/unit/connectors/`
- [ ] Example pipeline added to `pipelines/` (optional)
- [ ] Documented in `docs/connectors.md`

### New Transform Checklist
- [ ] Class decorated with `@register_transform()`
- [ ] Import added to `tram/transforms/__init__.py`
- [ ] Optional dependency added to `pyproject.toml` (if needed)
- [ ] Entry added to plugin table in `README.md`
- [ ] Test file created in `tests/unit/transforms/`
- [ ] Documented in `docs/transforms.md`

### New Serializer Checklist
- [ ] Class decorated with `@register_serializer()`
- [ ] Import added to `tram/serializers/__init__.py`
- [ ] Optional dependency added to `pyproject.toml` (if needed)
- [ ] Entry added to plugin table in `README.md`
- [ ] Test file created in `tests/unit/serializers/`

### Environment Variables
- [ ] New env var added to `.env.example` with description
- [ ] Documented in `docs/deployment.md` (Environment Variables section)
- [ ] Added to Helm `values.yaml` (if applicable)
- [ ] Added to Docker Compose `environment` section (if applicable)

### Documentation Updates
When making changes that affect user-facing behavior, update:
- [ ] `README.md` — Quick Start, plugin tables, features
- [ ] `docs/changelog.md` — add entry under `## [Unreleased]`
- [ ] `docs/architecture.md` — if core flow/component changes
- [ ] `docs/api.md` — if REST endpoints added/changed
- [ ] `docs/connectors.md` — if connector behavior changes
- [ ] `docs/transforms.md` — if transform behavior changes
- [ ] `docs/deployment.md` — if deployment config changes
- [ ] `CLAUDE.md` — if architecture patterns change

### API Changes
- [ ] OpenAPI schema updated (FastAPI auto-generates, but verify)
- [ ] New endpoint added to API table in `README.md`
- [ ] New endpoint added to `docs/api.md`
- [ ] Frontend API client updated (`tram/ui/src/api.js`)
- [ ] Auth/rate-limit middleware applied correctly

### UI Changes
- [ ] Test in both light and dark mode
- [ ] Test responsive layout (mobile/tablet/desktop)
- [ ] Update relevant page in `tram/ui/src/pages/`
- [ ] Run `cd tram/ui && npm run build` to verify no build errors
- [ ] Check for console errors in browser dev tools

### Docker/Helm Changes
- [ ] `Dockerfile` — test build: `docker build -t tram:test .`
- [ ] `docker-compose.yml` — test: `docker compose up`
- [ ] `helm/values.yaml` — bump `image.tag` if needed
- [ ] `helm/Chart.yaml` — bump `version` and `appVersion` if needed
- [ ] Helm lint: `helm lint helm/`
- [ ] Test Helm install: `helm install tram-test helm/ --dry-run --debug`

---

## Version Release Checklist

### Pre-Release (Version X.Y.Z)

#### 1. Version Bump
- [ ] Update `pyproject.toml` `version = "X.Y.Z"`
- [ ] Update `README.md` `**Version:** X.Y.Z` (line 5)
- [ ] Update `helm/Chart.yaml`:
  - [ ] `version: X.Y.Z` (chart version)
  - [ ] `appVersion: "X.Y.Z"` (app version)
- [ ] Verify `tram/__init__.py` reads version from `importlib.metadata` (no hardcoded version)

#### 2. CHANGELOG Update
- [ ] Move `## [Unreleased]` items to `## [X.Y.Z] - YYYY-MM-DD`
- [ ] Add comparison link at bottom: `[X.Y.Z]: https://github.com/tosumitdhaka/tram/compare/vX.Y.(Z-1)...vX.Y.Z`
- [ ] Create new empty `## [Unreleased]` section

#### 3. Documentation Sync
- [ ] All version references in docs match X.Y.Z
- [ ] `README.md` / `docs/index.md` quick-start examples use `latest`, with production pin guidance for X.Y.Z
- [ ] Helm install examples use `latest` for quick start or pin `image.tag=X.Y.Z` for release-specific docs
- [ ] `helm/values.yaml` default image tag matches X.Y.Z
- [ ] Auth docs match implementation: `TRAM_AUTH_USERS` bootstrap behavior and DB `scrypt` password storage
- [ ] Feature tables include version tags (e.g., "v1.1.0")

#### 4. Testing (Full Suite)
- [ ] `pytest tests/unit/ -v` — all pass
- [ ] `pytest tests/integration/ -v` — all pass
- [ ] `pytest tests/ --cov=tram --cov-fail-under=60` — coverage OK
- [ ] `ruff check .` — no errors
- [ ] Test docker build: `docker build -t tram:X.Y.Z .`
- [ ] Test docker run: `docker run -p 8765:8765 tram:X.Y.Z`
- [ ] Verify `curl http://localhost:8765/api/meta` returns correct version
- [ ] Test UI: open `http://localhost:8765/ui/` — no console errors

#### 5. Example Pipelines
- [ ] All pipelines in `pipelines/` validate: `tram validate pipelines/*.yaml`
- [ ] Test dry-run on at least 3 example pipelines
- [ ] Test template download endpoint: `curl http://localhost:8765/api/templates`

#### 6. CI/CD Check
- [ ] `.github/workflows/ci.yml` runs successfully on main branch
- [ ] Coverage upload artifact generated
- [ ] No flaky tests

#### 7. Git Commit
- [ ] Stage version bump files:
  ```bash
  git add pyproject.toml README.md docs/changelog.md helm/Chart.yaml
  ```
- [ ] Commit with message: `chore: bump version to X.Y.Z`
- [ ] **Do NOT push yet** — verify release workflow first

#### 8. Release Workflow Verification
- [ ] Check `.github/workflows/release.yml` is configured
- [ ] Verify Docker registry credentials are set (GitHub secrets)
- [ ] Verify Helm chart registry is configured
- [ ] Verify release workflow publishes both versioned and `latest` tags for manager and worker images

---

### Release (Create Tag)

- [ ] Push version commit: `git push origin main`
- [ ] Create and push tag:
  ```bash
  git tag -a vX.Y.Z -m "Release version X.Y.Z"
  git push origin vX.Y.Z
  ```
- [ ] Monitor release workflow in GitHub Actions
- [ ] Verify Docker image pushed: `ghcr.io/tosumitdhaka/trishul-ram:X.Y.Z`
- [ ] Verify Helm chart pushed: `oci://ghcr.io/tosumitdhaka/charts/trishul-ram:X.Y.Z`

---

### Post-Release

#### 1. GitHub Release
- [ ] Create GitHub release from tag `vX.Y.Z`
- [ ] Copy CHANGELOG entry for X.Y.Z into release notes
- [ ] Attach any relevant artifacts (if applicable)
- [ ] Mark as "Latest release"

#### 2. Verification
- [ ] Test pull image: `docker pull ghcr.io/tosumitdhaka/trishul-ram:X.Y.Z`
- [ ] Test Helm install:
  ```bash
  helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram --version X.Y.Z --dry-run
  ```
- [ ] Verify version endpoint: `curl http://localhost:8765/api/meta`

#### 3. Update `latest` Tag (Optional)
- [ ] Tag as latest:
  ```bash
  git tag -f latest
  git push origin latest --force
  ```

#### 4. Announce
- [ ] Update any external documentation
- [ ] Notify users/team of new release
- [ ] Post release notes to relevant channels

---

## Hotfix Release Checklist

For urgent fixes (X.Y.Z+1):

- [ ] Create hotfix branch: `git checkout -b hotfix/X.Y.(Z+1) vX.Y.Z`
- [ ] Apply minimal fix (no new features)
- [ ] Follow **Pre-Release** checklist (version X.Y.Z+1)
- [ ] Test thoroughly (unit + integration)
- [ ] Merge to main: `git checkout main && git merge hotfix/X.Y.(Z+1)`
- [ ] Follow **Release** checklist
- [ ] Delete hotfix branch: `git branch -d hotfix/X.Y.(Z+1)`

---

## Rollback Checklist

If a release has critical issues:

- [ ] Identify last known good version (X.Y.Z-1)
- [ ] Tag rollback release: `vX.Y.Z-rollback` pointing to `vX.Y.Z-1`
- [ ] Update Helm deployments:
  ```bash
  helm upgrade tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram --version X.Y.Z-1
  ```
- [ ] Update CHANGELOG with rollback note
- [ ] Communicate issue and rollback to users
- [ ] Create issue to track fix for next release

---

## Notes

- **Always test locally before pushing** — CI catches most issues, but local verification is faster
- **Never skip tests** — coverage regression means something important is untested
- **Version bumps are atomic** — all version references must change together
- **CHANGELOG is user-facing** — write clear, concise entries with examples
- **Tag format is strict** — always `vX.Y.Z` (lowercase 'v' prefix)

---

## v1.2.1 Release Status — 2026-04-14

### Code Quality
- [x] `ruff check .` — 5 fixable issues auto-corrected, 0 remaining
- [x] `ruff check --fix .` — applied
- [x] Unit tests: `pytest tests/unit/ -q` — **911 passed**
- [ ] Integration tests — not run (no live SFTP/Kafka in CI)
- [ ] Coverage check — not run separately (unit suite passes)
- [x] No secrets in code
- [x] No hardcoded paths

### Changes in this release
- [x] `tram/core/context.py` — `note_skip()` method added
- [x] `tram/pipeline/executor.py` — skip logs WARNING + `note_skip()` call
- [x] `tram/agent/server.py` — `errors` list propagated in worker callback
- [x] `tram/api/routers/internal.py` — `RunCompletePayload.errors` field added
- [x] `tram/pipeline/controller.py` — `on_worker_run_complete` accepts + stores `errors`
- [x] `tram/core/log_config.py` — `httpx` logger silenced to WARNING
- [x] `tram/agent/worker_pool.py` — single summary health log on count change
- [x] `tram/api/routers/health.py` — `/api/ready` returns `cluster` field
- [x] `tram-ui/src/pages/settings.js` — Daemon Status cluster field fixed
- [x] `tram-ui/src/pages/dashboard.js` — Start / Stop / Download buttons
- [x] `tram-ui/src/pages/detail.html` — Run Now trigger button
- [x] `tram-ui/src/pages/detail.js` — `_detailTrigger` (one-shot) vs `_detailRun` (schedule)
- [x] `tram-ui/src/pages/cluster.js` — per-worker assigned/running pipelines
- [x] CSS vars applied across editor, wizard, cluster, plugins, settings, templates
- [x] `aria-label` on all unlabelled form controls
- [x] `tests/unit/test_api_internal_router.py` — updated for `errors` field

### Version Bump
- [x] `pyproject.toml` → `1.2.1`
- [x] `README.md` → `1.2.1`
- [x] `helm/Chart.yaml` → `1.2.1`
- [x] `CHANGELOG.md` — `[1.2.1]` section added, comparison link added

### Docker / Helm
- [x] `docker build -t trishul-ram:1.2.4 .` — built successfully
- [x] `docker build -t trishul-ram-worker:1.2.4 . -f Dockerfile.worker` — built successfully
- [x] `helm upgrade` — REVISION 10, all 4 pods `1/1 Running`
- [x] Manager logs clean — single `Worker pool: 3/3 healthy` after startup
- [x] Settings page shows `manager · 3/3 workers`
- [x] Worker 401s resolved — `TRAM_API_KEY` set on all pods

### Pending (post-release)
- [ ] `git tag -a v1.2.1 -m "Release version 1.2.1" && git push origin v1.2.1`
- [ ] GitHub release created from tag
- [ ] Push images to registry: `ghcr.io/tosumitdhaka/trishul-ram:1.2.1`

---

## v1.2.2 — 2026-04-15

### Fixes
- `tram/cli/main.py` — `validate` and `run --dry-run` crashed unpacking `(config, raw_yaml)` tuple returned by `load_pipeline()`
- `tram/watcher/pipeline_watcher.py` — hot-reload raised `PipelineAlreadyExistsError` on changed YAML; fixed by passing `replace=True` to `manager.register()`
- `docs/api.md` — response shapes for dry-run, connector-test, and change-password corrected to match implementation
- `docs/connectors.md` — `on_error` valid values corrected (`continue | abort | retry | dlq`; `stop` was never valid)
- `pyproject.toml` — removed `tram[corba]` from `all` extra; `omniORBpy` is a system package with no PyPI wheel (CI was failing)
- `tests/unit/test_auth_utils.py` — `test_returns_sha256_prefix` → `test_returns_scrypt_prefix`; password hasher was upgraded to scrypt in v1.2.1 but test was not updated

### Changes
- `tram/ui/` — web UI source moved from `tram-ui/` to `tram/ui/`; `Dockerfile` updated
- `docs/changelog.md` — moved from `CHANGELOG.md` (root)
- `docs/checklist.md` — moved from `CHECKLIST.md` (root)
- `docs/roadmap.md` — replaces `docs/roadmap_1.2.0.md`; features/issues only, versioned or backlog
- `README.md` — overhauled: use-case driven (PM, FM, gNMI, syslog, CORBA), concise, links to docs
- `.gitignore` — added `CLAUDE.md`, `AGENTS.md`, `.codex`

### Tests
- Coverage raised from ~67% to 78.5% (1,296 passing, 0 failed)
- 9 new test files covering AI router, CLI, daemon server, pipeline manager/controller/watcher, API middleware, stats, serializers
- 25 ruff lint errors in test files resolved

### Version bumps
- `pyproject.toml` → `1.2.2`
- `helm/Chart.yaml` → `1.2.2`

---

## v1.2.3 — 2026-04-16

### Scope
- SNMPv3 poll validation completed for real `GET` and `WALK`
- ASN.1 decode-path hardening completed
- SNMP trap source deferred due to push-source architecture gap (`#11`)
- SNMP trap sink deferred pending a reachable real receiver

### Verified
- [x] `ruff check .` — passed
- [x] `pytest tests/unit/ -q -o log_cli=false` — **1278 passed**
- [x] `pytest tests/unit/test_loader.py -q -o log_cli=false` — **17 passed**
- [x] `pytest tests/unit/test_snmp_connectors.py -q -o log_cli=false` — **59 passed**
- [x] `pytest tests/integration/ -q -o log_cli=false` — **44 passed**
- [x] `pytest tests/ --cov=tram --cov-fail-under=60 -o log_cli=false` — **79.43% coverage**
- [x] Bundled pipeline examples validate
- [x] `scripts/deploy-kind-tram-dev.sh` rebuilt and rolled out local kind release
- [x] Live manager `/api/meta` verified through port-forward: `{"version":"1.2.3", ...}`
- [x] Real SNMPv3 `GET` pipeline validated against host SNMP agent
- [x] Real SNMPv3 `WALK` pipeline validated against host SNMP agent

### Fixes included
- [x] `tram/connectors/snmp/source.py` — walk loop no-progress guard added for repeated terminal OIDs
- [x] `tram/connectors/snmp/sink.py` / `tram/models/pipeline.py` — `trap_oid` introduced; legacy `enterprise_oid` kept as alias
- [x] `tram/api/routers/auth.py` — DB-backed browser auth works without `TRAM_AUTH_USERS`
- [x] `tram/pipeline/controller.py` — full UUID run IDs retained
- [x] Worker callback timestamps preserved through manager history path
- [x] ASN.1 serializer docs/tests updated for explicit decode-only behavior

### Version bumps
- [x] `pyproject.toml` → `1.2.3`
- [x] `helm/Chart.yaml` → `1.2.3`
- [x] `docs/index.md` / `docs/deployment.md` / `docs/changelog.md` updated for `1.2.3`

### Pending (post-bump)
- [ ] Tag and publish `v1.2.3`
