"""AI assist endpoint — YAML generation and dry-run error explanation."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from tram.api.config_schema import SCHEMA_FIELDS, SECRET_NAME_TOKENS, schema_version
from tram.api.routers.ai_docs import build_ai_context
from tram.core.config import ai_audit_enabled
from tram.core.exceptions import ConfigError
from tram.pipeline.loader import load_pipeline_from_yaml

router = APIRouter()

# A10: per-call audit log (mode, client host, provider, model, tokens,
# duration, outcome). The ai_usage DB rows are gated by TRAM_AI_AUDIT; the
# log line itself is always emitted.
logger = logging.getLogger("tram.ai")

_ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_OPENAI_DEFAULT_MODEL    = "gpt-4o-mini"
_BEDROCK_DEFAULT_MODEL   = "us.anthropic.claude-sonnet-4-6"

_AI_PROVIDERS = ("anthropic", "openai", "bedrock")

# Outbound LLM timeout, seconds — matches the Bedrock path's 60 s
# (urllib urlopen timeout at _call_ai). Bounds event-loop stalls: _call_ai
# runs in a worker thread (asyncio.to_thread) but a hung call would still
# occupy that thread, so cap the SDK default (minutes) here.
_AI_CALL_TIMEOUT = 60.0

_AI_KEYS = ("ai.provider", "ai.api_key", "ai.model", "ai.base_url")

_DEFAULT_MODELS = {
    "anthropic": _ANTHROPIC_DEFAULT_MODEL,
    "openai": _OPENAI_DEFAULT_MODEL,
    "bedrock": _BEDROCK_DEFAULT_MODEL,
}


def _resolve_model(cfg: dict) -> str:
    """Effective model for *cfg*: the configured one, else the provider default."""
    return cfg["model"] or _DEFAULT_MODELS.get(cfg["provider"], _ANTHROPIC_DEFAULT_MODEL)


@dataclass
class _AiResult:
    """Outcome of one provider call: the reply text plus the provider's stop
    reason (``None`` when the provider does not report one) and token usage
    (``None`` when the SDK does not expose it — A10 audit fields)."""

    text: str
    stop_reason: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


# ── A11: base_url scheme enforcement + allowlist ────────────────────────────

# http base_urls are only accepted for local use: loopback (127.0.0.0/8, ::1)
# plus RFC1918 private ranges (10/8, 172.16/12, 192.168/16) and IPv6
# unique-local (fc00::/7). Deliberately NOT included: link-local 169.254.0.0/16
# (cloud metadata endpoints) and CGNAT 100.64.0.0/10 — those are routable
# infrastructure, not "the operator's own host" (Ollama/LiteLLM local use).
_PRIVATE_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_loopback_or_private_host(host: str) -> bool:
    """True when *host* is a loopback/private-range IP literal, or a
    ``localhost`` name. DNS names (other than ``*.localhost``) are never
    treated as local — resolving them at validation time would make the check
    vulnerable to DNS-rebinding and network lookups."""
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE_NETS)


def _base_url_problem(base_url: str) -> str | None:
    """Return a human-readable rejection reason for *base_url*, or None when
    it is acceptable. ``https`` is always allowed; ``http`` only for loopback
    and private-range hosts (local Ollama/LiteLLM use). URLs without a
    recognized scheme are rejected too — urlsplit would mis-parse e.g.
    ``localhost:11434`` (scheme becomes the hostname)."""
    base_url = base_url.strip()
    if not base_url:
        return None
    parts = urlsplit(base_url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return (
            f"base_url must use the http or https scheme, got {base_url!r} — "
            "include the scheme, e.g. https://llm.example.com"
        )
    if scheme == "https":
        return None
    host = parts.hostname or ""
    if _is_loopback_or_private_host(host):
        return None
    return (
        f"base_url over http is only allowed for local/private hosts "
        f"(localhost or loopback/private IP ranges); got {base_url!r}"
    )


def _normalize_base_url(url: str) -> str:
    """Normalize a base URL for allowlist display/iteration: strip whitespace
    and trailing slashes, lowercase the scheme and host, drop userinfo,
    query, and fragment."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc:
        return url
    try:
        port = parts.port
    except ValueError:
        port = None
    host = parts.hostname or ""
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme.lower()}://{netloc}{parts.path.rstrip('/')}"


