from __future__ import annotations

from types import SimpleNamespace

from tram.models.pipeline import PipelineConfig
from tram.pipeline.k8s_service_manager import KubernetesServiceManager


class _ApiException(Exception):
    def __init__(self, status: int):
        super().__init__(f"status={status}")
        self.status = status


class _FakeCoreV1Api:
    def __init__(self):
        self.created: list[tuple[str, dict]] = []
        self.patched: list[tuple[str, str, dict]] = []
        self.deleted: list[tuple[str, str]] = []
        self.services: dict[tuple[str, str], dict] = {}
        self.endpoints_created: list[tuple[str, dict]] = []
        self.endpoints_patched: list[tuple[str, str, dict]] = []
        self.endpoints_deleted: list[tuple[str, str]] = []
        self.endpoints: dict[tuple[str, str], dict] = {}
        self.pods: dict[tuple[str, str], object] = {}

    def read_namespaced_service(self, name: str, namespace: str):
        key = (namespace, name)
        if key not in self.services:
            raise _ApiException(404)
        return self.services[key]

    def create_namespaced_service(self, namespace: str, body: dict):
        key = (namespace, body["metadata"]["name"])
        self.created.append((namespace, body))
        self.services[key] = body
        return body

    def patch_namespaced_service(self, name: str, namespace: str, body: dict):
        key = (namespace, name)
        self.patched.append((name, namespace, body))
        self.services[key] = body
        return body

    def delete_namespaced_service(self, name: str, namespace: str):
        key = (namespace, name)
        if key not in self.services:
            raise _ApiException(404)
        self.deleted.append((name, namespace))
        del self.services[key]

    def read_namespaced_endpoints(self, name: str, namespace: str):
        key = (namespace, name)
        if key not in self.endpoints:
            raise _ApiException(404)
        return self.endpoints[key]

    def create_namespaced_endpoints(self, namespace: str, body: dict):
        key = (namespace, body["metadata"]["name"])
        self.endpoints_created.append((namespace, body))
        self.endpoints[key] = body
        return body

    def patch_namespaced_endpoints(self, name: str, namespace: str, body: dict):
        key = (namespace, name)
        self.endpoints_patched.append((name, namespace, body))
        self.endpoints[key] = body
        return body

    def delete_namespaced_endpoints(self, name: str, namespace: str):
        key = (namespace, name)
        if key not in self.endpoints:
            raise _ApiException(404)
        self.endpoints_deleted.append((name, namespace))
        del self.endpoints[key]

    def read_namespaced_pod(self, name: str, namespace: str):
        key = (namespace, name)
        return self.pods[key]


def _pipeline() -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "name": "my_webhook_pipeline",
            "schedule": {"type": "stream"},
            "source": {"type": "webhook", "path": "/ingest"},
            "serializer_in": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "kubernetes": {"enabled": True, "service_type": "NodePort", "node_port": 30042},
        }
    )


def _list_pipeline() -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "name": "my_webhook_pipeline",
            "schedule": {"type": "stream"},
            "source": {"type": "webhook", "path": "/ingest"},
            "serializer_in": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "workers": {"list": ["tram-worker-0", "tram-worker-2"]},
            "kubernetes": {"enabled": True, "service_type": "NodePort", "node_port": 30042},
        }
    )


def test_generate_service_name_is_deterministic_and_bounded() -> None:
    name = KubernetesServiceManager.generate_service_name("a_very_long_pipeline_name_" * 4)
    assert len(name) <= 63
    assert name.startswith("tram-p-")
    assert KubernetesServiceManager.generate_service_name("same-name") == KubernetesServiceManager.generate_service_name("same-name")


def test_build_service_body_for_standalone_targets_current_pod() -> None:
    manager = KubernetesServiceManager(
        mode="standalone",
        node_id="tram-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=_FakeCoreV1Api(),
    )
    body = manager._build_service_body(_pipeline())
    assert body["spec"]["selector"] == {"statefulset.kubernetes.io/pod-name": "tram-0"}
    assert body["spec"]["ports"][0]["targetPort"] == 8765
    assert body["spec"]["ports"][0]["nodePort"] == 30042


def test_build_service_body_for_manager_targets_workers() -> None:
    api = _FakeCoreV1Api()
    api.pods[("default", "tram-manager-0")] = SimpleNamespace(
        metadata=SimpleNamespace(
            labels={
                "app.kubernetes.io/name": "tram",
                "app.kubernetes.io/instance": "release-a",
                "app.kubernetes.io/component": "manager",
            }
        )
    )
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    body = manager._build_service_body(_pipeline())
    assert body["spec"]["selector"] == {
        "app.kubernetes.io/name": "tram",
        "app.kubernetes.io/instance": "release-a",
        "app.kubernetes.io/component": "worker",
    }
    assert body["spec"]["ports"][0]["targetPort"] == 8767


