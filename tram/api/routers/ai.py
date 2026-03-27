"""AI assist endpoint — YAML generation and dry-run error explanation."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

_ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_OPENAI_DEFAULT_MODEL    = "gpt-4o-mini"

_SCHEMA_EXCERPT = """
Required top-level keys: name, schedule, source, sinks
Optional: description, serializer, serializer_in, transforms, dlq, enabled

schedule:
  type: stream | interval | cron | manual
  interval_seconds: 300        # only for interval
  cron_expr: "*/5 * * * *"    # only for cron

source:
  type: <source_type>
  # source-specific fields follow

sinks:
  - type: <sink_type>
    # sink-specific fields
    serializer_out: json        # optional per-sink override
    condition: "records_in > 0" # optional routing condition

transforms:
  - type: rename
    fields: {old_col: new_col}
  - type: filter
    condition: "status == 'active'"
  - type: add_field
    field: env
    value: prod
"""

_EXAMPLE_YAML = """
name: sftp-to-kafka
description: "Poll SFTP every 5 minutes and push CSV to Kafka"
schedule:
  type: interval
  interval_seconds: 300
source:
  type: sftp
  host: sftp.example.com
  port: 22
  username: tram
  password: secret
  remote_path: /data/pm
  file_pattern: "*.csv"
serializer: csv
transforms:
  - type: rename
    fields:
      Timestamp: timestamp
      Value: value
sinks:
  - type: kafka
    brokers:
      - kafka:9092
    topic: pm-raw
"""

_GENERATE_SYSTEM = """You are a TRAM pipeline configuration assistant.
TRAM pipelines are defined in YAML. Given a user description, output ONLY valid TRAM pipeline YAML — no prose, no markdown code fences.

Available source types: {source_types}
Available sink types:   {sink_types}
Available transforms:   {transform_types}
Available serializers:  {serializer_types}

Schema reference:
{schema_excerpt}

Example pipeline:
{example}
"""


def _call_ai(system: str, user: str, max_tokens: int) -> str:
    provider = os.getenv("TRAM_AI_PROVIDER", "anthropic")
    api_key  = os.getenv("TRAM_AI_API_KEY")
    model    = os.getenv("TRAM_AI_MODEL")
    base_url = os.getenv("TRAM_AI_BASE_URL")

    if provider == "anthropic":
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic package not installed — pip install tram[ai-anthropic]")
        model = model or _ANTHROPIC_DEFAULT_MODEL
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text.strip()

    elif provider == "openai":
        try:
            import openai
        except ImportError:
            raise RuntimeError("openai package not installed — pip install tram[ai-openai]")
        model = model or _OPENAI_DEFAULT_MODEL
        kwargs: dict = {"api_key": api_key or "none"}
        if base_url:
            kwargs["base_url"] = base_url
        client = openai.OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=model, max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )
        return resp.choices[0].message.content.strip()

    raise ValueError(f"Unknown TRAM_AI_PROVIDER: {provider!r} (must be 'anthropic' or 'openai')")


@router.get("/api/ai/status")
async def ai_status() -> dict:
    """Returns whether AI assist is configured."""
    enabled = bool(os.getenv("TRAM_AI_API_KEY"))
    return {
        "enabled": enabled,
        "provider": os.getenv("TRAM_AI_PROVIDER", "anthropic") if enabled else None,
        "model": os.getenv("TRAM_AI_MODEL") or (
            _ANTHROPIC_DEFAULT_MODEL if os.getenv("TRAM_AI_PROVIDER", "anthropic") == "anthropic"
            else _OPENAI_DEFAULT_MODEL
        ) if enabled else None,
    }


@router.post("/api/ai/suggest")
async def ai_suggest(request: Request) -> dict:
    """Generate YAML or explain a dry-run error using an LLM."""
    if not os.getenv("TRAM_AI_API_KEY"):
        raise HTTPException(status_code=503, detail="AI assist not configured (TRAM_AI_API_KEY not set)")

    body = await request.json()
    mode = body.get("mode", "generate")

    if mode == "generate":
        plugins = body.get("plugins", {})
        system = _GENERATE_SYSTEM.format(
            source_types     = ", ".join(plugins.get("sources", [])),
            sink_types       = ", ".join(plugins.get("sinks", [])),
            transform_types  = ", ".join(plugins.get("transforms", [])),
            serializer_types = ", ".join(plugins.get("serializers", [])),
            schema_excerpt   = _SCHEMA_EXCERPT,
            example          = _EXAMPLE_YAML,
        )
        try:
            yaml_text = _call_ai(system, body.get("prompt", ""), max_tokens=1024)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        # Strip accidental markdown fences
        yaml_text = yaml_text.strip()
        if yaml_text.startswith("```"):
            yaml_text = "\n".join(yaml_text.split("\n")[1:])
        if yaml_text.endswith("```"):
            yaml_text = "\n".join(yaml_text.split("\n")[:-1])
        return {"yaml": yaml_text.strip()}

    elif mode == "explain":
        user = (
            f"TRAM pipeline YAML:\n{body.get('yaml', '')}\n\n"
            f"Dry-run error: {body.get('error', '')}\n\n"
            f"Explain what is wrong and suggest a fix in 2-3 sentences."
        )
        try:
            explanation = _call_ai(
                "You are a helpful TRAM pipeline configuration assistant.",
                user, max_tokens=300,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"explanation": explanation}

    raise HTTPException(status_code=400, detail=f"Unknown mode: {mode!r}")
