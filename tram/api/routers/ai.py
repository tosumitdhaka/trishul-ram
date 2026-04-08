"""AI assist endpoint — YAML generation and dry-run error explanation."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request

from tram.api.routers.ai_docs import build_ai_context

router = APIRouter()

_ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_OPENAI_DEFAULT_MODEL    = "gpt-4o-mini"
_BEDROCK_DEFAULT_MODEL   = "us.anthropic.claude-sonnet-4-6"

_AI_KEYS = ("ai.provider", "ai.api_key", "ai.model", "ai.base_url")


def _get_ai_cfg(db) -> dict:
    """Resolve AI config: DB values take precedence over env vars."""
    def _db(key: str) -> str:
        return (db.get_setting(key) or "") if db else ""

    provider = _db("ai.provider") or os.getenv("TRAM_AI_PROVIDER", "anthropic")
    api_key  = _db("ai.api_key")  or os.getenv("TRAM_AI_API_KEY", "")
    model    = _db("ai.model")    or os.getenv("TRAM_AI_MODEL", "")
    base_url = _db("ai.base_url") or os.getenv("TRAM_AI_BASE_URL", "")
    return {"provider": provider, "api_key": api_key, "model": model, "base_url": base_url}

_PIPELINE_STRUCTURE = """
Pipeline top-level structure (YAML):
  name: <string>                     # required
  description: <string>              # optional
  schedule:                          # required
    type: interval | cron | manual | stream
    interval_seconds: 300            # only for interval
    cron_expr: "*/5 * * * *"        # only for cron
  source:                            # required
    type: <source_type>
    <source fields — see SOURCES below>
  serializer_in:                     # REQUIRED — object with type field
    type: <serializer_type>
    <serializer fields — see SERIALIZERS below>
  transforms:                        # optional
    - type: <transform_type>
      <transform fields>
  sinks:                             # required list
    - type: <sink_type>
      <sink fields — see SINKS below>
      condition: "field > 0"         # optional routing filter
      serializer_out:                # optional per-sink override
        type: <serializer_type>
"""

_GENERATE_SYSTEM = """You are a TRAM pipeline configuration assistant.
TRAM pipelines are defined in YAML. Given a user description, output ONLY valid TRAM pipeline YAML — no prose, no markdown code fences.

{pipeline_structure}

{connector_schema}
"""


def _call_ai(system: str, user: str, max_tokens: int, cfg: dict) -> str:
    provider = cfg["provider"]
    api_key  = cfg["api_key"]
    model    = cfg["model"]
    base_url = cfg["base_url"]

    if provider == "anthropic":
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic package not installed — pip install tram[ai-anthropic]")
        model = model or _ANTHROPIC_DEFAULT_MODEL
        client_kwargs: dict = {"api_key": api_key or None}
        if base_url:
            # Anthropic SDK appends /v1/messages itself — strip trailing /v1 to avoid duplication
            client_kwargs["base_url"] = base_url.rstrip("/").removesuffix("/v1")
        client = anthropic.Anthropic(**client_kwargs)
        try:
            msg = client.messages.create(
                model=model, max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AuthenticationError:
            raise RuntimeError("Invalid Anthropic API key — update it in Settings → AI Assist")
        except anthropic.APIConnectionError:
            raise RuntimeError("Could not reach Anthropic API — check network connectivity")
        except anthropic.RateLimitError:
            raise RuntimeError("Anthropic rate limit exceeded — try again shortly")
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"Anthropic API error: {exc.status_code} {exc.message}")
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
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
            )
        except openai.AuthenticationError:
            raise RuntimeError("Invalid OpenAI API key — update it in Settings → AI Assist")
        except openai.APIConnectionError:
            raise RuntimeError("Could not reach OpenAI API — check network connectivity or Base URL")
        except openai.RateLimitError:
            raise RuntimeError("OpenAI rate limit exceeded — try again shortly")
        except openai.APIStatusError as exc:
            raise RuntimeError(f"OpenAI API error: {exc.status_code} {exc.message}")
        return resp.choices[0].message.content.strip()

    elif provider == "bedrock":
        # AWS Bedrock-compatible proxy: POST {base_url}/model/{model}/invoke
        # Auth via Authorization: Bearer {api_key} (no AWS Sig V4 required)
        import json as _json
        import urllib.error
        import urllib.request
        model = model or _BEDROCK_DEFAULT_MODEL
        if not base_url:
            raise RuntimeError("Base URL is required for the bedrock provider")
        invoke_url = f"{base_url.rstrip('/')}/model/{model}/invoke"
        body = _json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(invoke_url, data=body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = _json.loads(resp.read())
            return result["content"][0]["text"].strip()
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode()
            if exc.code == 401:
                raise RuntimeError("Invalid Bedrock API key — update it in Settings → AI Assist")
            if exc.code == 404:
                raise RuntimeError(f"Bedrock endpoint not found — check Base URL and Model ID ({invoke_url})")
            raise RuntimeError(f"Bedrock API error: {exc.code} {err_body[:200]}")
        except Exception as exc:
            raise RuntimeError(f"Bedrock request failed: {exc}")

    raise ValueError(f"Unknown TRAM_AI_PROVIDER: {provider!r} (must be 'anthropic', 'openai', or 'bedrock')")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    return text.strip()


@router.get("/api/ai/status", tags=["ai"])
async def ai_status(request: Request) -> dict:
    """Returns whether AI assist is configured."""
    db = getattr(request.app.state, "db", None)
    cfg = _get_ai_cfg(db)
    enabled = bool(cfg["api_key"])
    _defaults = {"anthropic": _ANTHROPIC_DEFAULT_MODEL, "openai": _OPENAI_DEFAULT_MODEL, "bedrock": _BEDROCK_DEFAULT_MODEL}
    default_model = _defaults.get(cfg["provider"], _ANTHROPIC_DEFAULT_MODEL)
    return {
        "enabled": enabled,
        "provider": cfg["provider"] if enabled else None,
        "model": (cfg["model"] or default_model) if enabled else None,
    }


@router.get("/api/ai/config", tags=["ai"])
async def ai_get_config(request: Request) -> dict:
    """Return current AI configuration (API key masked)."""
    db = getattr(request.app.state, "db", None)
    cfg = _get_ai_cfg(db)
    api_key = cfg["api_key"]
    return {
        "provider": cfg["provider"],
        "api_key_set": bool(api_key),
        "api_key_hint": f"…{api_key[-4:]}" if len(api_key) >= 4 else ("set" if api_key else ""),
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "source": "db" if (db and db.get_setting("ai.api_key")) else "env",
    }


@router.post("/api/ai/config", tags=["ai"])
async def ai_save_config(request: Request) -> dict:
    """Persist AI configuration to the DB (overrides env vars). Empty string clears a key."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    body = await request.json()
    fields = {
        "ai.provider": body.get("provider", ""),
        "ai.api_key":  body.get("api_key", ""),
        "ai.model":    body.get("model", ""),
        "ai.base_url": body.get("base_url", ""),
    }
    for key, value in fields.items():
        if value:
            db.set_setting(key, value)
        else:
            db.delete_setting(key)
    return {"ok": True}


