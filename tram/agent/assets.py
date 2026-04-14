"""Asset synchronisation — pull schemas and MIBs from manager before running a pipeline.

Called by the worker agent before each pipeline execution.  When TRAM_MODE=worker
the worker pod has no persistent storage for user-uploaded assets; this module
fetches them on-demand from the manager's REST API and writes them to the
emptyDir volume at /data (or TRAM_DATA_DIR).

Strategy
--------
Schemas
    Fetch ALL schema files listed by GET /api/schemas on every run.
    Fetching the full set handles multi-file Protobuf bundles where
    one .proto imports another from the same directory — we can't know
    which files are transitively required without parsing every import
    graph, so pulling everything is simpler and always correct.
    Schema files are small text files; the round-trip is negligible.

MIBs
    Fetch only the modules explicitly named in the pipeline config
    (mib_modules: [...]). Standard MIBs (IF-MIB, ENTITY-MIB, …) are
    baked into the worker image at /mibs and never need to be fetched.
    Custom/vendor MIBs uploaded via the UI live on the manager's PVC
    and are pulled here into the emptyDir /data/mibs directory.

Auth
    If TRAM_API_KEY is set on the worker, it is forwarded as
    X-API-Key on every request to the manager.

Errors
    All errors are logged at WARNING level and swallowed.  A missing
    asset will surface as a clear error when the executor actually
    tries to open the file, which is the right place to fail.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_SCHEMA_SUBDIR = "schemas"
_MIB_SUBDIR    = "mibs"


# ── Public API ─────────────────────────────────────────────────────────────


def collect_mib_modules(config) -> list[str]:
    """Return deduplicated MIB module names referenced by *config*.

    Walks source and all sinks looking for ``mib_modules`` fields.
    """
    names: list[str] = []

    src = getattr(config, "source", None)
    if src is not None:
        names.extend(getattr(src, "mib_modules", None) or [])

    for sink in getattr(config, "sinks", None) or []:
        names.extend(getattr(sink, "mib_modules", None) or [])

    return list(dict.fromkeys(names))   # deduplicate, preserve order


def sync_assets(
    config,
    manager_url: str,
    data_dir: str = "/data",
    api_key: str = "",
) -> None:
    """Fetch all manager schemas + pipeline-referenced MIBs into *data_dir*.

    No-op when *manager_url* is empty (standalone / local mode).
    """
    if not manager_url:
        return

    mib_modules = collect_mib_modules(config)

    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    schema_dir = Path(data_dir) / _SCHEMA_SUBDIR
    mib_dir    = Path(data_dir) / _MIB_SUBDIR

    try:
        with httpx.Client(
            base_url=manager_url,
            headers=headers,
            timeout=30,
            follow_redirects=True,
        ) as client:
            _sync_all_schemas(client, schema_dir)
            for mib_name in mib_modules:
                _sync_mib(client, mib_name, mib_dir)
    except Exception as exc:
        logger.warning(
            "Asset sync failed",
            extra={"manager_url": manager_url, "error": str(exc)},
        )


# ── Internals ──────────────────────────────────────────────────────────────


def _sync_all_schemas(client: httpx.Client, schema_dir: Path) -> None:
    """Fetch every schema file listed by GET /api/schemas."""
    try:
        resp = client.get("/api/schemas")
        resp.raise_for_status()
        entries = resp.json()
    except Exception as exc:
        logger.warning("Could not list schemas from manager: %s", exc)
        return

    if not entries:
        return

    for entry in entries:
        rel_path = entry.get("path", "")
        if not rel_path:
            continue
        dest = schema_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = client.get(f"/api/schemas/{rel_path}")
            r.raise_for_status()
            dest.write_bytes(r.content)
            logger.debug("Synced schema %s", rel_path)
        except Exception as exc:
            logger.warning("Failed to fetch schema %s: %s", rel_path, exc)


def _sync_mib(client: httpx.Client, mib_name: str, mib_dir: Path) -> None:
    """Fetch a compiled MIB .py file from GET /api/mibs/{mib_name}."""
    dest = mib_dir / f"{mib_name}.py"
    mib_dir.mkdir(parents=True, exist_ok=True)
    try:
        resp = client.get(f"/api/mibs/{mib_name}")
        if resp.status_code == 404:
            # Standard MIBs baked into image — not present on manager, that's fine
            logger.debug("MIB %s not on manager (likely baked into image)", mib_name)
            return
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        logger.debug("Synced MIB %s", mib_name)
    except Exception as exc:
        logger.warning("Failed to fetch MIB %s: %s", mib_name, exc)
