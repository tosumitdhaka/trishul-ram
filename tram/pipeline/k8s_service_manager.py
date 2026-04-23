"""Kubernetes Service lifecycle helper for pipeline-owned push endpoints (HTTP and UDP)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tram.models.pipeline import PipelineConfig

logger = logging.getLogger(__name__)

HTTP_PUSH_SOURCES = {"webhook", "prometheus_rw"}
UDP_PUSH_SOURCES = {"syslog", "snmp_trap"}
ALL_PUSH_SOURCES = HTTP_PUSH_SOURCES | UDP_PUSH_SOURCES
UDP_PORT_DEFAULTS = {"syslog": 514, "snmp_trap": 162}
_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


class KubernetesServiceManager:
    """Create/update/delete dedicated Services for eligible active pipelines.

    The manager is intentionally tolerant:
    - missing kubernetes dependency → no-op with warning
    - no in-cluster / kubeconfig context → no-op with warning
    - worker mode or unsupported pipelines → no-op
    """

    def __init__(
        self,
        *,
        mode: str,
        node_id: str,
        standalone_port: int,
        worker_ingress_port: int,
        namespace: str | None = None,
        api: Any | None = None,
        pod_labels: dict[str, str] | None = None,
    ) -> None:
        self._mode = mode
        self._node_id = node_id or os.environ.get("HOSTNAME", "")
        self._standalone_port = standalone_port
        self._worker_ingress_port = worker_ingress_port
        self._namespace = namespace or self._detect_namespace()
        self._api = api
        self._pod_labels = dict(pod_labels or {}) or None
        self._disabled_reason: str | None = None

    @staticmethod
    def _detect_namespace() -> str:
        for env_name in ("TRAM_POD_NAMESPACE", "POD_NAMESPACE", "TRAM_WORKER_NAMESPACE"):
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
        try:
            return open(_NAMESPACE_FILE, encoding="utf-8").read().strip() or "default"
        except OSError:
            return "default"

    @staticmethod
    def _slugify_pipeline_name(name: str) -> str:
        slug = name.lower().replace("_", "-")
        slug = re.sub(r"[^a-z0-9-]+", "-", slug)
        slug = re.sub(r"-{2,}", "-", slug).strip("-")
        return slug or "pipeline"

    @classmethod
    def generate_service_name(cls, pipeline_name: str) -> str:
        slug = cls._slugify_pipeline_name(pipeline_name)
        hash_suffix = hashlib.sha1(pipeline_name.encode("utf-8")).hexdigest()[:8]
        prefix = "tram-p-"
        suffix = f"-{hash_suffix}"
        max_slug_len = 63 - len(prefix) - len(suffix)
        return f"{prefix}{slug[:max_slug_len].rstrip('-')}{suffix}"

    def service_name_for_pipeline(self, config: PipelineConfig) -> str:
        if config.kubernetes is not None and config.kubernetes.service_name:
            return config.kubernetes.service_name
        return self.generate_service_name(config.name)

    def is_eligible(self, config: PipelineConfig) -> bool:
        return (
            self._mode in {"standalone", "manager"}
            and config.kubernetes is not None
            and config.kubernetes.enabled
            and config.schedule.type == "stream"
            and config.source.type in ALL_PUSH_SOURCES
        )

    def ensure_service(
        self, config: PipelineConfig, dispatched_worker_ids: list[str] | None = None
    ) -> None:
        if not self.is_eligible(config):
            return
        api = self._get_api()
        if api is None:
            return
        body = self._build_service_body(config, dispatched_worker_ids=dispatched_worker_ids)
        name = body["metadata"]["name"]
        try:
            api.read_namespaced_service(name=name, namespace=self._namespace)
        except Exception as exc:  # noqa: BLE001
            if self._exception_status(exc) != 404:
                logger.warning(
                    "Pipeline service lookup failed",
                    extra={"pipeline": config.name, "service": name, "error": str(exc)},
                )
                return
            api.create_namespaced_service(namespace=self._namespace, body=body)
            logger.info(
                "Created pipeline Service",
                extra={"pipeline": config.name, "service": name, "namespace": self._namespace},
            )
        else:
            api.patch_namespaced_service(name=name, namespace=self._namespace, body=body)
            logger.info(
                "Updated pipeline Service",
                extra={"pipeline": config.name, "service": name, "namespace": self._namespace},
            )

        if self._uses_manual_endpoints(config, dispatched_worker_ids):
            self._ensure_endpoints(api, config, dispatched_worker_ids)

    def _had_manual_endpoints(self, config: PipelineConfig) -> bool:
        """True if this pipeline may have had manual Endpoints created (workers.list or count:N)."""
        if self._mode != "manager":
            return False
        if config.workers is None:
            return False
        if config.workers.worker_ids is not None:
            return True
        return isinstance(config.workers.count, int) and config.workers.count > 1

    def delete_service(self, config: PipelineConfig) -> None:
        if config.kubernetes is None:
            return
        api = self._get_api()
        if api is None:
            return
        name = self.service_name_for_pipeline(config)
        if self._had_manual_endpoints(config):
            self._delete_endpoints(api, name, config.name)
        try:
            api.delete_namespaced_service(name=name, namespace=self._namespace)
        except Exception as exc:  # noqa: BLE001
            if self._exception_status(exc) == 404:
                return
            logger.warning(
                "Pipeline service delete failed",
                extra={"pipeline": config.name, "service": name, "error": str(exc)},
            )
            return
        logger.info(
            "Deleted pipeline Service",
            extra={"pipeline": config.name, "service": name, "namespace": self._namespace},
        )

    def _get_api(self):
        if self._api is not None:
            return self._api
        if self._disabled_reason is not None:
            return None
        try:
            from kubernetes import client
            from kubernetes import config as k8s_config
        except Exception as exc:  # noqa: BLE001
            self._disabled_reason = str(exc)
            logger.warning("Kubernetes client unavailable — pipeline Service provisioning disabled: %s", exc)
            return None
        try:
            k8s_config.load_incluster_config()
        except Exception:  # noqa: BLE001
            try:
                k8s_config.load_kube_config()
            except Exception as exc:  # noqa: BLE001
                self._disabled_reason = str(exc)
                logger.warning("Kubernetes config unavailable — pipeline Service provisioning disabled: %s", exc)
                return None
        self._api = client.CoreV1Api()
        return self._api

    def _get_pod_labels(self) -> dict[str, str] | None:
        if self._pod_labels is not None:
            return self._pod_labels
        api = self._get_api()
        if api is None or not self._node_id:
            return None
        try:
            pod = api.read_namespaced_pod(name=self._node_id, namespace=self._namespace)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not read current pod labels for pipeline Service selector",
                extra={"pod": self._node_id, "namespace": self._namespace, "error": str(exc)},
            )
            return None
        self._pod_labels = dict(getattr(getattr(pod, "metadata", None), "labels", {}) or {})
        return self._pod_labels

    def _build_selector(self) -> dict[str, str] | None:
        if self._mode == "standalone":
            return {"statefulset.kubernetes.io/pod-name": self._node_id}
        if self._mode != "manager":
            return None
        labels = self._get_pod_labels()
        if labels is None:
            return None
        app_name = labels.get("app.kubernetes.io/name", "")
        instance = labels.get("app.kubernetes.io/instance", "")
        if not app_name or not instance:
            logger.warning("Current pod labels missing app identity — cannot derive worker selector")
            return None
        return {
            "app.kubernetes.io/name": app_name,
            "app.kubernetes.io/instance": instance,
            "app.kubernetes.io/component": "worker",
        }

    def _uses_manual_endpoints(
        self, config: PipelineConfig, dispatched_worker_ids: list[str] | None = None
    ) -> bool:
        if self._mode != "manager":
            return False
        if dispatched_worker_ids is not None:
            return True
        return config.workers is not None and config.workers.worker_ids is not None

    def _listed_worker_ids(
        self, config: PipelineConfig, dispatched_worker_ids: list[str] | None = None
    ) -> list[str]:
        if dispatched_worker_ids is not None:
            return list(dispatched_worker_ids)
        if config.workers is None or config.workers.worker_ids is None:
            return []
        return list(config.workers.worker_ids)

    def _resolve_protocol(self, source_type: str) -> str:
        return "UDP" if source_type in UDP_PUSH_SOURCES else "TCP"

    def _resolve_service_port(self, config: PipelineConfig) -> int:
        assert config.kubernetes is not None
        if config.kubernetes.port is not None:
            return config.kubernetes.port
        if config.source.type in UDP_PUSH_SOURCES:
            source_port = getattr(config.source, "port", None)
            if source_port is not None:
                return int(source_port)
            return UDP_PORT_DEFAULTS.get(config.source.type, 514)
        return self._worker_ingress_port if self._mode == "manager" else self._standalone_port

    def _resolve_target_port(self, config: PipelineConfig) -> int:
        assert config.kubernetes is not None
        if config.kubernetes.target_port is not None:
            return config.kubernetes.target_port
        if config.source.type in UDP_PUSH_SOURCES:
            source_port = getattr(config.source, "port", None)
            if source_port is not None:
                return int(source_port)
            return UDP_PORT_DEFAULTS.get(config.source.type, 514)
        return self._worker_ingress_port if self._mode == "manager" else self._standalone_port

    def _build_service_body(
        self, config: PipelineConfig, dispatched_worker_ids: list[str] | None = None
    ) -> dict[str, Any]:
        uses_manual = self._uses_manual_endpoints(config, dispatched_worker_ids)
        selector = None if uses_manual else self._build_selector()
        if selector is None and not uses_manual:
            raise RuntimeError(f"cannot derive selector for mode={self._mode}")
        assert config.kubernetes is not None
        service_name = self.service_name_for_pipeline(config)
        protocol = self._resolve_protocol(config.source.type)
        svc_port = self._resolve_service_port(config)
        target_port = self._resolve_target_port(config)
        source_path = getattr(config.source, "path", "")
        port_name = "udp" if protocol == "UDP" else "http"
        port_spec: dict[str, Any] = {
            "name": port_name,
            "port": svc_port,
            "protocol": protocol,
            "targetPort": target_port,
        }
        if config.kubernetes.service_type == "NodePort" and config.kubernetes.node_port is not None:
            port_spec["nodePort"] = config.kubernetes.node_port
        annotations: dict[str, str] = {}
        if protocol == "TCP":
            annotations["tram.trishul.io/webhook-path"] = str(source_path)
        annotations.update(config.kubernetes.annotations)
        body: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": self._namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "tram",
                    "tram.trishul.io/pipeline": config.name,
                    "tram.trishul.io/source-type": config.source.type,
                },
                "annotations": annotations,
            },
            "spec": {
                "type": config.kubernetes.service_type,
                "ports": [port_spec],
            },
        }
        if config.kubernetes.load_balancer_ip is not None:
            body["spec"]["loadBalancerIP"] = config.kubernetes.load_balancer_ip
        if selector is not None:
            body["spec"]["selector"] = selector
        return body

    def _build_endpoints_body(
        self, config: PipelineConfig, dispatched_worker_ids: list[str] | None = None
    ) -> dict[str, Any]:
        service_name = self.service_name_for_pipeline(config)
        protocol = self._resolve_protocol(config.source.type)
        ep_port = self._resolve_target_port(config)
        port_name = "udp" if protocol == "UDP" else "http"
        addresses: list[dict[str, Any]] = []
        for worker_id in self._listed_worker_ids(config, dispatched_worker_ids):
            pod = self._read_worker_pod(worker_id)
            if pod is None:
                continue
            pod_ip = str(getattr(getattr(pod, "status", None), "pod_ip", "") or "").strip()
            if not pod_ip:
                continue
            addresses.append({
                "ip": pod_ip,
                "targetRef": {
                    "kind": "Pod",
                    "namespace": self._namespace,
                    "name": worker_id,
                },
            })
        subsets: list[dict[str, Any]] = []
        if addresses:
            subsets.append({
                "addresses": addresses,
                "ports": [{
                    "name": port_name,
                    "port": ep_port,
                    "protocol": protocol,
                }],
            })
        return {
            "apiVersion": "v1",
            "kind": "Endpoints",
            "metadata": {
                "name": service_name,
                "namespace": self._namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "tram",
                    "tram.trishul.io/pipeline": config.name,
                    "tram.trishul.io/source-type": config.source.type,
                },
            },
            "subsets": subsets,
        }

    def _read_worker_pod(self, worker_id: str):
        api = self._get_api()
        if api is None:
            return None
        try:
            return api.read_namespaced_pod(name=worker_id, namespace=self._namespace)
        except Exception as exc:  # noqa: BLE001
            if self._exception_status(exc) == 404:
                return None
            logger.warning(
                "Could not read worker pod for pipeline Service endpoint",
                extra={"worker_id": worker_id, "namespace": self._namespace, "error": str(exc)},
            )
            return None

    def _ensure_endpoints(
        self, api, config: PipelineConfig, dispatched_worker_ids: list[str] | None = None
    ) -> None:
        body = self._build_endpoints_body(config, dispatched_worker_ids)
        name = body["metadata"]["name"]
        try:
            api.read_namespaced_endpoints(name=name, namespace=self._namespace)
        except Exception as exc:  # noqa: BLE001
            if self._exception_status(exc) != 404:
                logger.warning(
                    "Pipeline endpoints lookup failed",
                    extra={"pipeline": config.name, "service": name, "error": str(exc)},
                )
                return
            api.create_namespaced_endpoints(namespace=self._namespace, body=body)
            logger.info(
                "Created pipeline Endpoints",
                extra={"pipeline": config.name, "service": name, "namespace": self._namespace},
            )
            return
        api.patch_namespaced_endpoints(name=name, namespace=self._namespace, body=body)
        logger.info(
            "Updated pipeline Endpoints",
            extra={"pipeline": config.name, "service": name, "namespace": self._namespace},
        )

    def _delete_endpoints(self, api, name: str, pipeline_name: str) -> None:
        try:
            api.delete_namespaced_endpoints(name=name, namespace=self._namespace)
        except Exception as exc:  # noqa: BLE001
            if self._exception_status(exc) == 404:
                return
            logger.warning(
                "Pipeline endpoints delete failed",
                extra={"pipeline": pipeline_name, "service": name, "error": str(exc)},
            )
            return
        logger.info(
            "Deleted pipeline Endpoints",
            extra={"pipeline": pipeline_name, "service": name, "namespace": self._namespace},
        )

    @staticmethod
    def _exception_status(exc: Exception) -> int | None:
        return getattr(exc, "status", None)
