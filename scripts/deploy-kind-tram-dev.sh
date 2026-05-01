#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CLUSTER_NAME="${CLUSTER_NAME:-tram-dev}"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
RELEASE_NAME="${RELEASE_NAME:-trishul-ram}"
NAMESPACE="${NAMESPACE:-trishul-ram}"
MODE="${MODE:-manager}"

STANDALONE_IMAGE_REPOSITORY="${STANDALONE_IMAGE_REPOSITORY:-trishul-ram}"
MANAGER_IMAGE_REPOSITORY="${MANAGER_IMAGE_REPOSITORY:-trishul-ram-manager}"
WORKER_IMAGE_REPOSITORY="${WORKER_IMAGE_REPOSITORY:-trishul-ram-worker}"
IMAGE_TAG="${IMAGE_TAG:-local-$(date -u +%Y%m%d%H%M%S)}"

CHART_PATH="${CHART_PATH:-${ROOT_DIR}/helm}"
VALUES_FILE="${VALUES_FILE:-}"
KEEP_IMAGES="${KEEP_IMAGES:-5}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--mode MODE] [--tag TAG] [--release NAME] [--namespace NS] [--cluster NAME] [--keep-images N]

Build TRAM images, load them into a kind cluster, and upgrade the Helm release.

Modes:
  manager     Build manager + worker images; deploy with manager.enabled=true.
              Workers execute pipelines; manager owns scheduling and persistence.
              (default)
  standalone  Build standalone image only; deploy with manager.enabled=false.
              Single-node deployment — manager and worker in one container.

