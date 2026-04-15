"""Tests for tram CLI commands — direct + daemon-proxy."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import tram.cli.main as _cli_mod
from tram.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _patch_console(monkeypatch, caplog):
    """Replace Rich Console objects with MagicMocks.

    Two issues require this fixture:
    1. Rich Console internally wraps sys.stdout via a BytesIO/TextIOWrapper chain.
       When click's CliRunner captures stdout, the Console's internal stream state
       gets out of sync, causing 'I/O operation on closed file' in click's cleanup.
       Using MagicMock bypasses all Rich stream handling entirely.
    2. pyproject.toml has log_cli=true which makes pytest's live-log handler write
       to sys.stdout — i.e. click's captured BytesIO — during the test.  Using
       caplog here causes pytest to route log records through caplog instead of
       the live-log stream, preventing the BytesIO corruption.
    """
    import logging
    mock_console = MagicMock()
    monkeypatch.setattr(_cli_mod, "console", mock_console)
    monkeypatch.setattr(_cli_mod, "err_console", mock_console)
    with caplog.at_level(logging.WARNING):
        yield


# ── Fixtures ──────────────────────────────────────────────────────────────


_MINIMAL_YAML = """\
name: test-pipe
schedule:
  type: manual
source:
  type: local
  path: /tmp/in
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""


def _make_config(name="test-pipe"):
    from tram.pipeline.loader import load_pipeline_from_yaml
    return load_pipeline_from_yaml(_MINIMAL_YAML)


# ── version ────────────────────────────────────────────────────────────────


def test_version_prints_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0


# ── plugins ────────────────────────────────────────────────────────────────


def test_plugins_lists_categories():
    result = runner.invoke(app, ["plugins"])
    assert result.exit_code == 0


# ── validate ───────────────────────────────────────────────────────────────


def test_validate_success(tmp_path):
    pipeline_file = tmp_path / "test-pipe.yaml"
    pipeline_file.write_text(_MINIMAL_YAML)
    result = runner.invoke(app, ["validate", str(pipeline_file)])
    assert result.exit_code == 0


def test_validate_config_error(tmp_path):
    pipeline_file = tmp_path / "bad.yaml"
    pipeline_file.write_text("not: valid: pipeline: {{{")
    result = runner.invoke(app, ["validate", str(pipeline_file)])
    assert result.exit_code == 1


