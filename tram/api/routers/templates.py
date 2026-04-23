"""Pipeline templates — serve bundled YAML examples from the pipelines/ directory."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)
router = APIRouter()

_cache: list[dict] | None = None
_cache_at: float = 0.0
_CACHE_TTL = 60.0  # seconds


def _short_desc(text: str) -> str:
    """Return first sentence / first line of a description string."""
    if not text:
        return ""
    text = text.strip()
    # Take up to first blank line
    first_block = text.split("\n\n")[0].replace("\n", " ").strip()
    # Cap at 120 chars
    return first_block[:120] + ("…" if len(first_block) > 120 else "")


def _load_templates(pipeline_dir: str) -> list[dict]:
    templates = []
    for path in sorted(Path(pipeline_dir).glob("*.yaml")):
        try:
            import yaml as _yaml
            text = path.read_text()
            doc = _yaml.safe_load(text)
            if isinstance(doc, dict) and "pipeline" in doc and isinstance(doc["pipeline"], dict):
                p = doc["pipeline"]
            elif isinstance(doc, dict):
                p = doc
            else:
                p = {}

            name = p.get("name") or path.stem
            description = _short_desc(p.get("description", ""))
            schedule_type = (p.get("schedule") or {}).get("type", "interval")
            source_type = (p.get("source") or {}).get("type", "")
            # Support both sinks: (list) and sink: (singular dict)
            raw_sinks = p.get("sinks") or ([p["sink"]] if p.get("sink") else [])
            sink_types = list(dict.fromkeys(
                (s.get("type", "") if isinstance(s, dict) else "") for s in raw_sinks if s
            ))

            # Tags: source + sinks + schedule
            tags = list(dict.fromkeys(
                [t for t in [source_type] + sink_types + [schedule_type] if t]
            ))

            templates.append({
                "id": path.stem,
                "name": name,
                "description": description,
                "tags": tags,
                "source_type": source_type,
                "sink_types": sink_types,
                "schedule_type": schedule_type,
                "yaml": text,
            })
        except Exception as exc:
            logger.warning("Could not parse template %s: %s", path.name, exc)

    return templates


@router.get("/api/templates")
async def list_templates(request: Request) -> list[dict]:
    """Return all pipeline templates from the bundled templates directory."""
    global _cache, _cache_at
    now = time.monotonic()
    if _cache is None or (now - _cache_at) > _CACHE_TTL:
        templates_dir = request.app.state.config.templates_dir
        _cache = _load_templates(templates_dir)
        _cache_at = now
    return _cache
