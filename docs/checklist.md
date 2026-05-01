# Development & Release Checklist

This checklist is the current source of truth for local validation, version bumps,
and release execution. Historical release notes belong in `docs/changelog.md`,
not here.

## Pre-Commit Checklist

### Code Quality
- [ ] Run `ruff check --fix .`
- [ ] Run `ruff check .`
- [ ] Run `pytest tests/unit/ -q`
- [ ] Run `pytest tests/integration/ -q`
- [ ] Run `pytest tests/ --cov=tram --cov-report=term-missing --cov-fail-under=75`
- [ ] No secrets in code, example pipelines, docs, or Helm values
- [ ] No new hardcoded host paths without config or env override

### When Adding a Connector
- [ ] Add config model in `tram/models/pipeline.py`
- [ ] Decorate implementation with `@register_source()` or `@register_sink()`
- [ ] Add import in `tram/connectors/__init__.py`
- [ ] Add optional dependency in `pyproject.toml` if required
- [ ] Add tests under `tests/unit/connectors/`
- [ ] Update `README.md` plugin table if user-facing
- [ ] Update `docs/connectors.md`
- [ ] Add or refresh a sample pipeline in `pipelines/` when useful

### When Adding a Transform
- [ ] Decorate implementation with `@register_transform()`
- [ ] Add import in `tram/transforms/__init__.py`
- [ ] Add optional dependency in `pyproject.toml` if required
- [ ] Add tests under `tests/unit/transforms/`
- [ ] Update `README.md` if user-facing
- [ ] Update `docs/transforms.md`

### When Adding a Serializer
- [ ] Decorate implementation with `@register_serializer()`
- [ ] Add import in `tram/serializers/__init__.py`
- [ ] Add optional dependency in `pyproject.toml` if required
- [ ] Add tests under `tests/unit/serializers/`
- [ ] Update `README.md` if user-facing

### Environment / Config Surface
- [ ] Add new env vars to `.env.example`
- [ ] Document new env vars in `docs/deployment.md`
- [ ] Update `helm/values.yaml` if the setting is chart-managed
- [ ] Update `helm/values-template.yaml` if the setting should appear in the generic template
- [ ] Update `docker-compose.yml` if applicable

### Documentation
Update only the docs affected by the change:
- [ ] `README.md` for user-visible features or install flow
- [ ] `docs/changelog.md` under `## [Unreleased]`
- [ ] `docs/api.md` for REST contract changes
- [ ] `docs/connectors.md` for connector behavior
- [ ] `docs/transforms.md` for transform behavior
- [ ] `docs/deployment.md` for env, Docker, Helm, or runtime behavior
- [ ] `docs/architecture.md` for core execution/control-plane changes
- [ ] `docs/index.md` for landing-page level version or quick-start references
- [ ] `CLAUDE.md` / `AGENTS.md` only if repo instructions actually changed

### API / UI Changes
- [ ] Verify OpenAPI still renders correctly
- [ ] Update `README.md` API table for new public endpoints
- [ ] Update `docs/api.md` for new or changed endpoints
- [ ] Update frontend client code in `tram/ui/src/api.js` if backend contracts changed
- [ ] Update affected UI pages in `tram/ui/src/pages/`
- [ ] Run `cd tram/ui && npm run build`
- [ ] Check responsive behavior if the change is UI-visible

### Docker / Helm Changes
- [ ] Test manager image build: `docker build -t tram:test .`
- [ ] Test worker image build: `docker build -t tram-worker:test -f Dockerfile.worker .`
- [ ] Test `docker compose up` if Compose behavior changed
- [ ] Run `helm dependency update helm/` if chart dependencies or `Chart.yaml` changed
- [ ] Run `helm lint helm/`
- [ ] Run `helm install tram-test helm/ --dry-run --debug`
- [ ] Update `helm/values.yaml` image tags only when intentionally changing the deployed default

## Version Release Checklist

### 1. Version Bump
- [ ] Update `pyproject.toml` version to `X.Y.Z`
- [ ] Update `helm/Chart.yaml`:
  - [ ] `version: X.Y.Z`
  - [ ] `appVersion: "X.Y.Z"`
- [ ] Update version references in `README.md`, `docs/index.md`, `docs/deployment.md`, and any release-specific docs
- [ ] Verify `tram/__init__.py` still reads version from `importlib.metadata`

### 2. Changelog
- [ ] Move `## [Unreleased]` items in `docs/changelog.md` to `## [X.Y.Z] - YYYY-MM-DD`
- [ ] Add or update the comparison link for `X.Y.Z`
- [ ] Create a new empty `## [Unreleased]` section

