## Summary

<!-- What and why — one paragraph. Reference the issue(s) or roadmap item(s) this addresses. -->

## Impacted modules

<!-- e.g. tram/pipeline/, tram/api/, tram/ui/, helm/, Dockerfile* — be specific -->

## Test evidence

<!-- Commands run and their results, e.g.:
     pytest tests/unit/test_api_pipelines.py -q
     cd tram/ui && npm run build
     scripts/deploy-kind-tram-dev.sh (runtime verification) -->

## Documentation

- [ ] `docs/changelog.md` `[Unreleased]` entry added (required for user-facing changes)
- [ ] API contract changes → `docs/api.md` updated
- [ ] New env vars → `.env.example` + `docs/deployment.md` + `helm/values.yaml`
- [ ] Helm / Docker changes explicitly called out below (upgrade steps if any)

## UI changes

<!-- Screenshots for tram/ui/ changes; delete this section otherwise -->

## Release gate

<!-- For release PRs only (one branch + one PR per version, per docs/release-gate.md). -->

- [ ] Review sign-off recorded: self-review checklist + independent review of the full diff, summarized in this PR
- [ ] Versions bumped and aligned: `pyproject.toml`, `helm/Chart.yaml` (version + appVersion), `tram/ui/package.json`
- [ ] `docs/changelog.md` `[Unreleased]` renamed to the release version
- [ ] `scripts/release-gate.sh` full run green (paste the summary)
