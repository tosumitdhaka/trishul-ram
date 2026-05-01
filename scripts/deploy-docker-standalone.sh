#!/usr/bin/env bash
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
if [[ -n "${SCRIPT_SOURCE}" && -f "${SCRIPT_SOURCE}" ]]; then
  ROOT_DIR="$(cd "$(dirname "${SCRIPT_SOURCE}")/.." && pwd)"
  SCRIPT_NAME="$(basename "${SCRIPT_SOURCE}")"
else
  ROOT_DIR="$(pwd)"
  SCRIPT_NAME="deploy-docker-standalone.sh"
fi

COMMAND="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

DEFAULT_LOCAL_TAG="local-$(date +%s)"
CONTAINER_NAME="${CONTAINER_NAME:-trishul-ram}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-trishul-ram}"
IMAGE_TAG="${IMAGE_TAG:-${DEFAULT_LOCAL_TAG}}"
HOST_PORT="${HOST_PORT:-8765}"
CONTAINER_PORT="${CONTAINER_PORT:-8765}"
PIPELINES_DIR="${PIPELINES_DIR:-${ROOT_DIR}/pipelines}"
DATA_VOLUME="${DATA_VOLUME:-}"
DATA_DIR="${DATA_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
ENV_FILE="${ENV_FILE:-}"
RESTART_POLICY="${RESTART_POLICY:-unless-stopped}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-60}"
LOG_TAIL="${LOG_TAIL:-200}"
KEEP_IMAGES="${KEEP_IMAGES:-5}"

BUILD_FIRST=false
DRY_RUN=false
FOLLOW_LOGS=true
PULL_IMAGE=false
REMOVE_VOLUME=false
TAG_EXPLICIT=false
USE_GHCR=false
GHCR_IMAGE_REPOSITORY="ghcr.io/tosumitdhaka/trishul-ram"
UDP_PORTS=()
EXTRA_ENV_VARS=()

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} <command> [options]

Build, deploy, and manage a standalone TRAM container with persisted local state.

Commands:
  build      Build the standalone Docker image from ./Dockerfile
  up         Create or recreate the standalone container and start it
  start      Start an existing container
  stop       Stop a running container
  restart    Restart the container (or recreate it when --build is set)
  down       Stop and remove the container
  logs       Show container logs
  status     Show container status, ports, and key bind mounts
  shell      Open an interactive shell in the running container
  help       Show this help text

