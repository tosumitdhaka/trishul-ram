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

### 0. Release Gate (mandatory)
- [ ] Run `scripts/release-gate.sh` and require every check green — 11 checks: clean working tree, version consistency (pyproject / chart / UI), version not already tagged, changelog entry, `ruff check .`, pytest + 75% coverage floor, UI build, UI browser smoke (Playwright), example pipelines validate, helm lint + template, docs-sync
- [ ] The UI browser smoke check needs node >= 20 (or `TRAM_BROWSER_NODE=/path/to/node20`); it is never silently skipped
- [ ] `scripts/release-gate.sh --fast` skips pytest + UI build + browser smoke — iteration only; the **full gate** must pass before tagging

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
- [ ] Registry auth uses the built-in `GITHUB_TOKEN` (`packages: write`) — no PAT secret to rotate

### 8. Commit, Merge, and Tag
- [ ] Stage version bump and release-doc files
- [ ] Commit with `chore: bump version to X.Y.Z`
- [ ] Open the release PR (one branch + one PR per version, progress table in the PR body) and merge it to `main`
- [ ] Merging to `main` publishes **nothing** — `release.yml` fires only on a `v*` tag push
- [ ] Tag and push the tag (must equal the `pyproject.toml` version):
      `git tag vX.Y.Z && git push origin vX.Y.Z` — this triggers `.github/workflows/release.yml`

### 9. Post-Push Verification
- [ ] Monitor the `Release` workflow on the tag (its `gate` job re-runs `scripts/release-gate.sh --ci` on the tag before publishing anything)
- [ ] Verify pushed images:
  - [ ] `ghcr.io/<owner>/trishul-ram:X.Y.Z`
  - [ ] `ghcr.io/<owner>/trishul-ram-worker:X.Y.Z`
  - [ ] `ghcr.io/<owner>/trishul-ram:latest`
  - [ ] `ghcr.io/<owner>/trishul-ram-worker:latest`
- [ ] Verify pushed Helm chart: `oci://ghcr.io/<owner>/charts/trishul-ram:X.Y.Z`

### 10. Tag / GitHub Release
Tagging is the **publish trigger**, not an optional extra: `release.yml` fires only on `v*` tag
pushes and re-runs the gate on the tag before publishing images and the Helm chart. Push-to-main
publishes nothing.
- [ ] Create and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
- [ ] Create GitHub release from tag `vX.Y.Z`
- [ ] Copy the `docs/changelog.md` entry into release notes

## Hotfix Checklist

- [ ] Keep scope minimal
- [ ] Repeat the full validation steps above (including `scripts/release-gate.sh`, full gate)
- [ ] Update `docs/changelog.md`
- [ ] Merge to `main` — then tag `vX.Y.Z` and push the tag; the tag triggers the release workflow (merging to `main` alone publishes nothing)

## Rollback Checklist

- [ ] Identify the last known good release
- [ ] Roll back Docker image tags and Helm chart version to that release
- [ ] Update `docs/changelog.md` with rollback context if the bad release was published
- [ ] Communicate the rollback and open a follow-up issue

## Notes

- The release workflow (`release.yml`) fires **only on `v*` tag pushes** — merging to `main` never publishes artifacts. `ci.yml` runs lint/tests/UI build on every PR and push.
- `release.yml` already publishes `latest`; do not use a separate Git tag named `latest`.
- A successful `./scripts/deploy-kind-tram-dev.sh --tag <tag>` run for the release candidate can stand in for separate local Docker build checks and the Helm install-path sanity check, because it already builds images, loads them into kind, and performs a live `helm upgrade --install`; still record `helm dependency update helm/` and `helm lint helm/` explicitly.
- Keep this file procedural. Release history belongs in `docs/changelog.md`.
