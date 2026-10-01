#!/usr/bin/env python3
"""Validate every perf-bench pipeline template against the Pydantic schema.

Loads each ``templates/*.yaml`` through the same in-process path the unit
tests use (``tram.pipeline.loader.load_pipeline`` -> ``PipelineConfig.model_validate``
after ${VAR:-default} env substitution) and reports pass/fail per file.
Exits non-zero if any template fails to validate.

Run from the repo root (or anywhere, since tram is installed in the venv):

    .venv/bin/python scripts/perf/templates/validate_templates.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The harness venv's tram editable-install points at a stale checkout; make
# sure the repo's own tram package is importable regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tram.pipeline.loader import load_pipeline  # noqa: E402

TEMPLATE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate all perf-bench pipeline templates against the Pydantic schema."
    )
    parser.add_argument(
        "--dir",
        default=str(TEMPLATE_DIR),
        help="Template directory to scan (default: this file's directory)",
    )
    args = parser.parse_args()

    template_dir = Path(args.dir)
    files = sorted(template_dir.glob("*.yaml"))
    if not files:
        raise SystemExit(f"validate_templates: no *.yaml files found in {template_dir}")

    failures = 0
    for path in files:
        try:
            config, _raw = load_pipeline(path)
        except Exception as exc:  # ConfigError / yaml / pydantic — report all
            failures += 1
            print(f"FAIL  {path.name}: {exc}")
            continue
        print(f"pass  {path.name}  (pipeline={config.name}, source={config.source.type})")

    print(f"\nvalidate_templates: {len(files) - failures}/{len(files)} templates passed")
    if failures:
        raise SystemExit(f"validate_templates: {failures} template(s) FAILED validation")
    print("validate_templates: ALL TEMPLATES VALID")


if __name__ == "__main__":
    main()