Environment overrides:
  MODE                          default: ${MODE}
  CLUSTER_NAME                  default: ${CLUSTER_NAME}
  KUBE_CONTEXT                  default: ${KUBE_CONTEXT}
  RELEASE_NAME                  default: ${RELEASE_NAME}
  NAMESPACE                     default: ${NAMESPACE}
  STANDALONE_IMAGE_REPOSITORY   default: ${STANDALONE_IMAGE_REPOSITORY}
  MANAGER_IMAGE_REPOSITORY      default: ${MANAGER_IMAGE_REPOSITORY}
  WORKER_IMAGE_REPOSITORY       default: ${WORKER_IMAGE_REPOSITORY}
  IMAGE_TAG                     default: generated UTC timestamp
  CHART_PATH                    default: ${CHART_PATH}
  VALUES_FILE                   optional extra values file to merge during upgrade
  KEEP_IMAGES                   keep newest N local \`local-*\` images per repository after build/load (default: ${KEEP_IMAGES}; 0 disables cleanup)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"
      shift 2
      ;;
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
    --keep-images)
      KEEP_IMAGES="$2"
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

if [[ "${MODE}" != "manager" && "${MODE}" != "standalone" ]]; then
  echo "Invalid mode: ${MODE}. Must be 'manager' or 'standalone'." >&2
  exit 2
fi

if ! [[ "${KEEP_IMAGES}" =~ ^[0-9]+$ ]]; then
  echo "--keep-images must be a non-negative integer." >&2
  exit 2
fi

STANDALONE_IMAGE="${STANDALONE_IMAGE_REPOSITORY}:${IMAGE_TAG}"
MANAGER_IMAGE="${MANAGER_IMAGE_REPOSITORY}:${IMAGE_TAG}"
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

run_cmd_nonfatal() {
  printf '+'
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
  if ! "$@"; then
    echo "Warning: command failed but cleanup will continue." >&2
  fi
}

cleanup_local_images() {
  local repository="$1"
  local keep_images="$2"
  local -a refs=()
  local -a records=()
  local -a sorted=()
  local ref created

  if (( keep_images == 0 )); then
    return 0
  fi

  mapfile -t refs < <(docker image ls --format '{{.Repository}}:{{.Tag}}' --filter "reference=${repository}:local-*")
  if (( ${#refs[@]} <= keep_images )); then
    return 0
  fi

  for ref in "${refs[@]}"; do
    [[ -n "${ref}" ]] || continue
    created="$(docker image inspect --format '{{.Created}}' "${ref}" 2>/dev/null || true)"
    [[ -n "${created}" ]] || continue
    records+=("${created} ${ref}")
  done

  if (( ${#records[@]} <= keep_images )); then
    return 0
  fi

  mapfile -t sorted < <(printf '%s\n' "${records[@]}" | sort -r)
  echo "Pruning older local images for ${repository} (keeping ${keep_images})."
  for ((i=keep_images; i<${#sorted[@]}; i++)); do
    ref="${sorted[$i]#* }"
    run_cmd_nonfatal docker image rm "${ref}"
  done
}

DEPLOY_START=$(date +%s)

echo "Mode:              ${MODE}"
echo "Using kube context: ${KUBE_CONTEXT}"
kubectl config use-context "${KUBE_CONTEXT}" >/dev/null

if [[ "${MODE}" == "standalone" ]]; then
  echo "Building standalone image: ${STANDALONE_IMAGE}"
  docker build -t "${STANDALONE_IMAGE}" -f "${ROOT_DIR}/Dockerfile" "${ROOT_DIR}"

  echo "Loading image into kind cluster: ${CLUSTER_NAME}"
  kind load docker-image --name "${CLUSTER_NAME}" "${STANDALONE_IMAGE}"
  cleanup_local_images "${STANDALONE_IMAGE_REPOSITORY}" "${KEEP_IMAGES}"
else
  echo "Building manager image: ${MANAGER_IMAGE}"
  docker build -t "${MANAGER_IMAGE}" -f "${ROOT_DIR}/Dockerfile.manager" "${ROOT_DIR}"

  echo "Building worker image: ${WORKER_IMAGE}"
  docker build -t "${WORKER_IMAGE}" -f "${ROOT_DIR}/Dockerfile.worker" "${ROOT_DIR}"

  echo "Loading images into kind cluster: ${CLUSTER_NAME}"
  kind load docker-image --name "${CLUSTER_NAME}" "${MANAGER_IMAGE}"
  kind load docker-image --name "${CLUSTER_NAME}" "${WORKER_IMAGE}"
  cleanup_local_images "${MANAGER_IMAGE_REPOSITORY}" "${KEEP_IMAGES}"
  cleanup_local_images "${WORKER_IMAGE_REPOSITORY}" "${KEEP_IMAGES}"
fi

echo "Upgrading Helm release: ${RELEASE_NAME}"
if [[ "${MODE}" == "standalone" ]]; then
  HELM_ARGS=(
    upgrade --install "${RELEASE_NAME}" "${CHART_PATH}"
    --namespace "${NAMESPACE}"
    --create-namespace
    --reuse-values
    --set manager.enabled=false
    --set image.repository="${STANDALONE_IMAGE_REPOSITORY}"
    --set image.tag="${IMAGE_TAG}"
  )
else
  HELM_ARGS=(
    upgrade --install "${RELEASE_NAME}" "${CHART_PATH}"
    --namespace "${NAMESPACE}"
    --create-namespace
    --reuse-values
    --set manager.enabled=true
    --set manager.image.repository="${MANAGER_IMAGE_REPOSITORY}"
    --set manager.image.tag="${IMAGE_TAG}"
    --set worker.image.repository="${WORKER_IMAGE_REPOSITORY}"
    --set worker.image.tag="${IMAGE_TAG}"
  )
fi

if [[ -n "${VALUES_FILE}" ]]; then
  HELM_ARGS+=(--values "${VALUES_FILE}")
fi

helm "${HELM_ARGS[@]}"

echo "Waiting for rollout in namespace: ${NAMESPACE}"
if [[ "${MODE}" == "standalone" ]]; then
  if kubectl get "statefulset/${RELEASE_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    kubectl rollout status "statefulset/${RELEASE_NAME}" -n "${NAMESPACE}" --timeout=5m
  fi
else
  if kubectl get "statefulset/${RELEASE_NAME}-manager" -n "${NAMESPACE}" >/dev/null 2>&1; then
    kubectl rollout status "statefulset/${RELEASE_NAME}-manager" -n "${NAMESPACE}" --timeout=5m
  elif kubectl get "deployment/${RELEASE_NAME}-manager" -n "${NAMESPACE}" >/dev/null 2>&1; then
    kubectl rollout status "deployment/${RELEASE_NAME}-manager" -n "${NAMESPACE}" --timeout=5m
  fi

  if kubectl get "statefulset/${RELEASE_NAME}-worker" -n "${NAMESPACE}" >/dev/null 2>&1; then
    kubectl rollout status "statefulset/${RELEASE_NAME}-worker" -n "${NAMESPACE}" --timeout=5m
  fi
fi

DEPLOY_END=$(date +%s)
DEPLOY_SECS=$((DEPLOY_END - DEPLOY_START))
DEPLOY_TIME=$(printf "%dm%02ds" $((DEPLOY_SECS / 60)) $((DEPLOY_SECS % 60)))

echo
echo "Deployment complete."
echo "Time:    ${DEPLOY_TIME}"
echo "Mode:    ${MODE}"
if [[ "${MODE}" == "standalone" ]]; then
  echo "Image:   ${STANDALONE_IMAGE}"
else
  echo "Manager: ${MANAGER_IMAGE}"
  echo "Worker:  ${WORKER_IMAGE}"
fi
echo "Release:   ${RELEASE_NAME}"
echo "Namespace: ${NAMESPACE}"
