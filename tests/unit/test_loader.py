"""Tests for pipeline loader — YAML parsing and env var substitution."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tram.core.exceptions import ConfigError
from tram.pipeline.loader import load_pipeline, load_pipeline_from_yaml, scan_pipeline_dir


def test_load_minimal_pipeline():
    yaml_text = textwrap.dedent("""
        version: "1"
        pipeline:
          name: test-minimal
          source:
            type: sftp
            host: localhost
            username: user
            password: pass
            remote_path: /data
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: sftp
            host: localhost
            username: user
            password: pass
            remote_path: /out
    """)
    config = load_pipeline_from_yaml(yaml_text)
    assert config.name == "test-minimal"
    assert config.source.type == "sftp"
    assert config.sink.type == "sftp"
    assert config.serializer_in.type == "json"
    assert config.serializer_out.type == "json"


def test_env_var_substitution(monkeypatch):
    monkeypatch.setenv("TEST_SFTP_HOST", "10.0.0.1")
    monkeypatch.setenv("TEST_SFTP_USER", "pmuser")
    monkeypatch.setenv("TEST_SFTP_PASS", "secret")

    yaml_text = textwrap.dedent("""
        pipeline:
          name: test-env
          source:
            type: sftp
            host: ${TEST_SFTP_HOST}
            username: ${TEST_SFTP_USER}
            password: ${TEST_SFTP_PASS}
            remote_path: /data
          serializer_in:
            type: csv
          serializer_out:
            type: json
          sink:
            type: sftp
            host: ${TEST_SFTP_HOST}
            username: ${TEST_SFTP_USER}
            password: ${TEST_SFTP_PASS}
            remote_path: /out
    """)
    config = load_pipeline_from_yaml(yaml_text)
    assert config.source.host == "10.0.0.1"
    assert config.source.username == "pmuser"


def test_env_var_default():
    yaml_text = textwrap.dedent("""
        pipeline:
          name: test-default
          source:
            type: sftp
            host: myhost
            port: ${NONEXISTENT_PORT:-2222}
            username: user
            password: pass
            remote_path: /data
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: sftp
            host: myhost
            username: user
            password: pass
            remote_path: /out
    """)
    config = load_pipeline_from_yaml(yaml_text)
    assert config.source.port == 2222


def test_missing_required_env_var_raises():
    yaml_text = textwrap.dedent("""
        pipeline:
          name: test-missing
          source:
            type: sftp
            host: ${MISSING_VAR_XYZ_ABC}
            username: user
            password: pass
            remote_path: /data
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: sftp
            host: localhost
            username: user
            password: pass
            remote_path: /out
    """)
    with pytest.raises(ConfigError, match="MISSING_VAR_XYZ_ABC"):
        load_pipeline_from_yaml(yaml_text)


def test_invalid_yaml_raises():
    with pytest.raises(ConfigError):
        load_pipeline_from_yaml("not: valid: yaml: {{{")


def test_invalid_pipeline_name_raises():
    yaml_text = textwrap.dedent("""
        pipeline:
          name: "invalid name with spaces!"
          source:
            type: sftp
            host: localhost
            username: user
            password: pass
            remote_path: /data
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: sftp
            host: localhost
            username: user
            password: pass
            remote_path: /out
    """)
    with pytest.raises(ConfigError):
        load_pipeline_from_yaml(yaml_text)


# ── load_pipeline (file-based) ─────────────────────────────────────────────


_FILE_YAML = """\
name: file-pipe
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


def test_load_pipeline_returns_tuple(tmp_path):
    f = tmp_path / "file-pipe.yaml"
    f.write_text(_FILE_YAML)
    config, raw = load_pipeline(f)
    assert config.name == "file-pipe"
    assert "file-pipe" in raw


def test_load_pipeline_file_not_found(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_pipeline(tmp_path / "missing.yaml")


def test_load_pipeline_unreadable(tmp_path, monkeypatch):
    f = tmp_path / "bad.yaml"
    f.write_text(_FILE_YAML)
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("perm denied")))
    with pytest.raises(ConfigError, match="Cannot read"):
        load_pipeline(f)


def test_load_pipeline_empty_file(tmp_path):
    f = tmp_path / "empty.yaml"
    f.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load_pipeline(f)


def test_load_pipeline_flat_format(tmp_path):
    """Pipeline YAML without top-level 'pipeline:' key is accepted."""
    f = tmp_path / "flat.yaml"
    f.write_text(_FILE_YAML)
    config, _ = load_pipeline(f)
    assert config.name == "file-pipe"


def test_load_pipeline_wrapped_format(tmp_path):
    """Pipeline YAML with top-level 'pipeline:' key is accepted."""
    wrapped = "pipeline:\n" + "\n".join(f"  {line}" for line in _FILE_YAML.splitlines()) + "\n"
    f = tmp_path / "wrapped.yaml"
    f.write_text(wrapped)
    config, _ = load_pipeline(f)
    assert config.name == "file-pipe"


def test_load_pipeline_env_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PIPE_NAME", "env-pipe")
    yaml_text = _FILE_YAML.replace("file-pipe", "${TEST_PIPE_NAME}")
    f = tmp_path / "env.yaml"
    f.write_text(yaml_text)
    config, _ = load_pipeline(f)
    assert config.name == "env-pipe"


def test_all_bundled_pipeline_examples_validate():
    pipeline_dir = Path("pipelines")
    failures: list[str] = []

    for path in sorted(pipeline_dir.glob("*.yaml")):
        try:
            load_pipeline_from_yaml(path.read_text())
        except Exception as exc:
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")

    assert not failures, "Bundled pipeline validation failures:\n" + "\n".join(failures)


# ── scan_pipeline_dir ──────────────────────────────────────────────────────


def test_scan_pipeline_dir_loads_all_yamls(tmp_path):
    for name in ("pipe-a", "pipe-b"):
        (tmp_path / f"{name}.yaml").write_text(
            _FILE_YAML.replace("file-pipe", name)
        )
    results = scan_pipeline_dir(tmp_path)
    names = [c.name for c, _ in results]
    assert "pipe-a" in names
    assert "pipe-b" in names


def test_scan_pipeline_dir_nonexistent_returns_empty(tmp_path):
    results = scan_pipeline_dir(tmp_path / "nonexistent")
    assert results == []


def test_scan_pipeline_dir_skips_invalid_yaml(tmp_path):
    (tmp_path / "good.yaml").write_text(_FILE_YAML)
    (tmp_path / "bad.yaml").write_text("not: valid: {{{")
    results = scan_pipeline_dir(tmp_path)
    assert len(results) == 1
    assert results[0][0].name == "file-pipe"