Options:
  --build                    Build the image before \`up\` or \`restart\` (already default in local mode)
  --pull                     Pull the configured image before \`up\` or \`restart\`
  --image-repository REPO    Image repository to use (default: ${IMAGE_REPOSITORY})
  --tag TAG                  Image tag to use (default local: auto-generated \`local-<epoch>\`; with --ghcr: latest)
  --ghcr                     Use ${GHCR_IMAGE_REPOSITORY} and pull it before deploy
  --container-name NAME      Docker container name (default: ${CONTAINER_NAME})
  --host-port PORT           Host TCP port published for the UI/API (default: ${HOST_PORT})
  --container-port PORT      Container TCP port for TRAM (default: ${CONTAINER_PORT})
  --pipelines-dir DIR        Host directory mounted to /pipelines (default: ${PIPELINES_DIR})
  --data-volume NAME         Docker volume mounted to /data (default: <container>-data)
  --data-dir DIR             Host directory mounted to /data instead of a Docker volume
  --output-dir DIR           Optional host directory mounted to /data/output (default: inside /data)
  --env-file FILE            Optional docker --env-file for additional TRAM settings
  --env KEY=VALUE            Extra docker -e entry (repeatable)
  --udp-port HOST[:CTR]      Publish a UDP port for trap/syslog ingress (repeatable)
  --keep-images N            Keep the newest N local \`local-*\` images (default: ${KEEP_IMAGES}; 0 disables cleanup)
  --tail N                   Log lines to show for \`logs\` (default: ${LOG_TAIL})
  --no-follow                Do not follow logs for \`logs\`
  --remove-volume            With \`down\`, also delete the named Docker data volume
  --dry-run                  Print commands without executing them
  --help, -h                 Show this help text

Defaults:
  Image:        local mode auto-builds ${IMAGE_REPOSITORY}:${DEFAULT_LOCAL_TAG}; --ghcr uses latest unless tagged
  TCP port:     ${HOST_PORT}:${CONTAINER_PORT}
  Pipelines:    ${PIPELINES_DIR} -> /pipelines (read-only)
  Data:         <default volume> -> /data
  Output:       /data/output inside /data (or bind-mounted with --output-dir)

Examples:
  ${SCRIPT_NAME} up
  ${SCRIPT_NAME} up --tag local-test
  ${SCRIPT_NAME} up --ghcr
  ${SCRIPT_NAME} up --ghcr --tag 1.3.3
  ${SCRIPT_NAME} up --keep-images 10
  ${SCRIPT_NAME} up --output-dir ./output
  ${SCRIPT_NAME} up --udp-port 1162 --udp-port 1514:1514
  ${SCRIPT_NAME} build
  ${SCRIPT_NAME} logs --tail 100
  ${SCRIPT_NAME} status
  ${SCRIPT_NAME} down

Notes:
  - \`up\` recreates the container so config and image changes take effect cleanly.
  - Runtime state persists in /data; by default the script creates and reuses a named Docker volume.
  - Local mode auto-builds a fresh image on each \`up\`/\`restart\` using a timestamp tag such as
    \`local-1714588800\` unless you pass \`--tag\`.
  - After local builds, the script prunes older \`local-*\` images for the same repository and keeps the
    newest ${KEEP_IMAGES} by default; pass \`--keep-images\` to override or \`0\` to disable cleanup.
  - \`--ghcr\` is a convenience wrapper for the published GHCR standalone image and defaults the tag to
    \`latest\` unless you pass \`--tag\`.
  - The host pipelines directory is created automatically when missing so \`curl | bash\` can bootstrap
    from the current working directory without extra setup.
  - Advanced \`--env\` overrides can replace the script's default TRAM_* values, so keep them
    consistent with the mounted directories and published ports.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)
      BUILD_FIRST=true
      shift
      ;;
    --pull)
      PULL_IMAGE=true
      shift
      ;;
    --image-repository)
      IMAGE_REPOSITORY="$2"
      shift 2
      ;;
    --tag)
      IMAGE_TAG="$2"
      TAG_EXPLICIT=true
      shift 2
      ;;
    --ghcr)
      USE_GHCR=true
      PULL_IMAGE=true
      shift
      ;;
    --container-name)
      CONTAINER_NAME="$2"
      shift 2
      ;;
    --host-port)
      HOST_PORT="$2"
      shift 2
      ;;
    --container-port)
      CONTAINER_PORT="$2"
      shift 2
      ;;
    --pipelines-dir)
      PIPELINES_DIR="$2"
      shift 2
      ;;
    --data-volume)
      DATA_VOLUME="$2"
      shift 2
      ;;
    --data-dir)
      DATA_DIR="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --env)
      EXTRA_ENV_VARS+=("$2")
      shift 2
      ;;
    --udp-port)
      UDP_PORTS+=("$2")
      shift 2
      ;;
    --keep-images)
      KEEP_IMAGES="$2"
      shift 2
      ;;
    --tail)
      LOG_TAIL="$2"
      shift 2
      ;;
    --no-follow)
      FOLLOW_LOGS=false
      shift
      ;;
    --remove-volume)
      REMOVE_VOLUME=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${USE_GHCR}" == "true" ]]; then
  IMAGE_REPOSITORY="${GHCR_IMAGE_REPOSITORY}"
  if [[ "${TAG_EXPLICIT}" == "false" ]]; then
    IMAGE_TAG="latest"
  fi
elif [[ "${PULL_IMAGE}" == "false" ]]; then
  BUILD_FIRST=true
fi

IMAGE="${IMAGE_REPOSITORY}:${IMAGE_TAG}"

if [[ -n "${DATA_VOLUME}" && -n "${DATA_DIR}" ]]; then
  echo "Use either --data-volume or --data-dir, not both." >&2
  exit 2
fi

if ! [[ "${KEEP_IMAGES}" =~ ^[0-9]+$ ]]; then
  echo "--keep-images must be a non-negative integer." >&2
  exit 2
fi

if [[ "${BUILD_FIRST}" == "true" && "${PULL_IMAGE}" == "true" ]]; then
  echo "Use either --build or --pull/--ghcr, not both." >&2
  exit 2
fi

if [[ -z "${DATA_VOLUME}" && -z "${DATA_DIR}" ]]; then
  DATA_VOLUME="${CONTAINER_NAME}-data"
fi

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

run_cmd() {
  printf '+'
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
  if [[ "${DRY_RUN}" == "false" ]]; then
    "$@"
  fi
}

run_cmd_nonfatal() {
  printf '+'
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
  if [[ "${DRY_RUN}" == "false" ]]; then
    if ! "$@"; then
      echo "Warning: command failed but cleanup will continue." >&2
    fi
  fi
}

cleanup_local_images() {
  local repository="$1"
  local keep_images="$2"
  local -a refs=()
  local -a records=()
  local -a sorted=()
  local ref created

  if [[ "${DRY_RUN}" == "true" ]]; then
    return 0
  fi

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

container_exists() {
  docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1
}

container_running() {
  [[ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null || true)" == "true" ]]
}

ensure_bind_mounts() {
  if [[ -z "${PIPELINES_DIR}" ]]; then
    echo "Pipelines directory cannot be empty." >&2
    exit 1
  fi
  mkdir -p "${PIPELINES_DIR}"
  if [[ -n "${ENV_FILE}" && ! -f "${ENV_FILE}" ]]; then
    echo "Env file does not exist: ${ENV_FILE}" >&2
    exit 1
  fi

  if [[ -n "${DATA_DIR}" ]]; then
    mkdir -p "${DATA_DIR}" \
             "${DATA_DIR}/mibs" \
             "${DATA_DIR}/mib-sources" \
             "${DATA_DIR}/schemas"
  fi
  if [[ -n "${OUTPUT_DIR}" ]]; then
    mkdir -p "${OUTPUT_DIR}"
  fi
}

ensure_data_volume() {
  if [[ -n "${DATA_VOLUME}" ]]; then
    run_cmd docker volume create "${DATA_VOLUME}"
  fi
}

wait_for_health() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    return 0
  fi

  local deadline status
  deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))
  echo "Waiting for ${CONTAINER_NAME} to become healthy..."

  while (( SECONDS < deadline )); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{if .State.Running}}running{{else}}{{.State.Status}}{{end}}{{end}}' "${CONTAINER_NAME}" 2>/dev/null || echo missing)"
    case "${status}" in
      healthy|running)
        echo "Container is ${status}."
        return 0
        ;;
      unhealthy|exited|dead)
        echo "Container entered state: ${status}" >&2
        docker logs --tail 50 "${CONTAINER_NAME}" || true
        return 1
        ;;
    esac
    sleep 2
  done

  echo "Timed out waiting for ${CONTAINER_NAME} to become healthy." >&2
  docker logs --tail 50 "${CONTAINER_NAME}" || true
  return 1
}

build_image() {
  run_cmd docker build -t "${IMAGE}" -f "${ROOT_DIR}/Dockerfile" "${ROOT_DIR}"
}

pull_image() {
  run_cmd docker pull "${IMAGE}"
}

show_summary() {
  echo
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "Dry run complete."
  else
    echo "Standalone container ready."
  fi
  echo "Container: ${CONTAINER_NAME}"
  echo "Image:     ${IMAGE}"
  echo "UI/API:    http://localhost:${HOST_PORT}"
  echo "Pipelines: ${PIPELINES_DIR}"
  if [[ -n "${DATA_VOLUME}" ]]; then
    echo "Data:      volume ${DATA_VOLUME}"
  else
    echo "Data:      ${DATA_DIR}"
  fi
  if [[ -n "${OUTPUT_DIR}" ]]; then
    echo "Output:    ${OUTPUT_DIR}"
  else
    echo "Output:    /data/output inside data storage"
  fi
}

cmd_up() {
  ensure_bind_mounts
  ensure_data_volume

  if [[ "${BUILD_FIRST}" == "true" ]]; then
    build_image
  elif [[ "${PULL_IMAGE}" == "true" ]]; then
    pull_image
  fi

  if container_exists; then
    echo "Recreating container: ${CONTAINER_NAME}"
    run_cmd docker rm -f "${CONTAINER_NAME}"
  fi

  local -a args=(
    docker run -d
    --name "${CONTAINER_NAME}"
    --restart "${RESTART_POLICY}"
    --add-host "host.docker.internal:host-gateway"
  )

  if [[ -n "${ENV_FILE}" ]]; then
    args+=(--env-file "${ENV_FILE}")
  fi

  args+=(
    -p "${HOST_PORT}:${CONTAINER_PORT}"
    -v "${PIPELINES_DIR}:/pipelines:ro"
    -e "TRAM_MODE=standalone"
    -e "TRAM_HOST=0.0.0.0"
    -e "TRAM_PORT=${CONTAINER_PORT}"
    -e "TRAM_NODE_ID=${CONTAINER_NAME}"
    -e "TRAM_PIPELINE_DIR=/pipelines"
    -e "TRAM_DB_URL=sqlite:////data/tram.db"
    -e "TRAM_MIB_DIR=/data/mibs"
    -e "TRAM_MIB_SOURCE_DIR=/data/mib-sources"
    -e "TRAM_MIB_BUNDLED_SOURCE_DIR=/mib-sources:/data/mib-sources"
    -e "TRAM_SCHEMA_DIR=/data/schemas"
  )

  if [[ -n "${DATA_VOLUME}" ]]; then
    args+=(-v "${DATA_VOLUME}:/data")
  else
    args+=(-v "${DATA_DIR}:/data")
  fi

  if [[ -n "${OUTPUT_DIR}" ]]; then
    args+=(-v "${OUTPUT_DIR}:/data/output")
  fi

  local udp_spec host_port container_port
  for udp_spec in "${UDP_PORTS[@]}"; do
    if [[ "${udp_spec}" == *:* ]]; then
      host_port="${udp_spec%%:*}"
      container_port="${udp_spec##*:}"
    else
      host_port="${udp_spec}"
      container_port="${udp_spec}"
    fi
    args+=(-p "${host_port}:${container_port}/udp")
  done

  local env_kv
  for env_kv in "${EXTRA_ENV_VARS[@]}"; do
    args+=(-e "${env_kv}")
  done

  args+=("${IMAGE}" daemon)

  run_cmd "${args[@]}"
  wait_for_health
  show_summary
  if [[ "${USE_GHCR}" == "false" ]]; then
    cleanup_local_images "${IMAGE_REPOSITORY}" "${KEEP_IMAGES}"
  fi
}

cmd_start() {
  if ! container_exists; then
    echo "Container does not exist: ${CONTAINER_NAME}" >&2
    exit 1
  fi
  run_cmd docker start "${CONTAINER_NAME}"
  wait_for_health
}

cmd_stop() {
  if ! container_exists; then
    echo "Container does not exist: ${CONTAINER_NAME}"
    return 0
  fi
  run_cmd docker stop "${CONTAINER_NAME}"
}

cmd_restart() {
  if [[ "${BUILD_FIRST}" == "true" || "${PULL_IMAGE}" == "true" ]]; then
    cmd_up
    return 0
  fi
  if ! container_exists; then
    echo "Container does not exist yet; creating it instead."
    cmd_up
    return 0
  fi
  run_cmd docker restart "${CONTAINER_NAME}"
  wait_for_health
}

cmd_down() {
  if ! container_exists; then
    echo "Container does not exist: ${CONTAINER_NAME}"
  else
    run_cmd docker rm -f "${CONTAINER_NAME}"
  fi

  if [[ "${REMOVE_VOLUME}" == "true" && -n "${DATA_VOLUME}" ]]; then
    run_cmd docker volume rm "${DATA_VOLUME}"
  fi
}

cmd_logs() {
  if ! container_exists; then
    echo "Container does not exist: ${CONTAINER_NAME}" >&2
    exit 1
  fi

  local -a args=(docker logs --tail "${LOG_TAIL}")
  if [[ "${FOLLOW_LOGS}" == "true" ]]; then
    args+=(-f)
  fi
  args+=("${CONTAINER_NAME}")
  run_cmd "${args[@]}"
}

cmd_status() {
  if ! container_exists; then
    echo "Container does not exist: ${CONTAINER_NAME}" >&2
    exit 1
  fi

  local state health image
  state="$(docker inspect --format '{{.State.Status}}' "${CONTAINER_NAME}")"
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "${CONTAINER_NAME}")"
  image="$(docker inspect --format '{{.Config.Image}}' "${CONTAINER_NAME}")"

  echo "Container: ${CONTAINER_NAME}"
  echo "State:     ${state}"
  echo "Health:    ${health}"
  echo "Image:     ${image}"
  echo "UI/API:    http://localhost:${HOST_PORT}"
  echo "Pipelines: ${PIPELINES_DIR}"
  if [[ -n "${DATA_VOLUME}" ]]; then
    echo "Data:      volume ${DATA_VOLUME}"
  else
    echo "Data:      ${DATA_DIR}"
  fi
  if [[ -n "${OUTPUT_DIR}" ]]; then
    echo "Output:    ${OUTPUT_DIR}"
  else
    echo "Output:    /data/output inside data storage"
  fi
  echo "Ports:"
  docker port "${CONTAINER_NAME}" || true
}

cmd_shell() {
  if ! container_running; then
    echo "Container is not running: ${CONTAINER_NAME}" >&2
    exit 1
  fi
  if [[ "${DRY_RUN}" == "true" ]]; then
    run_cmd docker exec -it "${CONTAINER_NAME}" /bin/bash
  else
    docker exec -it "${CONTAINER_NAME}" /bin/bash
  fi
}

if [[ "${COMMAND}" != "help" ]]; then
  require_cmd docker
fi

case "${COMMAND}" in
  build)
    build_image
    if [[ "${USE_GHCR}" == "false" ]]; then
      cleanup_local_images "${IMAGE_REPOSITORY}" "${KEEP_IMAGES}"
    fi
    ;;
  up)
    cmd_up
    ;;
  start)
    cmd_start
    ;;
  stop)
    cmd_stop
    ;;
  restart)
    cmd_restart
    ;;
  down)
    cmd_down
    ;;
  logs)
    cmd_logs
    ;;
  status)
    cmd_status
    ;;
  shell)
    cmd_shell
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: ${COMMAND}" >&2
    usage >&2
    exit 2
    ;;
esac