### 3. Documentation Sync
- [ ] `README.md` and `docs/index.md` quick-start examples still use `latest`
- [ ] Production examples pin a concrete release tag where appropriate
- [ ] `helm/values-template.yaml` generic release `image.tag` matches `X.Y.Z`
- [ ] `helm/values.yaml` kind/dev profile tags are updated only when intentionally moving the local deployment baseline
- [ ] Explicit `manager.image.tag` / `worker.image.tag` examples or comments match `X.Y.Z` when shown
- [ ] Auth docs still match implementation (`TRAM_AUTH_USERS`, bootstrap behavior, DB password storage)
- [ ] Feature/version tables still match actual release history

### 4. Validation Before Push
- [ ] `ruff check .`
- [ ] `pytest tests/unit/ -v -o log_cli=false`
- [ ] `pytest tests/integration/ -v -o log_cli=false`
- [ ] `pytest tests/ --cov=tram --cov-fail-under=75 -o log_cli=false`
- [ ] `docker build -t tram:X.Y.Z .`
- [ ] `docker build -t tram-worker:X.Y.Z -f Dockerfile.worker .`
- [ ] Verify `curl http://localhost:8765/api/meta` returns `X.Y.Z` from a local run or container
- [ ] Run `cd tram/ui && npm run build`
- [ ] Run `helm dependency update helm/`
- [ ] Run `helm lint helm/`
- [ ] Run `helm install tram-test helm/ --dry-run --debug`

### 5. Example Pipelines
- [ ] Validate bundled examples: `tram validate pipelines/*.yaml`
- [ ] Dry-run at least 3 representative examples
- [ ] Verify template listing endpoint: `curl http://localhost:8765/api/templates`

### 6. Kind / Local Cluster Validation
Recommended for releases that touch scheduling, placement, stats, K8s behavior, or ingress.
- [ ] Deploy with `./scripts/deploy-kind-tram-dev.sh --tag <tag>`
- [ ] Verify `/api/meta`, `/api/ready`, `/api/cluster/nodes`
- [ ] Verify any changed placement, stats, or ingress behavior live
- [ ] Return the dev cluster to a clean baseline after testing

### 7. CI / Release Workflow Alignment
- [ ] Verify `.github/workflows/ci.yml` still matches the local validation bar
- [ ] Verify `.github/workflows/release.yml` still matches the intended release process
- [ ] Confirm `release.yml` reads the version from `pyproject.toml`
- [ ] Confirm `release.yml` runs `helm dependency update helm/` before packaging
- [ ] Confirm `release.yml` publishes both versioned and `latest` tags for manager and worker images
- [ ] Confirm `GHCR_TOKEN` is still the required registry secret

### 8. Commit and Push
- [ ] Stage version bump and release-doc files
- [ ] Commit with `chore: bump version to X.Y.Z`
- [ ] Push `main` to trigger `.github/workflows/release.yml`

### 9. Post-Push Verification
- [ ] Monitor the `CI` workflow on `main`
- [ ] Monitor the `Release` workflow on `main`
- [ ] Verify pushed images:
  - [ ] `ghcr.io/<owner>/trishul-ram:X.Y.Z`
  - [ ] `ghcr.io/<owner>/trishul-ram-worker:X.Y.Z`
  - [ ] `ghcr.io/<owner>/trishul-ram:latest`
  - [ ] `ghcr.io/<owner>/trishul-ram-worker:latest`
- [ ] Verify pushed Helm chart: `oci://ghcr.io/<owner>/charts/trishul-ram:X.Y.Z`

### 10. Tag / GitHub Release
Git tagging is optional from a publishing perspective because the current release
workflow is push-to-main based. Do it when you want Git history and GitHub
Releases to track the shipped version explicitly.
- [ ] Create and push annotated tag: `git tag -a vX.Y.Z -m "Release version X.Y.Z" && git push origin vX.Y.Z`
- [ ] Create GitHub release from tag `vX.Y.Z`
- [ ] Copy the `docs/changelog.md` entry into release notes

## Hotfix Checklist

- [ ] Keep scope minimal
- [ ] Repeat the full validation steps above
- [ ] Update `docs/changelog.md`
- [ ] Push `main` and verify the same workflows
- [ ] Tag only if you want a GitHub release entry for the hotfix

## Rollback Checklist

- [ ] Identify the last known good release
- [ ] Roll back Docker image tags and Helm chart version to that release
- [ ] Update `docs/changelog.md` with rollback context if the bad release was published
- [ ] Communicate the rollback and open a follow-up issue

## Notes

- CI and release workflows currently trigger on pushes to `main`; tag creation is not required to publish artifacts.
- `release.yml` already publishes `latest`; do not use a separate Git tag named `latest`.
- A successful `./scripts/deploy-kind-tram-dev.sh --tag <tag>` run for the release candidate can stand in for separate local Docker build checks and the Helm install-path sanity check, because it already builds images, loads them into kind, and performs a live `helm upgrade --install`; still record `helm dependency update helm/` and `helm lint helm/` explicitly.
- Keep this file procedural. Release history belongs in `docs/changelog.md`.