def _allowed_base_urls() -> list[str]:
    """Normalized entries from TRAM_AI_ALLOWED_BASE_URLS (comma-separated).
    Empty when the env var is unset — no allowlist restriction applies."""
    return [
        _normalize_base_url(entry)
        for entry in os.getenv("TRAM_AI_ALLOWED_BASE_URLS", "").split(",")
        if entry.strip()
    ]


def _origin_key(url: str) -> tuple[str, str, int | None] | None:
    """Return the (scheme, hostname, port) origin of *url*, or None when it is
    not a valid http(s) origin (no scheme/host, or a garbage port). Default
    ports are normalized away (80 for http, 443 for https) so ``https://host``
    and ``https://host:443`` compare equal."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()
    host = parts.hostname or ""
    if scheme not in ("http", "https") or not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    default_port = 443 if scheme == "https" else 80
    if port is not None and port == default_port:
        port = None
    return (scheme, host, port)


def _path_allowed(submitted_path: str, entry_path: str) -> bool:
    """Directory-boundary path match: the submitted path must equal the entry
    path or extend it one directory level deeper. ``/v1`` matches ``/v1`` and
    ``/v1/foo`` but never ``/v1anything``. Trailing slashes are insignificant
    and an empty/root entry path matches any path on the same origin."""
    submitted = submitted_path.rstrip("/")
    entry = entry_path.rstrip("/")
    if entry in ("", "/"):
        return True
    return submitted == entry or submitted.startswith(entry + "/")


def _base_url_allowed(base_url: str, allowed: list[str]) -> bool:
    """Allowlist check — origin-exact + directory-boundary path match.

    A submitted URL is allowed only when its (scheme, hostname, port) equals
    an entry's, AND its path is an exact match or a directory-boundary prefix
    of the entry path. Sibling domains (``https://llm.example.com.evil.io``)
    and partial-path suffixes (``/v1anything``) never match, so the allowlist
    cannot be widened by sharing a hostname prefix or path prefix."""
    origin = _origin_key(base_url)
    if origin is None:
        return False
    scheme, host, port = origin
    submitted_path = urlsplit(base_url.strip()).path
    for entry in allowed:
        entry_origin = _origin_key(entry)
        if entry_origin is None:
            continue
        if (scheme, host, port) != entry_origin:
            continue
        if _path_allowed(submitted_path, urlsplit(entry.strip()).path):
            return True
    return False


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


def _call_ai(system: str, user: str, max_tokens: int, cfg: dict) -> _AiResult:
    provider = cfg["provider"]
    api_key  = cfg["api_key"]
    model    = cfg["model"]
    base_url = cfg["base_url"]

    # A11 defense-in-depth: also enforced at save time in ai_save_config, but
    # env-var-configured base_urls bypass the config endpoint, so re-check
    # both the scheme and the allowlist here before any provider path attaches
    # the API key.
    if base_url:
        if problem := _base_url_problem(base_url):
            raise RuntimeError(problem)
        if allowed := _allowed_base_urls():
            if not _base_url_allowed(base_url, allowed):
                raise RuntimeError(
                    "base_url not allowed by TRAM_AI_ALLOWED_BASE_URLS: "
                    f"{base_url!r} — it must prefix-match one of {allowed}"
                )

    if provider == "anthropic":
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic package not installed — pip install tram[ai-anthropic]")
        model = model or _ANTHROPIC_DEFAULT_MODEL
        client_kwargs: dict = {"api_key": api_key or None, "timeout": _AI_CALL_TIMEOUT}
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
        return _AiResult(
            msg.content[0].text.strip(),
            getattr(msg, "stop_reason", None),
            getattr(getattr(msg, "usage", None), "input_tokens", None),
            getattr(getattr(msg, "usage", None), "output_tokens", None),
        )

    elif provider == "openai":
        try:
            import openai
        except ImportError:
            raise RuntimeError("openai package not installed — pip install tram[ai-openai]")
        model = model or _OPENAI_DEFAULT_MODEL
        kwargs: dict = {"api_key": api_key or "none", "timeout": _AI_CALL_TIMEOUT}
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
        return _AiResult(
            resp.choices[0].message.content.strip(),
            getattr(resp.choices[0], "finish_reason", None),
            getattr(getattr(resp, "usage", None), "prompt_tokens", None),
            getattr(getattr(resp, "usage", None), "completion_tokens", None),
        )

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
            usage = result.get("usage") or {}
            return _AiResult(
                result["content"][0]["text"].strip(),
                result.get("stop_reason"),
                usage.get("input_tokens"),
                usage.get("output_tokens"),
            )
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


# ── Model-output validation (A3) ───────────────────────────────────────────


def _validate_yaml(yaml_text: str) -> list[str]:
    """Validate model-produced YAML, mirroring the dry-run endpoint's
    ``yaml.safe_load`` + ``load_pipeline_from_yaml`` check. Returns a list of
    human-readable issues (empty when the YAML parses and validates)."""
    if not yaml_text.strip():
        return ["Model returned empty YAML"]
    try:
        yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]
    try:
        load_pipeline_from_yaml(yaml_text)
    except ConfigError as exc:
        return [str(exc)]
    return []


_TRUNCATION_REASONS = {"max_tokens", "length"}  # anthropic / openai (bedrock is anthropic-shaped)


def _truncation_warning(stop_reason: str | None) -> str | None:
    """Return a warning when the provider stopped because it ran out of output
    tokens (anthropic/bedrock: ``max_tokens``, openai: ``length``)."""
    if stop_reason in _TRUNCATION_REASONS:
        return (
            f"Model output may be truncated (stop_reason={stop_reason!r}); "
            "the YAML is likely incomplete — consider retrying or raising max_tokens"
        )
    return None


def _yaml_mode_result(result: _AiResult) -> dict:
    """Post-process a generate/fix/modify reply: strip fences, validate against
    the pipeline schema, and surface provider truncation (A3). The raw YAML is
    always returned; ``valid`` is false when issues were found."""
    yaml_text = _strip_fences(result.text)
    issues = _validate_yaml(yaml_text)
    if warning := _truncation_warning(result.stop_reason):
        issues.append(warning)
    return {"yaml": yaml_text, "valid": not issues, "issues": issues}


# ── Secret redaction for outbound prompts (A4) ─────────────────────────────

_MASK_VALUE = "***redacted***"
_HEADER_FIELD_NAMES = ("headers", "extra_headers")


def _is_secret_key(name: str) -> bool:
    """Same heuristic the schema cache uses to compute ``secret`` metadata
    (``SECRET_NAME_TOKENS`` in config_schema.py) — also masks keys of
    connector types outside the schema cache (e.g. plugin connectors)."""
    return any(token in name for token in SECRET_NAME_TOKENS)


def _mask_dict_values(mapping: dict) -> bool:
    """Mask every scalar string value of *mapping* in place, keeping the keys
    (they are structural; the values are the secret-bearing part — e.g. header
    dicts whose values carry Authorization tokens). ``${VAR}`` env references
    are left intact. Returns True when any value changed."""
    changed = False
    for key, value in list(mapping.items()):
        if isinstance(value, str) and "${" not in value:
            mapping[key] = _MASK_VALUE
            changed = True
    return changed


def _mask_block(block: dict, category: str) -> bool:
    """Mask secret field values in one connector block (in place). ``${VAR}``
    env references are left intact — the loader substitutes them at runtime,
    so they are env refs, not secrets. Dict-valued ``headers``/``extra_headers``
    fields have ALL their values masked (keys kept). Returns True when any
    value changed."""
    type_name = block.get("type")
    secret_names: set[str] = set()
    if isinstance(type_name, str):
        secret_names = {
            field["name"]
            for field in SCHEMA_FIELDS[category].get(type_name, [])
            if field.get("secret")
        }
    changed = False
    for key, value in list(block.items()):
        if key in _HEADER_FIELD_NAMES and isinstance(value, dict):
            changed |= _mask_dict_values(value)
            continue
        if not isinstance(value, str) or "${" in value:
            continue
        if key in secret_names or _is_secret_key(key):
            block[key] = _MASK_VALUE
            changed = True
    return changed


def _mask_alert_webhooks(alerts: list) -> bool:
    """Mask ``webhook_url`` on every alert rule (in place) — webhook URLs embed
    credentials in their userinfo/query, so the whole value is the secret.
    ``${VAR}`` env references are left intact. Returns True when any changed."""
    changed = False
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        webhook = alert.get("webhook_url")
        if isinstance(webhook, str) and "${" not in webhook:
            alert["webhook_url"] = _MASK_VALUE
            changed = True
    return changed


def _redact_yaml(yaml_text: str) -> str:
    """Return a copy of the pipeline YAML with secret field values masked, for
    use in outbound AI prompts. The operator's pipeline on disk is never
    touched.

    Fails closed: when the YAML cannot be parsed, or the top-level document is
    a sequence (a bare list — which can still carry secret-bearing mappings),
    a ValueError is raised so callers return a 400 "fix your YAML syntax
    first" instead of sending possibly secret-bearing text verbatim to the
    provider. Scalar documents (e.g. a single string or number) have no
    structured secrets and pass through unchanged, as does empty input.

    Known limitation: masking covers scalar secret fields (schema ``secret``
    metadata + the password/token/secret/api_key name heuristic), every value
    of ``headers``/``extra_headers`` dicts, and ``alerts[].webhook_url``.
    Arbitrary dict-valued fields (e.g. ``metadata``) and ``${VAR}`` env
    references are intentionally left untouched — env refs are substituted at
    load time and are not secrets in the file.
    """
    if not yaml_text or not yaml_text.strip():
        return yaml_text
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(
            "Pipeline YAML does not parse — fix the YAML syntax first before "
            f"using AI explain/fix/modify (yaml error: {exc})"
        ) from exc
    if not isinstance(data, dict):
        if isinstance(data, list):
            raise ValueError(
                "Pipeline YAML must be a mapping at the top level — fix the "
                "YAML syntax first before using AI explain/fix/modify"
            )
        return yaml_text  # scalar documents carry no structured secrets

    wrapped = isinstance(data.get("pipeline"), dict)
    root = data["pipeline"] if wrapped else data
    if not isinstance(root, dict):
        raise ValueError(
            "Pipeline YAML must be a mapping at the top level — fix the YAML "
            "syntax first before using AI explain/fix/modify"
        )

    def mask(category: str, block) -> bool:
        return _mask_block(block, category) if isinstance(block, dict) else False

    changed = False
    changed |= mask("source", root.get("source"))
    changed |= mask("serializer", root.get("serializer_in"))
    changed |= mask("serializer", root.get("serializer_out"))
    changed |= mask("sink", root.get("sink"))      # backward-compat singular sink
    changed |= mask("sink", root.get("dlq"))
    for sink in root.get("sinks") or []:
        if not isinstance(sink, dict):
            continue
        changed |= mask("sink", sink)
        changed |= mask("serializer", sink.get("serializer_out"))
        for transform in sink.get("transforms") or []:
            changed |= mask("transform", transform)
    for transform in root.get("transforms") or []:
        changed |= mask("transform", transform)
    changed |= _mask_alert_webhooks(root.get("alerts") or [])

    if not changed:
        return yaml_text
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)


# ── A10: per-call audit log + optional ai_usage persistence ────────────────


def _audit_ai_call(
    request: Request,
    mode: str,
    cfg: dict,
    result: _AiResult | None,
    duration_s: float,
    error: BaseException | None = None,
) -> None:
    """Emit one audit line per AI call: who (client host), what (mode,
    provider, model), cost (tokens when the SDK reported them), duration,
    and outcome. The log line is always emitted; the ai_usage DB row is
    gated by the TRAM_AI_AUDIT feature flag (default on)."""
    client = request.client.host if request.client else ""
    provider = cfg["provider"]
    model = _resolve_model(cfg)
    tokens_in = result.tokens_in if result else None
    tokens_out = result.tokens_out if result else None
    ok = error is None
    extra = {
        "mode": mode,
        "client": client,
        "provider": provider,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "duration_s": round(duration_s, 3),
        "ok": ok,
    }
    if ok:
        logger.info("AI call completed", extra=extra)
    else:
        logger.warning("AI call failed", extra=extra, exc_info=error)
    db = getattr(request.app.state, "db", None)
    if db is not None and ai_audit_enabled():
        try:
            db.append_ai_usage(
                ts=datetime.now(UTC).isoformat(),
                mode=mode, client=client, provider=provider, model=model,
                tokens_in=tokens_in, tokens_out=tokens_out, ok=ok,
                schema_version=schema_version(),
            )
        except Exception:
            # Audit persistence must never break the AI call itself.
            logger.exception("Failed to persist AI usage row")


async def _run_ai_call(request: Request, mode: str, system: str, user: str,
                       max_tokens: int, cfg: dict) -> _AiResult:
    """Execute one AI call off the event loop and audit it (A10). Raises the
    underlying exception on failure — callers convert it to HTTPException."""
    started = time.monotonic()
    try:
        result = await asyncio.to_thread(_call_ai, system, user, max_tokens, cfg)
    except Exception as exc:
        _audit_ai_call(request, mode, cfg, None, time.monotonic() - started, error=exc)
        raise
    _audit_ai_call(request, mode, cfg, result, time.monotonic() - started)
    return result


@router.get("/api/ai/status", tags=["ai"])
async def ai_status(request: Request) -> dict:
    """Returns whether AI assist is configured."""
    db = getattr(request.app.state, "db", None)
    cfg = _get_ai_cfg(db)
    enabled = bool(cfg["api_key"])
    return {
        "enabled": enabled,
        "provider": cfg["provider"] if enabled else None,
        "model": _resolve_model(cfg) if enabled else None,
        "schema_version": schema_version(),
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


class _AiConfigBody(BaseModel):
    """Validated /api/ai/config request body. ``str | None`` fields reject
    non-string JSON values (booleans, numbers) instead of ``str()`` coercion;
    only an explicit JSON ``null`` clears a stored setting."""

    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None


@router.post("/api/ai/config", tags=["ai"])
async def ai_save_config(request: Request) -> dict:
    """Persist AI configuration to the DB (overrides env vars).

    Three-state semantics per field (absent/blank = keep, value = set,
    explicit null = clear):

    - absent OR blank (``""``): no change — a blank `api_key` (which older
      UIs sent whenever the password field was left empty) never deletes a
      DB-stored key, and untouched fields are never overwritten.
    - a non-empty value: validated (``provider`` and ``base_url``) and stored.
    - explicit JSON ``null``: clears the setting (delete_setting), reverting
      to the env-var / default. Use this to deliberately clear a stored key,
      provider, model, or base URL without editing the DB by hand.

    The whole body is validated before anything is persisted, so a rejected
    field leaves earlier fields untouched (no partial saves).
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    body = await request.json()
    try:
        data = _AiConfigBody.model_validate(body)
    except ValidationError as exc:
        fields = ", ".join(
            str(err["loc"][0]) if err["loc"] else "body"
            for err in exc.errors()
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid AI config: {fields} must be strings or null",
        ) from exc

    # Validate everything first — no DB writes yet, so a 400 mid-way can never
    # leave earlier fields saved.
    actions: list[tuple[str, str | None]] = []  # (setting, value); None = clear
    for field, setting in (
        ("provider", "ai.provider"),
        ("api_key", "ai.api_key"),
        ("model", "ai.model"),
        ("base_url", "ai.base_url"),
    ):
        if field not in data.model_fields_set:
            continue                      # absent → keep (no change)
        value = getattr(data, field)
        if value is None:
            actions.append((setting, None))  # explicit null → clear
            continue
        value = value.strip()
        if not value:
            continue                      # blank → keep (no change)
        if field == "provider":
            if value not in _AI_PROVIDERS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown AI provider: {value!r} — must be one of {', '.join(_AI_PROVIDERS)}",
                )
        if field == "base_url":
            # A11: reject bad schemes/allowlist violations at save time so the
            # operator hears about them here, not on the first AI call.
            if problem := _base_url_problem(value):
                raise HTTPException(status_code=400, detail=problem)
            if allowed := _allowed_base_urls():
                if not _base_url_allowed(value, allowed):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "base_url not allowed by TRAM_AI_ALLOWED_BASE_URLS: "
                            f"{value!r} — it must prefix-match one of {allowed}"
                        ),
                    )
        actions.append((setting, value))

    # Persist the validated body in one pass.
    for setting, value in actions:
        if value is None:
            db.delete_setting(setting)
        else:
            db.set_setting(setting, value)
    return {"ok": True}


