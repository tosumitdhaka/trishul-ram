from __future__ import annotations

import pytest

from tram.models.pipeline import PipelineConfig


def _base_pipeline() -> dict:
    return {
        "name": "webhook-pipeline",
        "schedule": {"type": "stream"},
        "source": {"type": "webhook", "path": "/ingest"},
        "serializer_in": {"type": "json"},
        "sinks": [{"type": "local", "path": "/tmp/out"}],
    }


def test_kubernetes_block_valid_for_stream_webhook() -> None:
    cfg = PipelineConfig.model_validate(
        {
            **_base_pipeline(),
            "kubernetes": {
                "enabled": True,
                "service_type": "NodePort",
                "node_port": 30042,
            },
        }
    )
    assert cfg.kubernetes is not None
    assert cfg.kubernetes.node_port == 30042


def test_kubernetes_block_valid_for_workers_list() -> None:
    cfg = PipelineConfig.model_validate(
        {
            **_base_pipeline(),
            "workers": {"list": ["tram-worker-0", "tram-worker-1"]},
            "kubernetes": {
                "enabled": True,
                "service_type": "NodePort",
                "node_port": 30042,
            },
        }
    )
    assert cfg.workers is not None
    assert cfg.workers.worker_ids == ["tram-worker-0", "tram-worker-1"]


def test_kubernetes_block_rejected_for_non_http_push_source() -> None:
    with pytest.raises(ValueError, match="webhook and prometheus_rw"):
        PipelineConfig.model_validate(
            {
                **_base_pipeline(),
                "source": {"type": "local", "path": "/tmp"},
                "kubernetes": {"enabled": True},
            }
        )


def test_kubernetes_block_rejected_for_non_stream_pipeline() -> None:
    with pytest.raises(ValueError, match="only supported for stream pipelines"):
        PipelineConfig.model_validate(
            {
                **_base_pipeline(),
                "schedule": {"type": "manual"},
                "kubernetes": {"enabled": True},
            }
        )


def test_kubernetes_node_port_rejected_for_load_balancer() -> None:
    with pytest.raises(ValueError, match="only valid when service_type=NodePort"):
        PipelineConfig.model_validate(
            {
                **_base_pipeline(),
                "kubernetes": {
                    "enabled": True,
                    "service_type": "LoadBalancer",
                    "node_port": 30042,
                },
            }
        )


def test_kubernetes_custom_service_name_must_be_dns1123() -> None:
    with pytest.raises(ValueError, match="DNS-1123"):
        PipelineConfig.model_validate(
            {
                **_base_pipeline(),
                "kubernetes": {
                    "enabled": True,
                    "service_name": "Bad_Name",
                },
            }
        )