@router.post("/api/ai/test", tags=["ai"])
async def ai_test(request: Request) -> dict:
    """Send a minimal test prompt to verify the AI provider config is working."""
    db  = getattr(request.app.state, "db", None)
    cfg = _get_ai_cfg(db)
    if not cfg["api_key"]:
        raise HTTPException(status_code=503, detail="AI assist not configured — set API key in Settings → AI")
    try:
        reply = _call_ai(
            "You are a helpful assistant.",
            "Reply with exactly: OK",
            max_tokens=10, cfg=cfg,
        )
        provider = cfg["provider"]
        _defaults = {"anthropic": _ANTHROPIC_DEFAULT_MODEL, "openai": _OPENAI_DEFAULT_MODEL, "bedrock": _BEDROCK_DEFAULT_MODEL}
        model = cfg["model"] or _defaults.get(provider, _ANTHROPIC_DEFAULT_MODEL)
        return {"ok": True, "reply": reply, "provider": provider, "model": model}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/api/ai/suggest", tags=["ai"])
async def ai_suggest(request: Request) -> dict:
    """Generate YAML or explain a dry-run error using an LLM."""
    db  = getattr(request.app.state, "db", None)
    cfg = _get_ai_cfg(db)
    if not cfg["api_key"]:
        raise HTTPException(status_code=503, detail="AI assist not configured — set API key in Settings → AI")

    body = await request.json()
    mode = body.get("mode", "generate")

    prompt = body.get("prompt", "")

    if mode == "generate":
        plugins = body.get("plugins", {})
        system = _GENERATE_SYSTEM.format(
            pipeline_structure = _PIPELINE_STRUCTURE,
            connector_schema   = build_ai_context(prompt, plugins),
        )
        try:
            yaml_text = _call_ai(system, prompt, max_tokens=1024, cfg=cfg)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"yaml": _strip_fences(yaml_text)}

    elif mode == "explain":
        user = (
            f"TRAM pipeline YAML:\n{body.get('yaml', '')}\n\n"
            f"Dry-run error: {body.get('error', '')}\n\n"
            "Explain in 2-3 sentences what is wrong and how to fix it."
        )
        try:
            explanation = _call_ai(
                "You are a helpful TRAM pipeline configuration assistant. "
                "Be concise and actionable.",
                user, max_tokens=300, cfg=cfg,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"explanation": explanation}

    elif mode == "fix":
        plugins = body.get("plugins", {})
        system = _GENERATE_SYSTEM.format(
            pipeline_structure = _PIPELINE_STRUCTURE,
            connector_schema   = build_ai_context(body.get("yaml", "") + " " + body.get("error", ""), plugins),
        ) + "\nFix the provided YAML to resolve the error. Output ONLY valid TRAM pipeline YAML — no prose, no markdown fences."
        user = (
            f"TRAM pipeline YAML:\n{body.get('yaml', '')}\n\n"
            f"Error to fix: {body.get('error', '')}"
        )
        try:
            yaml_text = _call_ai(system, user, max_tokens=1024, cfg=cfg)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"yaml": _strip_fences(yaml_text)}

    elif mode == "modify":
        plugins = body.get("plugins", {})
        system = _GENERATE_SYSTEM.format(
            pipeline_structure = _PIPELINE_STRUCTURE,
            connector_schema   = build_ai_context(body.get("yaml", "") + " " + body.get("instruction", ""), plugins),
        ) + "\nModify the provided YAML per the user instruction. Output ONLY the complete modified TRAM pipeline YAML — no prose, no markdown fences."
        user = (
            f"Existing pipeline YAML:\n{body.get('yaml', '')}\n\n"
            f"Instruction: {body.get('instruction', '')}"
        )
        try:
            yaml_text = _call_ai(system, user, max_tokens=1024, cfg=cfg)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"yaml": _strip_fences(yaml_text)}

    raise HTTPException(status_code=400, detail=f"Unknown mode: {mode!r}")
