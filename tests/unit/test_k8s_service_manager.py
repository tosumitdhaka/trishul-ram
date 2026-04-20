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
