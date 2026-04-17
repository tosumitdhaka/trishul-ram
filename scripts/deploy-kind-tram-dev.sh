#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CLUSTER_NAME="${CLUSTER_NAME:-tram-dev}"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
RELEASE_NAME="${RELEASE_NAME:-trishul-ram}"
NAMESPACE="${NAMESPACE:-trishul-ram}"

IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-trishul-ram}"
WORKER_IMAGE_REPOSITORY="${WORKER_IMAGE_REPOSITORY:-trishul-ram-worker}"
IMAGE_TAG="${IMAGE_TAG:-local-$(date -u +%Y%m%d%H%M%S)}"

CHART_PATH="${CHART_PATH:-${ROOT_DIR}/helm}"
VALUES_FILE="${VALUES_FILE:-}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--tag TAG] [--release NAME] [--namespace NS] [--cluster NAME]

Build both TRAM images locally, load them into a kind cluster, and upgrade the
existing Helm release using --reuse-values.

Environment overrides:
  CLUSTER_NAME             default: ${CLUSTER_NAME}
  KUBE_CONTEXT             default: ${KUBE_CONTEXT}
  RELEASE_NAME             default: ${RELEASE_NAME}
  NAMESPACE                default: ${NAMESPACE}
  IMAGE_REPOSITORY         default: ${IMAGE_REPOSITORY}
  WORKER_IMAGE_REPOSITORY  default: ${WORKER_IMAGE_REPOSITORY}
  IMAGE_TAG                default: generated UTC timestamp
  CHART_PATH               default: ${CHART_PATH}
  VALUES_FILE              optional extra values file to merge during upgrade
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      IMAGE_TAG="$2"
      shift 2
      ;;
    --release)
      RELEASE_NAME="$2"
      shift 2
      ;;
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --cluster)
      CLUSTER_NAME="$2"
      KUBE_CONTEXT="kind-${CLUSTER_NAME}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

MAIN_IMAGE="${IMAGE_REPOSITORY}:${IMAGE_TAG}"
WORKER_IMAGE="${WORKER_IMAGE_REPOSITORY}:${IMAGE_TAG}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

require_cmd docker
require_cmd kind
require_cmd kubectl
require_cmd helm

echo "Using kube context: ${KUBE_CONTEXT}"
kubectl config use-context "${KUBE_CONTEXT}" >/dev/null

echo "Building manager image: ${MAIN_IMAGE}"
docker build -t "${MAIN_IMAGE}" -f "${ROOT_DIR}/Dockerfile" "${ROOT_DIR}"

echo "Building worker image: ${WORKER_IMAGE}"
docker build -t "${WORKER_IMAGE}" -f "${ROOT_DIR}/Dockerfile.worker" "${ROOT_DIR}"

echo "Loading images into kind cluster: ${CLUSTER_NAME}"
kind load docker-image --name "${CLUSTER_NAME}" "${MAIN_IMAGE}"
kind load docker-image --name "${CLUSTER_NAME}" "${WORKER_IMAGE}"

echo "Upgrading Helm release: ${RELEASE_NAME}"
HELM_ARGS=(
  upgrade --install "${RELEASE_NAME}" "${CHART_PATH}"
  --namespace "${NAMESPACE}"
  --create-namespace
  --reuse-values
  --set image.repository="${IMAGE_REPOSITORY}"
  --set image.tag="${IMAGE_TAG}"
  --set worker.image.repository="${WORKER_IMAGE_REPOSITORY}"
  --set worker.image.tag="${IMAGE_TAG}"
)

if [[ -n "${VALUES_FILE}" ]]; then
  HELM_ARGS+=(--values "${VALUES_FILE}")
fi

helm "${HELM_ARGS[@]}"

echo "Waiting for rollout in namespace: ${NAMESPACE}"
if kubectl get "statefulset/${RELEASE_NAME}-manager" -n "${NAMESPACE}" >/dev/null 2>&1; then
  kubectl rollout status "statefulset/${RELEASE_NAME}-manager" -n "${NAMESPACE}" --timeout=5m
elif kubectl get "deployment/${RELEASE_NAME}-manager" -n "${NAMESPACE}" >/dev/null 2>&1; then
  kubectl rollout status "deployment/${RELEASE_NAME}-manager" -n "${NAMESPACE}" --timeout=5m
fi

if kubectl get "statefulset/${RELEASE_NAME}-worker" -n "${NAMESPACE}" >/dev/null 2>&1; then
  kubectl rollout status "statefulset/${RELEASE_NAME}-worker" -n "${NAMESPACE}" --timeout=5m
elif kubectl get "statefulset/${RELEASE_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
  kubectl rollout status "statefulset/${RELEASE_NAME}" -n "${NAMESPACE}" --timeout=5m
fi

echo
echo "Deployment complete."
echo "Manager image: ${MAIN_IMAGE}"
echo "Worker image:  ${WORKER_IMAGE}"
echo "Release:       ${RELEASE_NAME}"
echo "Namespace:     ${NAMESPACE}"
