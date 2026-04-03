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
- [ ] `CHANGELOG.md` — add entry under `## [Unreleased]`
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
- [ ] Frontend API client updated (`tram-ui/src/api.js`)
- [ ] Auth/rate-limit middleware applied correctly

### UI Changes
- [ ] Test in both light and dark mode
- [ ] Test responsive layout (mobile/tablet/desktop)
- [ ] Update relevant page in `tram-ui/src/pages/`
- [ ] Run `cd tram-ui && npm run build` to verify no build errors
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
- [ ] `README.md` Quick Start examples use correct version
- [ ] Helm install examples use `--set image.tag=X.Y.Z`
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
  git add pyproject.toml README.md CHANGELOG.md helm/Chart.yaml
  ```
- [ ] Commit with message: `chore: bump version to X.Y.Z`
- [ ] **Do NOT push yet** — verify release workflow first

#### 8. Release Workflow Verification
- [ ] Check `.github/workflows/release.yml` is configured
- [ ] Verify Docker registry credentials are set (GitHub secrets)
- [ ] Verify Helm chart registry is configured

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