def test_validate_file_not_found(tmp_path):
    result = runner.invoke(app, ["validate", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1


def test_validate_with_lint_warning(tmp_path):
    """Pipeline with lint warning still exits 0."""
    pipeline_file = tmp_path / "warn.yaml"
    pipeline_file.write_text(_MINIMAL_YAML)
    mock_finding = MagicMock()
    mock_finding.severity = "warning"
    mock_finding.rule_id = "L001"
    mock_finding.message = "some lint warning"
    with patch("tram.pipeline.linter.lint", return_value=[mock_finding]):
        result = runner.invoke(app, ["validate", str(pipeline_file)])
    assert result.exit_code == 0


def test_validate_with_lint_error(tmp_path):
    """Lint error causes exit code 1."""
    pipeline_file = tmp_path / "err.yaml"
    pipeline_file.write_text(_MINIMAL_YAML)
    mock_finding = MagicMock()
    mock_finding.severity = "error"
    mock_finding.rule_id = "L002"
    mock_finding.message = "must have a description"
    with patch("tram.pipeline.linter.lint", return_value=[mock_finding]):
        result = runner.invoke(app, ["validate", str(pipeline_file)])
    assert result.exit_code == 1


# ── run --dry-run ──────────────────────────────────────────────────────────


def test_run_dry_run_valid(tmp_path):
    pipeline_file = tmp_path / "test-pipe.yaml"
    pipeline_file.write_text(_MINIMAL_YAML)
    mock_executor = MagicMock()
    mock_executor.dry_run.return_value = {"valid": True, "issues": []}
    with patch("tram.pipeline.executor.PipelineExecutor", return_value=mock_executor):
        result = runner.invoke(app, ["run", str(pipeline_file), "--dry-run"])
    assert result.exit_code == 0


def test_run_dry_run_invalid(tmp_path):
    pipeline_file = tmp_path / "test-pipe.yaml"
    pipeline_file.write_text(_MINIMAL_YAML)
    mock_executor = MagicMock()
    mock_executor.dry_run.return_value = {"valid": False, "issues": ["bad source"]}
    with patch("tram.pipeline.executor.PipelineExecutor", return_value=mock_executor):
        result = runner.invoke(app, ["run", str(pipeline_file), "--dry-run"])
    assert result.exit_code == 1


def test_run_dry_run_config_error(tmp_path):
    pipeline_file = tmp_path / "bad.yaml"
    pipeline_file.write_text("not: valid: {{{")
    result = runner.invoke(app, ["run", str(pipeline_file), "--dry-run"])
    assert result.exit_code == 1


# ── run (batch) ────────────────────────────────────────────────────────────


def test_run_batch_success(tmp_path):
    pipeline_file = tmp_path / "test-pipe.yaml"
    pipeline_file.write_text(_MINIMAL_YAML)
    mock_executor = MagicMock()
    mock_result = MagicMock()
    mock_result.status.value = "success"
    mock_result.records_in = 10
    mock_result.records_out = 10
    mock_result.records_skipped = 0
    mock_executor.batch_run.return_value = mock_result
    with patch("tram.pipeline.executor.PipelineExecutor", return_value=mock_executor):
        result = runner.invoke(app, ["run", str(pipeline_file)])
    assert result.exit_code == 0


def test_run_batch_failure(tmp_path):
    pipeline_file = tmp_path / "test-pipe.yaml"
    pipeline_file.write_text(_MINIMAL_YAML)
    mock_executor = MagicMock()
    mock_result = MagicMock()
    mock_result.status.value = "failed"
    mock_result.error = "source unreachable"
    mock_executor.batch_run.return_value = mock_result
    with patch("tram.pipeline.executor.PipelineExecutor", return_value=mock_executor):
        result = runner.invoke(app, ["run", str(pipeline_file)])
    assert result.exit_code == 1


# ── pipeline proxy commands ────────────────────────────────────────────────


def _mock_get(return_value):
    return patch("tram.cli.main._api_get", return_value=return_value)


def _mock_post(return_value=None):
    return patch("tram.cli.main._api_post", return_value=return_value or {})


def _mock_delete():
    return patch("tram.cli.main._api_delete", return_value=None)


def test_pipeline_list_empty():
    with _mock_get([]):
        result = runner.invoke(app, ["pipeline", "list"])
    assert result.exit_code == 0


def test_pipeline_list_with_pipelines():
    pipelines = [{"name": "my-pipe", "status": "running", "schedule_type": "interval",
                  "enabled": True, "last_run": None, "last_run_status": None}]
    with _mock_get(pipelines):
        result = runner.invoke(app, ["pipeline", "list"])
    assert result.exit_code == 0


def test_pipeline_add(tmp_path):
    pipeline_file = tmp_path / "pipe.yaml"
    pipeline_file.write_text(_MINIMAL_YAML)
    with _mock_post({"name": "test-pipe"}):
        result = runner.invoke(app, ["pipeline", "add", str(pipeline_file)])
    assert result.exit_code == 0


def test_pipeline_add_file_not_found(tmp_path):
    result = runner.invoke(app, ["pipeline", "add", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1


def test_pipeline_remove():
    with _mock_delete():
        result = runner.invoke(app, ["pipeline", "remove", "my-pipe"])
    assert result.exit_code == 0


def test_pipeline_start():
    with _mock_post():
        result = runner.invoke(app, ["pipeline", "start", "my-pipe"])
    assert result.exit_code == 0


def test_pipeline_stop():
    with _mock_post():
        result = runner.invoke(app, ["pipeline", "stop", "my-pipe"])
    assert result.exit_code == 0


def test_pipeline_run():
    with _mock_post():
        result = runner.invoke(app, ["pipeline", "run", "my-pipe"])
    assert result.exit_code == 0


def test_pipeline_status():
    with _mock_get({"name": "my-pipe", "status": "stopped"}):
        result = runner.invoke(app, ["pipeline", "status", "my-pipe"])
    assert result.exit_code == 0


def test_pipeline_reload():
    with _mock_post({"reloaded": 3, "total": 3}):
        result = runner.invoke(app, ["pipeline", "reload"])
    assert result.exit_code == 0


def test_pipeline_history_empty():
    with _mock_get([]):
        result = runner.invoke(app, ["pipeline", "history", "my-pipe"])
    assert result.exit_code == 0


def test_pipeline_history_with_versions():
    versions = [{"version": 1, "created_at": "2026-04-01T00:00:00Z", "is_active": 1}]
    with _mock_get(versions):
        result = runner.invoke(app, ["pipeline", "history", "my-pipe"])
    assert result.exit_code == 0


def test_pipeline_rollback():
    with _mock_post({"name": "my-pipe", "status": "stopped", "rolled_back_to_version": 2}):
        result = runner.invoke(app, ["pipeline", "rollback", "my-pipe", "--version", "2"])
    assert result.exit_code == 0


# ── pipeline init ──────────────────────────────────────────────────────────


def test_pipeline_init_stdout():
    result = runner.invoke(app, ["pipeline", "init", "my-pipeline"])
    assert result.exit_code == 0
    # console is mocked; verify the yaml text was passed to console.print
    printed = str(_cli_mod.console.print.call_args)
    assert "my-pipeline" in printed


def test_pipeline_init_to_file(tmp_path):
    out = tmp_path / "out.yaml"
    result = runner.invoke(app, ["pipeline", "init", "my-pipeline", "--output", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    assert "my-pipeline" in out.read_text()


def test_pipeline_init_invalid_name():
    result = runner.invoke(app, ["pipeline", "init", "bad name!"])
    assert result.exit_code == 1


# ── runs commands ──────────────────────────────────────────────────────────


def test_runs_list_empty():
    with _mock_get([]):
        result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0
    printed = str(_cli_mod.console.print.call_args)
    assert "No runs" in printed


def test_runs_list_with_data():
    runs = [{"run_id": "abc123", "pipeline": "my-pipe", "status": "success",
             "records_in": 100, "records_out": 99, "finished_at": "2026-04-01T00:00:00Z"}]
    with _mock_get(runs):
        result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0
    # A Rich Table is passed to console.print (not an empty-list message)
    from rich.table import Table
    args, _ = _cli_mod.console.print.call_args
    assert isinstance(args[0], Table)


def test_runs_list_with_pipeline_filter():
    with _mock_get([]):
        result = runner.invoke(app, ["runs", "list", "--pipeline", "my-pipe"])
    assert result.exit_code == 0


def test_runs_get():
    with _mock_get({"run_id": "abc123", "pipeline": "my-pipe", "status": "success"}):
        result = runner.invoke(app, ["runs", "get", "abc123"])
    assert result.exit_code == 0
    all_calls = str(_cli_mod.console.print.call_args_list)
    assert "abc123" in all_calls


# ── API connection errors ──────────────────────────────────────────────────


def test_api_connect_error_get():
    with patch("tram.cli.main._api_get", side_effect=SystemExit(1)):
        result = runner.invoke(app, ["pipeline", "list"])
    assert result.exit_code == 1


def test_api_connect_error_post():
    with patch("tram.cli.main._api_post",
               side_effect=SystemExit(1)):
        result = runner.invoke(app, ["pipeline", "start", "my-pipe"])
    assert result.exit_code == 1


# ── auth header helper ─────────────────────────────────────────────────────


def test_auth_headers_with_api_key(monkeypatch):
    monkeypatch.setenv("TRAM_API_KEY", "secret")
    from tram.cli.main import _auth_headers
    headers = _auth_headers()
    assert headers == {"X-API-Key": "secret"}


def test_auth_headers_without_api_key(monkeypatch):
    monkeypatch.delenv("TRAM_API_KEY", raising=False)
    from tram.cli.main import _auth_headers
    assert _auth_headers() == {}


# ── _api_get / _api_post / _api_delete direct tests ───────────────────────


def test_api_get_success(monkeypatch):
    import httpx
    import respx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    monkeypatch.delenv("TRAM_API_KEY", raising=False)
    from tram.cli.main import _api_get

    with respx.mock:
        respx.get("http://localhost:8765/api/pipelines").mock(
            return_value=httpx.Response(200, json=[{"name": "p1"}])
        )
        result = _api_get("/api/pipelines")
    assert result == [{"name": "p1"}]


def test_api_get_connect_error(monkeypatch):
    import httpx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_get

    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(Exception) as exc:
            _api_get("/api/pipelines")
    assert exc.value.exit_code == 1


def test_api_get_http_error(monkeypatch):
    import httpx
    import respx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_get

    with respx.mock:
        respx.get("http://localhost:8765/api/pipelines").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        with pytest.raises(Exception) as exc:
            _api_get("/api/pipelines")
    assert exc.value.exit_code == 1


def test_api_post_with_str_body(monkeypatch):
    import httpx
    import respx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    monkeypatch.delenv("TRAM_API_KEY", raising=False)
    from tram.cli.main import _api_post

    with respx.mock:
        respx.post("http://localhost:8765/api/pipelines").mock(
            return_value=httpx.Response(200, json={"name": "p1"})
        )
        result = _api_post("/api/pipelines", body="yaml: text", content_type="text/plain")
    assert result == {"name": "p1"}


def test_api_post_with_dict_body(monkeypatch):
    import httpx
    import respx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_post

    with respx.mock:
        respx.post("http://localhost:8765/api/test").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        result = _api_post("/api/test", body={"key": "val"})
    assert result == {"ok": True}


def test_api_post_204_returns_empty(monkeypatch):
    import httpx
    import respx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_post

    with respx.mock:
        respx.post("http://localhost:8765/api/test").mock(
            return_value=httpx.Response(204)
        )
        result = _api_post("/api/test")
    assert result == {}


def test_api_post_no_body(monkeypatch):
    import httpx
    import respx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_post

    with respx.mock:
        respx.post("http://localhost:8765/api/run").mock(
            return_value=httpx.Response(200, json={})
        )
        result = _api_post("/api/run")
    assert result == {}


def test_api_post_connect_error(monkeypatch):
    import httpx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_post

    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(Exception) as exc:
            _api_post("/api/test")
    assert exc.value.exit_code == 1


def test_api_post_http_error(monkeypatch):
    import httpx
    import respx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_post

    with respx.mock:
        respx.post("http://localhost:8765/api/test").mock(
            return_value=httpx.Response(500, text="Internal error")
        )
        with pytest.raises(Exception) as exc:
            _api_post("/api/test")
    assert exc.value.exit_code == 1


def test_api_delete_success(monkeypatch):
    import httpx
    import respx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_delete

    with respx.mock:
        respx.delete("http://localhost:8765/api/pipelines/p1").mock(
            return_value=httpx.Response(200)
        )
        _api_delete("/api/pipelines/p1")  # should not raise


def test_api_delete_connect_error(monkeypatch):
    import httpx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_delete

    with patch("httpx.delete", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(Exception) as exc:
            _api_delete("/api/pipelines/p1")
    assert exc.value.exit_code == 1


def test_api_delete_http_error(monkeypatch):
    import httpx
    import respx

    monkeypatch.setenv("TRAM_API_URL", "http://localhost:8765")
    from tram.cli.main import _api_delete

    with respx.mock:
        respx.delete("http://localhost:8765/api/pipelines/p1").mock(
            return_value=httpx.Response(404, text="Not found")
        )
        with pytest.raises(Exception) as exc:
            _api_delete("/api/pipelines/p1")
    assert exc.value.exit_code == 1


# ── daemon command ─────────────────────────────────────────────────────────


def test_daemon_command_calls_serve(monkeypatch, caplog):
    import logging
    with patch("tram.daemon.server.serve") as mock_serve, \
         caplog.at_level(logging.WARNING):
        result = runner.invoke(app, ["daemon"])
    assert result.exit_code == 0
    mock_serve.assert_called_once()


def test_daemon_command_with_host_port_loglevel(monkeypatch, caplog):
    import logging
    # Register cleanup for env vars set directly via os.environ inside the CLI code.
    # monkeypatch.delenv records the absent state so teardown removes them afterward.
    monkeypatch.delenv("TRAM_HOST", raising=False)
    monkeypatch.delenv("TRAM_PORT", raising=False)
    monkeypatch.delenv("TRAM_LOG_LEVEL", raising=False)
    with patch("tram.daemon.server.serve") as mock_serve, \
         caplog.at_level(logging.WARNING):
        result = runner.invoke(app, ["daemon", "--host", "0.0.0.0", "--port", "9000",
                                     "--log-level", "debug"])
    assert result.exit_code == 0
    mock_serve.assert_called_once()