def test_build_service_body_for_manager_named_workers_omits_selector() -> None:
    api = _FakeCoreV1Api()
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    body = manager._build_service_body(_list_pipeline())
    assert "selector" not in body["spec"]
    assert body["spec"]["ports"][0]["targetPort"] == 8767


def test_build_endpoints_body_for_named_workers_targets_specific_pods() -> None:
    api = _FakeCoreV1Api()
    api.pods[("default", "tram-worker-0")] = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.10"))
    api.pods[("default", "tram-worker-2")] = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.12"))
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    body = manager._build_endpoints_body(_list_pipeline())
    addresses = body["subsets"][0]["addresses"]
    assert [item["targetRef"]["name"] for item in addresses] == ["tram-worker-0", "tram-worker-2"]
    assert [item["ip"] for item in addresses] == ["10.0.0.10", "10.0.0.12"]


def test_ensure_service_creates_then_updates() -> None:
    api = _FakeCoreV1Api()
    manager = KubernetesServiceManager(
        mode="standalone",
        node_id="tram-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    cfg = _pipeline()
    manager.ensure_service(cfg)
    assert len(api.created) == 1
    manager.ensure_service(cfg)
    assert len(api.patched) == 1


def test_ensure_service_creates_service_and_endpoints_for_named_workers() -> None:
    api = _FakeCoreV1Api()
    api.pods[("default", "tram-worker-0")] = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.10"))
    api.pods[("default", "tram-worker-2")] = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.12"))
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    cfg = _list_pipeline()
    manager.ensure_service(cfg)
    assert len(api.created) == 1
    assert len(api.endpoints_created) == 1
    manager.ensure_service(cfg)
    assert len(api.endpoints_patched) == 1


def test_ensure_service_patches_named_worker_endpoints_when_pod_disappears() -> None:
    api = _FakeCoreV1Api()
    api.pods[("default", "tram-worker-0")] = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.10"))
    api.pods[("default", "tram-worker-2")] = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.12"))
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    cfg = _list_pipeline()

    manager.ensure_service(cfg)
    del api.pods[("default", "tram-worker-0")]
    del api.pods[("default", "tram-worker-2")]

    manager.ensure_service(cfg)

    assert len(api.endpoints_patched) == 1
    _, _, body = api.endpoints_patched[0]
    assert body["subsets"] == []


def test_delete_service_ignores_missing_service() -> None:
    api = _FakeCoreV1Api()
    manager = KubernetesServiceManager(
        mode="standalone",
        node_id="tram-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    manager.delete_service(_pipeline())


def test_delete_service_removes_named_worker_endpoints() -> None:
    api = _FakeCoreV1Api()
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    cfg = _list_pipeline()
    name = manager.service_name_for_pipeline(cfg)
    api.services[("default", name)] = {"metadata": {"name": name}}
    api.endpoints[("default", name)] = {"metadata": {"name": name}}
    manager.delete_service(cfg)
    assert api.deleted == [(name, "default")]
    assert api.endpoints_deleted == [(name, "default")]


# ── UDP push source tests ─────────────────────────────────────────────────


def _syslog_pipeline() -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "name": "my-syslog",
            "schedule": {"type": "stream"},
            "source": {"type": "syslog", "host": "0.0.0.0", "port": 5514, "protocol": "udp"},
            "serializer_in": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "workers": {"count": "all"},
            "kubernetes": {"enabled": True, "service_type": "NodePort"},
        }
    )


def test_build_service_body_udp_derives_port_from_source() -> None:
    """Service port must come from source.port (5514), not the hardcoded default (514)."""
    manager = KubernetesServiceManager(
        mode="standalone",
        node_id="tram-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=_FakeCoreV1Api(),
    )
    body = manager._build_service_body(_syslog_pipeline())
    port_spec = body["spec"]["ports"][0]
    assert port_spec["protocol"] == "UDP"
    assert port_spec["name"] == "udp"
    assert port_spec["port"] == 5514   # from source.port, not hardcoded default
    assert port_spec["targetPort"] == 5514


def test_build_service_body_udp_fallback_default_port() -> None:
    """Without source.port attribute the hardcoded default is used."""
    cfg = PipelineConfig.model_validate(
        {
            "name": "snmp-trap",
            "schedule": {"type": "stream"},
            "source": {"type": "snmp_trap", "host": "0.0.0.0"},
            "serializer_in": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "workers": {"count": "all"},
            "kubernetes": {"enabled": True, "service_type": "NodePort"},
        }
    )
    manager = KubernetesServiceManager(
        mode="standalone",
        node_id="tram-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=_FakeCoreV1Api(),
    )
    body = manager._build_service_body(cfg)
    port_spec = body["spec"]["ports"][0]
    assert port_spec["protocol"] == "UDP"
    assert port_spec["port"] == 162   # snmp_trap default