## v1.3.3 Status — 2026-05-01

This section records the current known status of the `1.3.3` release pass.

### Completed
- [x] `pyproject.toml` version bumped to `1.3.3`
- [x] `helm/Chart.yaml` `version` and `appVersion` bumped to `1.3.3`
- [x] `helm/values-template.yaml` generic release tag bumped to `1.3.3`
- [x] Release-facing docs updated for `1.3.3` (`docs/changelog.md`, `docs/index.md`, `docs/deployment.md`, `docs/roadmap.md`, `docs/api.md`)
- [x] CI/release workflow alignment rechecked:
  - [x] `.github/workflows/ci.yml` uses Python `3.13` and still runs `ruff check .` plus `pytest tests/unit/ tests/integration/`
  - [x] `.github/workflows/release.yml` uses Python `3.13`, reads the version from `pyproject.toml`, runs `helm dependency update helm/`, publishes versioned and `latest` image tags, and still requires `GHCR_TOKEN`
- [x] Full local source-level validation completed in the current environment:
  - [x] `ruff check .`
  - [x] `pytest tests/unit/ -v -o log_cli=false`
  - [x] `pytest tests/integration/ -v -o log_cli=false`
  - [x] `pytest tests/ --cov=tram --cov-fail-under=75 -o log_cli=false`
  - [x] `cd tram/ui && npm run build`
- [x] Helm dependency and chart lint validation completed:
  - [x] `helm dependency update helm/`
  - [x] `helm lint helm/`
- [x] Example-pipeline validation completed:
  - [x] `tram validate pipelines/*.yaml` passed for all `32` bundled YAMLs
  - [x] Representative dry-runs passed for `pipelines/minimal.yaml`, `pipelines/multi-format-fanout.yaml`, and `pipelines/webhook-alarm-fanout.yaml`
  - [x] `/api/templates` endpoint behavior is covered by the passing unit API suite (`tests/unit/test_api_misc_routers.py`)
- [x] Existing live kind deploy validation via `./scripts/deploy-kind-tram-dev.sh` is accepted as the release proof for local Docker image build + Helm upgrade/install behavior

### Intentionally Retained
- [x] `helm/values.yaml` remains the active kind/dev deployment profile with local image tags; the generic release baseline is `helm/values-template.yaml`
- [x] The current local `.venv` remains on Python `3.12`, so installed `tram` package metadata is not the source of truth for `1.3.3`; source files, tests, and docs are updated, but runtime version reporting in this venv is intentionally not used as release evidence

### Still Open
- [ ] Local `/api/meta` verification from a bumped runtime instance remains blocked in the current `.venv` because installed package metadata still reports the previously installed version
- [ ] Release commit/push to `main` is not recorded here yet
- [ ] Post-push artifact verification is not recorded here yet
- [ ] Optional Git tag / GitHub Release status is not recorded here yet

## v1.3.1 Status — 2026-04-20

This section records the current known status of the `1.3.1` release pass.

### Completed
- [x] `pyproject.toml` version bumped to `1.3.1`
- [x] `helm/Chart.yaml` `version` and `appVersion` bumped to `1.3.1`
- [x] Full Python validation completed earlier in the release pass:
  - [x] `ruff check .`
  - [x] `pytest tests/unit/ -q`
  - [x] `pytest tests/integration/ -q`
  - [x] `pytest tests/ --cov=tram --cov-fail-under=75`
- [x] Helm validation completed in the release pass:
  - [x] `helm lint helm/`
  - [x] chart dependency handling verified in `.github/workflows/release.yml`
- [x] Live kind deployment validation completed
- [x] Real manager/worker ingress and placement tests completed on kind
- [x] Scale-down / stale-slot / degraded placement behavior validated on kind
- [x] `workers.list` dedicated service endpoint reconciliation bug fixed and revalidated live
- [x] Release workflow checked:
  - [x] reads version from `pyproject.toml`
  - [x] runs `helm dependency update helm/`
  - [x] publishes versioned and `latest` manager/worker images
  - [x] uses `GHCR_TOKEN`
- [x] Helm values split is intentional:
  - [x] `helm/values.yaml` tracks the active kind/dev deployment profile
  - [x] `helm/values-template.yaml` is the generic release baseline

### Still Open
- [ ] Post-push artifact verification is not recorded here yet:
  - [ ] GHCR manager image verification
  - [ ] GHCR worker image verification
  - [ ] OCI Helm chart verification
- [ ] Optional Git tag / GitHub Release status is not recorded here yet