@router.post("/api/ai/test", tags=["ai"])
async def ai_test(request: Request) -> dict:
    """Send a minimal test prompt to verify the AI provider config is working."""
    db  = getattr(request.app.state, "db", None)
    cfg = _get_ai_cfg(db)
    if not cfg["api_key"]:
        raise HTTPException(status_code=503, detail="AI assist not configured — set API key in Settings → AI")
    try:
        # Run the blocking SDK/urllib call in a worker thread so the event
        # loop stays responsive for all other API traffic (A1). The audit
        # wrapper (A10) also records the call with mode="test".
        reply = await _run_ai_call(
            request, "test",
            "You are a helpful assistant.",
            "Reply with exactly: OK",
            max_tokens=10, cfg=cfg,
        )
        return {"ok": True, "reply": reply.text, "provider": cfg["provider"], "model": _resolve_model(cfg)}
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

    # Redaction fails closed: unparseable/malformed YAML is a client error
    # (400) — never send raw, possibly secret-bearing text to the provider.
    redacted_yaml: str | None = None
    if mode in ("explain", "fix", "modify"):
        try:
            redacted_yaml = _redact_yaml(body.get("yaml", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if mode == "generate":
        plugins = body.get("plugins", {})
        system = _GENERATE_SYSTEM.format(
            pipeline_structure = _PIPELINE_STRUCTURE,
            connector_schema   = build_ai_context(prompt, plugins),
        )
        try:
            result = await _run_ai_call(request, "generate", system, prompt, max_tokens=1024, cfg=cfg)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return _yaml_mode_result(result)

    elif mode == "explain":
        user = (
            f"TRAM pipeline YAML:\n{redacted_yaml}\n\n"
            f"Dry-run error: {body.get('error', '')}\n\n"
            "Explain in 2-3 sentences what is wrong and how to fix it."
        )
        try:
            explanation = await _run_ai_call(
                request, "explain",
                "You are a helpful TRAM pipeline configuration assistant. "
                "Be concise and actionable.",
                user, max_tokens=300, cfg=cfg,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"explanation": explanation.text}

    elif mode == "fix":
        plugins = body.get("plugins", {})
        system = _GENERATE_SYSTEM.format(
            pipeline_structure = _PIPELINE_STRUCTURE,
            connector_schema   = build_ai_context(redacted_yaml + " " + body.get("error", ""), plugins),
        ) + "\nFix the provided YAML to resolve the error. Output ONLY valid TRAM pipeline YAML — no prose, no markdown fences."
        user = (
            f"TRAM pipeline YAML:\n{redacted_yaml}\n\n"
            f"Error to fix: {body.get('error', '')}"
        )
        try:
            result = await _run_ai_call(request, "fix", system, user, max_tokens=1024, cfg=cfg)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return _yaml_mode_result(result)

    elif mode == "modify":
        plugins = body.get("plugins", {})
        system = _GENERATE_SYSTEM.format(
            pipeline_structure = _PIPELINE_STRUCTURE,
            connector_schema   = build_ai_context(redacted_yaml + " " + body.get("instruction", ""), plugins),
        ) + "\nModify the provided YAML per the user instruction. Output ONLY the complete modified TRAM pipeline YAML — no prose, no markdown fences."
        user = (
            f"Existing pipeline YAML:\n{redacted_yaml}\n\n"
            f"Instruction: {body.get('instruction', '')}"
        )
        try:
            result = await _run_ai_call(request, "modify", system, user, max_tokens=1024, cfg=cfg)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return _yaml_mode_result(result)

    raise HTTPException(status_code=400, detail=f"Unknown mode: {mode!r}")
