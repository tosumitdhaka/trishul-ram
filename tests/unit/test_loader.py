"""Tests for pipeline loader — YAML parsing and env var substitution."""

from __future__ import annotations

import os
import textwrap

import pytest

from tram.core.exceptions import ConfigError
from tram.pipeline.loader import load_pipeline_from_yaml


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
