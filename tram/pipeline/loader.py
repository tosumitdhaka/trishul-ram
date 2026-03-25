"""Pipeline loader — parses YAML with ${VAR:-default} substitution."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from tram.core.exceptions import ConfigError
from tram.models.pipeline import PipelineConfig, PipelineFile

logger = logging.getLogger(__name__)

# Matches ${VAR} or ${VAR:-default}
_VAR_PATTERN = re.compile(r"\$\{(\w+)(?::-(.*?))?\}")


def _substitute_env_vars(text: str) -> str:
    """Replace ${VAR:-default} placeholders with environment variable values."""
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)  # None if no :- was present
        value = os.environ.get(var_name)
        if value is None:
            if default is not None:
                return default
            raise ConfigError(
                f"Required environment variable '{var_name}' is not set and has no default"
            )
        return value

    return _VAR_PATTERN.sub(replacer, text)


def load_pipeline(path: str | Path) -> tuple[PipelineConfig, str]:
    """Load and validate a pipeline YAML file.

    Performs environment variable substitution before parsing.

    Args:
        path: Path to the pipeline YAML file.

    Returns:
        Validated ``PipelineConfig`` instance.

    Raises:
        ConfigError: If the file is missing, has invalid YAML, or fails Pydantic validation.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Pipeline file not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read pipeline file {path}: {exc}") from exc

    try:
        substituted = _substitute_env_vars(raw_text)
    except ConfigError:
        raise

    try:
        data = yaml.safe_load(substituted)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error in {path}: {exc}") from exc

    if data is None:
        raise ConfigError(f"Pipeline file is empty: {path}")

    # Support both wrapped (pipeline: {...}) and flat formats
    if "pipeline" not in data:
        # Treat the whole document as the pipeline config
        pipeline_data = data
    else:
        pipeline_data = data["pipeline"]

    try:
        config = PipelineConfig.model_validate(pipeline_data)
    except ValidationError as exc:
        raise ConfigError(f"Pipeline validation error in {path}:\n{exc}") from exc

    logger.info("Loaded pipeline", extra={"pipeline": config.name, "filepath": str(path)})
    return config, raw_text


def load_pipeline_from_yaml(yaml_text: str) -> PipelineConfig:
    """Load a pipeline from a YAML string (used by REST API)."""
    try:
        substituted = _substitute_env_vars(yaml_text)
        data = yaml.safe_load(substituted)
    except ConfigError:
        raise
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error: {exc}") from exc

    if data is None:
        raise ConfigError("Pipeline YAML is empty")

    pipeline_data = data.get("pipeline", data)

    try:
        return PipelineConfig.model_validate(pipeline_data)
    except ValidationError as exc:
        raise ConfigError(f"Pipeline validation error:\n{exc}") from exc


def scan_pipeline_dir(pipeline_dir: str | Path) -> list[tuple[PipelineConfig, str]]:
    """Scan a directory for pipeline YAML files and load them all.

    Returns a list of (PipelineConfig, raw_yaml_text) tuples.
    """
    pipeline_dir = Path(pipeline_dir)
    if not pipeline_dir.exists():
        logger.warning("Pipeline directory does not exist: %s", pipeline_dir)
        return []

    results = []
    for yaml_file in sorted(pipeline_dir.glob("*.yaml")) + sorted(pipeline_dir.glob("*.yml")):
        try:
            config, yaml_text = load_pipeline(yaml_file)
            results.append((config, yaml_text))
        except ConfigError as exc:
            logger.error("Failed to load pipeline %s: %s", yaml_file, exc)

    logger.info(
        "Scanned pipeline directory",
        extra={"dir": str(pipeline_dir), "loaded": len(results)},
    )
    return results