def test_build_service_body_udp_kubernetes_port_overrides_source() -> None:
    """kubernetes.port explicitly set overrides source.port."""
    cfg = PipelineConfig.model_validate(
        {
            "name": "syslog-override",
            "schedule": {"type": "stream"},
            "source": {"type": "syslog", "host": "0.0.0.0", "port": 5514, "protocol": "udp"},
            "serializer_in": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "workers": {"count": "all"},
            "kubernetes": {"enabled": True, "service_type": "NodePort", "port": 30514, "target_port": 5514},
        }
    )
    manager = KubernetesServiceManager(
        mode="standalone",
        node_id="tram-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=_FakeCoreV1Api(),
    )
    body = manager._build_service_body(cfg)
    port_spec = body["spec"]["ports"][0]
    assert port_spec["port"] == 30514    # kubernetes.port wins
    assert port_spec["targetPort"] == 5514  # kubernetes.target_port wins
    assert port_spec["protocol"] == "UDP"


def test_build_service_body_annotations_merged() -> None:
    cfg = PipelineConfig.model_validate(
        {
            **{
                "name": "annotated-webhook",
                "schedule": {"type": "stream"},
                "source": {"type": "webhook", "path": "/ingest"},
                "serializer_in": {"type": "json"},
                "sinks": [{"type": "local", "path": "/tmp/out"}],
            },
            "kubernetes": {
                "enabled": True,
                "annotations": {"metallb.universe.tf/address-pool": "default"},
            },
        }
    )
    manager = KubernetesServiceManager(
        mode="standalone",
        node_id="tram-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=_FakeCoreV1Api(),
    )
    body = manager._build_service_body(cfg)
    assert body["metadata"]["annotations"]["metallb.universe.tf/address-pool"] == "default"
    assert "tram.trishul.io/webhook-path" in body["metadata"]["annotations"]


def test_is_eligible_for_udp_push_source() -> None:
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=_FakeCoreV1Api(),
    )
    assert manager.is_eligible(_syslog_pipeline()) is True


def test_ensure_service_with_dispatched_worker_ids_creates_endpoints() -> None:
    api = _FakeCoreV1Api()
    api.pods[("default", "tram-worker-0")] = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.10"))
    api.pods[("default", "tram-worker-1")] = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.11"))
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    # count:all pipeline — normally no manual endpoints, but controller provides dispatched IDs for count:N
    cfg = PipelineConfig.model_validate(
        {
            "name": "count-n-webhook",
            "schedule": {"type": "stream"},
            "source": {"type": "webhook", "path": "/ingest"},
            "serializer_in": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "workers": {"count": 2},
            "kubernetes": {"enabled": True, "service_type": "NodePort"},
        }
    )
    manager.ensure_service(cfg, dispatched_worker_ids=["tram-worker-0", "tram-worker-1"])
    assert len(api.created) == 1
    assert len(api.endpoints_created) == 1
    ep_addresses = api.endpoints_created[0][1]["subsets"][0]["addresses"]
    assert {a["targetRef"]["name"] for a in ep_addresses} == {"tram-worker-0", "tram-worker-1"}
    port_spec = api.endpoints_created[0][1]["subsets"][0]["ports"][0]
    assert port_spec["protocol"] == "TCP"


def test_delete_service_removes_count_n_endpoints() -> None:
    """count:N pipelines create manual Endpoints; delete_service must clean them up."""
    api = _FakeCoreV1Api()
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    cfg = PipelineConfig.model_validate(
        {
            "name": "count-n-webhook",
            "schedule": {"type": "stream"},
            "source": {"type": "webhook", "path": "/ingest"},
            "serializer_in": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "workers": {"count": 2},
            "kubernetes": {"enabled": True, "service_type": "NodePort"},
        }
    )
    name = manager.service_name_for_pipeline(cfg)
    api.services[("default", name)] = {"metadata": {"name": name}}
    api.endpoints[("default", name)] = {"metadata": {"name": name}}
    manager.delete_service(cfg)
    assert api.deleted == [(name, "default")]
    assert api.endpoints_deleted == [(name, "default")]


def test_ensure_service_udp_endpoints_protocol() -> None:
    api = _FakeCoreV1Api()
    api.pods[("default", "tram-worker-0")] = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.10"))
    manager = KubernetesServiceManager(
        mode="manager",
        node_id="tram-manager-0",
        standalone_port=8765,
        worker_ingress_port=8767,
        namespace="default",
        api=api,
    )
    cfg = PipelineConfig.model_validate(
        {
            "name": "syslog-count-n",
            "schedule": {"type": "stream"},
            "source": {"type": "syslog", "host": "0.0.0.0", "port": 5514, "protocol": "udp"},
            "serializer_in": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "workers": {"count": 1},
            "kubernetes": {"enabled": True, "service_type": "NodePort"},
        }
    )
    manager.ensure_service(cfg, dispatched_worker_ids=["tram-worker-0"])
    port_spec = api.endpoints_created[0][1]["subsets"][0]["ports"][0]
    assert port_spec["protocol"] == "UDP"
    assert port_spec["name"] == "udp"
