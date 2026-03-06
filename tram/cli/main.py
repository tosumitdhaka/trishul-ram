"""TRAM CLI — direct commands + daemon-proxy pipeline commands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="tram",
    help="TRAM — Trishul Real-time Adapter & Mapper",
    add_completion=False,
)
pipeline_app = typer.Typer(help="Pipeline management commands (proxies to running daemon)")
runs_app = typer.Typer(help="Run history commands")
mib_app = typer.Typer(help="SNMP MIB utilities")

app.add_typer(pipeline_app, name="pipeline")
app.add_typer(runs_app, name="runs")
app.add_typer(mib_app, name="mib")

console = Console()
err_console = Console(stderr=True)


# ── Shared helpers ─────────────────────────────────────────────────────────


def _get_api_url() -> str:
    import os
    return os.environ.get("TRAM_API_URL", "http://localhost:8765")


def _auth_headers() -> dict[str, str]:
    """Return X-API-Key header dict if TRAM_API_KEY is set."""
    import os
    key = os.environ.get("TRAM_API_KEY", "")
    return {"X-API-Key": key} if key else {}


def _api_get(path: str) -> dict | list:
    import httpx
    url = f"{_get_api_url()}{path}"
    try:
        resp = httpx.get(url, headers=_auth_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.ConnectError:
        err_console.print(f"[red]Cannot connect to TRAM daemon at {_get_api_url()}[/red]")
        err_console.print("  Start it with: [bold]tram daemon[/bold]")
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        err_console.print(f"[red]API error {exc.response.status_code}: {exc.response.text}[/red]")
        raise typer.Exit(1)


def _api_post(path: str, body: str | dict | None = None, content_type: str = "application/json") -> dict:
    import httpx
    url = f"{_get_api_url()}{path}"
    auth = _auth_headers()
    try:
        if isinstance(body, str):
            headers = {"Content-Type": content_type, **auth}
            resp = httpx.post(url, content=body.encode(), headers=headers, timeout=30)
        elif isinstance(body, dict):
            resp = httpx.post(url, json=body, headers=auth, timeout=30)
        else:
            resp = httpx.post(url, headers=auth, timeout=30)
        resp.raise_for_status()
        if resp.status_code == 204:
            return {}
        return resp.json()
    except httpx.ConnectError:
        err_console.print(f"[red]Cannot connect to TRAM daemon at {_get_api_url()}[/red]")
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        err_console.print(f"[red]API error {exc.response.status_code}: {exc.response.text}[/red]")
        raise typer.Exit(1)


def _api_delete(path: str) -> None:
    import httpx
    url = f"{_get_api_url()}{path}"
    try:
        resp = httpx.delete(url, headers=_auth_headers(), timeout=10)
        resp.raise_for_status()
    except httpx.ConnectError:
        err_console.print(f"[red]Cannot connect to TRAM daemon at {_get_api_url()}[/red]")
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        err_console.print(f"[red]API error {exc.response.status_code}: {exc.response.text}[/red]")
        raise typer.Exit(1)


# ── Direct commands (no daemon needed) ────────────────────────────────────


@app.command()
def version():
    """Print TRAM version and exit."""
    from tram import __version__
    console.print(f"tram {__version__}")


@app.command()
def plugins():
    """Print all registered plugin keys by category."""
    # Import plugins to trigger registration
    import tram.connectors  # noqa: F401
    import tram.serializers  # noqa: F401
    import tram.transforms  # noqa: F401
    from tram.registry.registry import list_plugins

    data = list_plugins()
    for category, keys in data.items():
        console.print(f"[bold cyan]{category}:[/bold cyan] {', '.join(keys) if keys else '(none)'}")


@app.command()
def validate(
    pipeline_file: Path = typer.Argument(..., help="Path to pipeline YAML file"),
):
    """Validate a pipeline YAML file (schema check + lint). Exit 0 on success."""
    from tram.pipeline.loader import load_pipeline
    from tram.core.exceptions import ConfigError

    try:
        config = load_pipeline(pipeline_file)
    except ConfigError as exc:
        err_console.print(f"[red]✗ Validation failed:[/red]\n{exc}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Pipeline '{config.name}' is valid")

    # Run linter
    try:
        from tram.pipeline.linter import lint
        findings = lint(config)
        has_error = False
        for f in findings:
            color = "red" if f.severity == "error" else "yellow"
            console.print(f"  [{color}]{f.severity.upper()}[/{color}] [{f.rule_id}] {f.message}")
            if f.severity == "error":
                has_error = True
        if has_error:
            raise typer.Exit(1)
    except ImportError:
        pass  # linter module not available

    raise typer.Exit(0)


@app.command()
def run(
    pipeline_file: Path = typer.Argument(..., help="Path to pipeline YAML file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate wiring only, no I/O"),
):
    """Execute a pipeline once and exit (no daemon needed)."""
    from tram.core.config import AppConfig
    from tram.core.log_config import setup_logging
    from tram.core.exceptions import ConfigError
    from tram.pipeline.loader import load_pipeline
    from tram.pipeline.executor import PipelineExecutor

    config = AppConfig.from_env()
    setup_logging(level=config.log_level, fmt="text")

    # Import plugins
    import tram.connectors  # noqa: F401
    import tram.serializers  # noqa: F401
    import tram.transforms  # noqa: F401

    try:
        pipeline_config = load_pipeline(pipeline_file)
    except ConfigError as exc:
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(1)

    executor = PipelineExecutor()

    if dry_run:
        result = executor.dry_run(pipeline_config)
        if result["valid"]:
            console.print(f"[green]✓[/green] Dry run OK — pipeline '{pipeline_config.name}' wiring is valid")
        else:
            err_console.print("[red]✗ Dry run failed:[/red]")
            for issue in result["issues"]:
                err_console.print(f"  • {issue}")
            raise typer.Exit(1)
    else:
        console.print(f"Running pipeline [bold]{pipeline_config.name}[/bold]...")
        result = executor.batch_run(pipeline_config)
        if result.status.value == "success":
            console.print(
                f"[green]✓[/green] Completed: {result.records_in} records in, "
                f"{result.records_out} out, {result.records_skipped} skipped"
            )
        else:
            err_console.print(f"[red]✗ Failed:[/red] {result.error}")
            raise typer.Exit(1)


@app.command()
def daemon(
    host: str = typer.Option(None, "--host", help="Bind address"),
    port: int = typer.Option(None, "--port", help="Port"),
    log_level: str = typer.Option(None, "--log-level", help="Log level"),
):
    """Start the TRAM daemon (scheduler + REST API)."""
    import os
    from tram.core.config import AppConfig
    from tram.daemon.server import serve

    if host:
        os.environ["TRAM_HOST"] = host
    if port:
        os.environ["TRAM_PORT"] = str(port)
    if log_level:
        os.environ["TRAM_LOG_LEVEL"] = log_level.upper()

    config = AppConfig.from_env()
    serve(config)


# ── Pipeline proxy commands ────────────────────────────────────────────────


@pipeline_app.command("list")
def pipeline_list():
    """List all registered pipelines."""
    data = _api_get("/api/pipelines")
    if not data:
        console.print("No pipelines registered.")
        return

    table = Table(title="Pipelines")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Schedule")
    table.add_column("Enabled")
    table.add_column("Last Run")
    table.add_column("Last Status")

    for p in data:
        status_color = "green" if p["status"] == "running" else "yellow" if p["status"] == "stopped" else "red"
        table.add_row(
            p["name"],
            f"[{status_color}]{p['status']}[/{status_color}]",
            p.get("schedule_type", "—"),
            "✓" if p.get("enabled") else "✗",
            p.get("last_run") or "—",
            p.get("last_run_status") or "—",
        )

    console.print(table)


@pipeline_app.command("add")
def pipeline_add(
    pipeline_file: Path = typer.Argument(..., help="Path to pipeline YAML file"),
):
    """Register a new pipeline from a YAML file."""
    if not pipeline_file.exists():
        err_console.print(f"[red]File not found:[/red] {pipeline_file}")
        raise typer.Exit(1)

    yaml_text = pipeline_file.read_text()
    result = _api_post("/api/pipelines", body=yaml_text, content_type="text/plain")
    console.print(f"[green]✓[/green] Registered pipeline '{result.get('name')}'")


@pipeline_app.command("remove")
def pipeline_remove(
    name: str = typer.Argument(..., help="Pipeline name"),
):
    """Deregister a pipeline."""
    _api_delete(f"/api/pipelines/{name}")
    console.print(f"[green]✓[/green] Removed pipeline '{name}'")


@pipeline_app.command("start")
def pipeline_start(name: str = typer.Argument(..., help="Pipeline name")):
    """Start a pipeline."""
    _api_post(f"/api/pipelines/{name}/start")
    console.print(f"[green]✓[/green] Started '{name}'")


@pipeline_app.command("stop")
def pipeline_stop(name: str = typer.Argument(..., help="Pipeline name")):
    """Stop a pipeline."""
    _api_post(f"/api/pipelines/{name}/stop")
    console.print(f"[green]✓[/green] Stopped '{name}'")


@pipeline_app.command("run")
def pipeline_run(name: str = typer.Argument(..., help="Pipeline name")):
    """Trigger one immediate batch run."""
    _api_post(f"/api/pipelines/{name}/run")
    console.print(f"[green]✓[/green] Triggered run for '{name}'")


@pipeline_app.command("status")
def pipeline_status(name: str = typer.Argument(..., help="Pipeline name")):
    """Show pipeline config and live status."""
    data = _api_get(f"/api/pipelines/{name}")
    for key, val in data.items():
        console.print(f"  [bold]{key}:[/bold] {val}")


@pipeline_app.command("reload")
def pipeline_reload():
    """Reload all pipelines from pipeline_dir."""
    result = _api_post("/api/pipelines/reload")
    console.print(f"[green]✓[/green] Reloaded {result.get('reloaded', 0)} / {result.get('total', 0)} pipelines")


@pipeline_app.command("history")
def pipeline_history(
    name: str = typer.Argument(..., help="Pipeline name"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """Show version history for a pipeline."""
    data = _api_get(f"/api/pipelines/{name}/versions")
    if not data:
        console.print(f"No saved versions found for pipeline '{name}'.")
        return

    table = Table(title=f"Versions: {name}")
    table.add_column("Version", justify="right")
    table.add_column("Created At")
    table.add_column("Active")

    for v in data[:limit]:
        active_mark = "[green]✓[/green]" if v.get("is_active") else ""
        table.add_row(
            str(v["version"]),
            v.get("created_at", "—"),
            active_mark,
        )

    console.print(table)


@pipeline_app.command("rollback")
def pipeline_rollback(
    name: str = typer.Argument(..., help="Pipeline name"),
    version: int = typer.Option(..., "--version", "-v", help="Version number to restore"),
):
    """Rollback a pipeline to a previously saved version."""
    result = _api_post(f"/api/pipelines/{name}/rollback?version={version}")
    console.print(
        f"[green]✓[/green] Rolled back pipeline '{name}' to version {version}. "
        f"Status: {result.get('status', '—')}"
    )


# ── Runs proxy commands ────────────────────────────────────────────────────


@runs_app.command("list")
def runs_list(
    pipeline: Optional[str] = typer.Option(None, "--pipeline", "-p", help="Filter by pipeline"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """List run history."""
    path = f"/api/runs?limit={limit}"
    if pipeline:
        path += f"&pipeline={pipeline}"
    data = _api_get(path)

    if not data:
        console.print("No runs found.")
        return

    table = Table(title="Run History")
    table.add_column("Run ID", style="dim")
    table.add_column("Pipeline", style="bold")
    table.add_column("Status")
    table.add_column("Records In", justify="right")
    table.add_column("Records Out", justify="right")
    table.add_column("Finished")

    for r in data:
        status = r["status"]
        color = "green" if status == "success" else "red" if status == "failed" else "yellow"
        table.add_row(
            r["run_id"],
            r["pipeline"],
            f"[{color}]{status}[/{color}]",
            str(r.get("records_in", 0)),
            str(r.get("records_out", 0)),
            r.get("finished_at", "—"),
        )

    console.print(table)


@runs_app.command("get")
def runs_get(run_id: str = typer.Argument(..., help="Run ID")):
    """Get a single run result."""
    data = _api_get(f"/api/runs/{run_id}")
    for key, val in data.items():
        console.print(f"  [bold]{key}:[/bold] {val}")


# ── Pipeline init ──────────────────────────────────────────────────────────


@pipeline_app.command("init")
def pipeline_init(
    name: str = typer.Argument(..., help="Pipeline name (alphanumeric, hyphens, underscores)"),
    source: str = typer.Option("local", "--source", "-s", help="Source type"),
    sink: str = typer.Option("local", "--sink", "-k", help="Sink type"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write to file (default: stdout)"
    ),
):
    """Scaffold a minimal valid pipeline YAML."""
    import re
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        err_console.print(
            f"[red]Invalid pipeline name:[/red] '{name}' — "
            "use only alphanumeric characters, hyphens, or underscores"
        )
        raise typer.Exit(1)

    yaml_text = f"""\
