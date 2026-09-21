# Release Gate

The release gate is the mandatory quality bar for every TRAM version release. **No version ships without passing it.** It has three enforcement layers that must all agree: a local script, PR-level CI, and a tag-triggered release workflow.

---

## The gate

Run from the repo root:

```bash
scripts/release-gate.sh          # full gate
scripts/release-gate.sh --fast   # skip pytest + UI build (quick iteration only)
```

The script exits non-zero if any check fails. `--fast` is for iteration only — **the full gate is what must pass before tagging.**

| # | Check | What it enforces |
|---|---|---|
| 1 | Clean working tree | The release is built from committed state only |
| 2 | Version consistency | `pyproject.toml` == `helm/Chart.yaml` (`version` **and** `appVersion`) == `tram/ui/package.json` |
| 3 | Version not already tagged | Never re-release an existing version |
| 4 | Changelog entry | `docs/changelog.md` contains a `## [X.Y.Z]` section for the version being released |
| 5 | `ruff check .` | Lint clean |
| 6 | Full pytest + coverage floor 75% | Unit + integration suites, `--cov-fail-under=75` |
| 7 | UI build | `tram/ui` builds with vite |
| 8 | Example pipelines validate | `tram validate` over every `pipelines/*.yaml` |
| 9 | Helm lint + template | Chart lints and renders (dependencies fetched to a temp copy) |
| 10 | Docs-sync | Every `TRAM_*` env var referenced in the Python source appears in `.env.example` |

Notes on check 10: it is a best-effort static scan — dynamically constructed names can slip through. If a variable is deliberately internal-only, document that decision next to the check rather than deleting it from `.env.example` silently.

## How the layers enforce it

1. **PR-level CI** (`.github/workflows/ci.yml`): every PR runs lint, tests + coverage, UI build, example-pipeline validation, and Helm lint. A red PR cannot satisfy the gate.
2. **Release workflow** (`.github/workflows/release.yml`): fires **only on `v*` tag pushes** — merging to main no longer publishes anything. The `gate` job verifies tag == pyproject version, verifies the changelog section, and re-runs `scripts/release-gate.sh --ci` on the tag itself. Docker images and the Helm chart publish only after the gate is green.
3. **Process rule**: this document + AGENTS.md bind both maintainers and coding agents — no tagging past a red gate.

## Release procedure

1. **Scope complete.** All roadmap items assigned to this version (see `docs/ideas/consolidated-roadmap.md`) are done, and the release PR (one branch + one PR per version, with its progress table) is ready to merge or already merged.
2. **Review sign-off** (required, recorded in the release PR body):
   - Self-review checklist: every commit's diff re-read against its stated intent.
   - Independent review of the full release diff (an adversarial agent review is the established bar — see the v1.4.0 wave reviews). Summarize findings and their resolutions in the PR.
3. **Documentation pass:**
   - `docs/changelog.md`: rename `[Unreleased]` → `[X.Y.Z] - YYYY-MM-DD` and add the new compare link at the bottom.
   - API contract changes → `docs/api.md`.
   - New env vars → `.env.example` + `docs/deployment.md` + `helm/values.yaml` (check 10 enforces `.env.example`; Helm-side consistency is a manual review item).
   - UI changes → screenshots in the PR.
4. **Version bump** in all three places (check 2 enforces): `pyproject.toml`, `helm/Chart.yaml` (`version` + `appVersion`), `tram/ui/package.json`.
5. **Run the full gate locally** — `scripts/release-gate.sh`. Every check must pass.
6. **Tag and push:**
   ```bash
   git tag vX.Y.Z          # must equal the pyproject version
   git push origin vX.Y.Z
   ```
7. **Watch the release workflow** — the CI gate re-runs on the tag, then images (standalone/manager/worker) and the Helm chart publish to GHCR.
8. **Post-release:** verify the images and chart on GHCR; create the GitHub release notes from the changelog section; update the roadmap status; close the issues the version resolves.

## Manual validation

The automated gate cannot click a UI. Before merging the release PR and tagging, run the current tree in the kind dev cluster and click through the release's riskiest interactions yourself:

```bash
scripts/release-gate.sh --deploy-kind              # full gate, then deploy
scripts/release-gate.sh --fast --deploy-kind       # skip slow checks, then deploy
```

What it does: after a green gate, builds manager+worker images from the working tree, loads them into the `tram-dev` kind cluster, and upgrades the `trishul-ram` Helm release. The manager UI is then reachable via the service NodePort, which the kind cluster maps directly to the same host port — the script discovers and prints it (currently `http://localhost:30001`). No port-forward is involved.

**Stale-SPA warning:** browsers happily serve a cached SPA from an older deployment — it shows the old version string and empty pages (its hashed asset references 404 against the new server). Hard-refresh (**Ctrl-Shift-R**) after any redeploy before judging the UI.

Smoke checklist (minimum bar for a release that touches the UI):

- Every confirm modal executes its action after Confirm (Stop, Reload, rollback, delete pipeline/MIB/schema/alert) — the v1.4.2 review caught a class of bug where the modal closed and nothing ran.
- Deep links survive refresh; browser Back/Forward work.
- Dashboard "+ New" opens a blank editor; Save never overwrites an unrelated pipeline.
- Any release-specific items named in the independent review's report.

Record the smoke result in the release PR before tagging.

## Rules

- **Never tag with a red gate.** If a check fails, fix it or defer the item — do not force past it.
- **Override policy:** a maintainer override is possible only with a written justification in the release PR stating which check was waived, why, and the tracking item for the follow-up. Overrides should be exceptional.
- **Coverage floor** is 75% (`--cov-fail-under`). Raising it is a separate decision; the gate does not ratchet it automatically.
- **Re-releasing a version is prohibited** (check 3). If a release goes bad, cut the next patch version.