version: "1"
pipeline:
  name: {name}
  enabled: true
  schedule:
    type: manual
  source:
    type: {source}
    path: ./input
  serializer_in:
    type: json
  serializer_out:
    type: json
  sink:
    type: {sink}
    path: ./output
  on_error: continue
"""
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(yaml_text)
        console.print(f"[green]✓[/green] Scaffolded pipeline '{name}' → {output}")
    else:
        console.print(yaml_text, end="")


# ── MIB utilities ──────────────────────────────────────────────────────────


@mib_app.command("compile")
def mib_compile(
    source_mib: Path = typer.Argument(..., help="Path to .mib source file"),
    out: Path = typer.Option(
        Path("./compiled_mibs"), "--out", "-o", help="Output directory for compiled .py files"
    ),
):
    """Compile a raw .mib text file to Python format for use with tram[snmp].

    Requires the tram[mib] optional extra (pysmi-lextudio).
    """
    try:
        from pysmi.reader import FileReader
        from pysmi.searcher import PyFileSearcher, StubSearcher
        from pysmi.writer import PyFileWriter
        from pysmi.parser.smi import parserFactory
        from pysmi.codegen.pysnmp import PySnmpCodeGen
        from pysmi.compiler import MibCompiler
    except ImportError:
        err_console.print(
            "[red]pysmi-lextudio is required for MIB compilation.[/red]\n"
            "  Install with: [bold]pip install tram[mib][/bold]"
        )
        raise typer.Exit(1)

    if not source_mib.exists():
        err_console.print(f"[red]File not found:[/red] {source_mib}")
        raise typer.Exit(1)

    out.mkdir(parents=True, exist_ok=True)
    mib_dir = str(source_mib.parent)
    mib_name = source_mib.stem

    parser = parserFactory()()
    codegen = PySnmpCodeGen()
    writer = PyFileWriter(str(out))

    compiler = MibCompiler(parser, codegen, writer)
    compiler.addSources(FileReader(mib_dir))
    compiler.addSearchers(PyFileSearcher(str(out)))
    compiler.addSearchers(StubSearcher(*PySnmpCodeGen.PYSNMP_STUBS))

    results = compiler.compile(mib_name)
    for name, status in results.items():
        color = "green" if status == "compiled" else "yellow"
        console.print(f"  [{color}]{status}[/{color}] {name}")
    console.print(f"[green]✓[/green] Compiled MIB '{mib_name}' → {out}")
