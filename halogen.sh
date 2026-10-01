#!/usr/bin/env bash
# halogen.sh — run latest halogen-flash-server, prune old images
set -euo pipefail

REGISTRY="ghcr.io"
REPO="peonist-ai/halogen-flash-server"
IMAGE="${REGISTRY}/${REPO}"
MODEL_DIR="/mnt/data/models/halogen-qwen3.8-flash-next"
CACHE_DIR="/mnt/data/halogen-cache"
PORT=8731

latest_tag() {
  local token
  token=$(curl -fsSL "https://${REGISTRY}/token?scope=repository:${REPO}:pull" | jq -r .token)
  curl -fsSL -H "Authorization: Bearer ${token}" \
      "https://${REGISTRY}/v2/${REPO}/tags/list?n=1000" \
    | jq -r '.tags[]' \
    | grep -E '^v?[0-9]+\.[0-9]+\.[0-9]+$' \
    | sort -V | tail -n1
}

local_latest_tag() {
  podman images --filter "reference=${IMAGE}" --format '{{.Tag}}' \
    | grep -E '^v?[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1
}

clean() {
  local keep="$1"
  podman images --filter "reference=${IMAGE}" --format '{{.Tag}}' \
    | grep -vxF "$keep" \
    | xargs -r -I{} podman rmi "${IMAGE}:{}" || true   # skips images still in use
  podman image prune -f >/dev/null                      # dangling layers
}

run() {
  local tag="$1"; shift

  sudo sysctl -q vm.compact_memory=1 2>/dev/null || true
  sleep 2

  podman pull -q "${IMAGE}:${tag}" >/dev/null
  exec podman run --rm -p "${PORT}:${PORT}" \
    --device /dev/kfd --device /dev/dri --group-add keep-groups \
    --ipc=host --ulimit memlock=-1:-1 \
    -v "${MODEL_DIR}:/models:ro" \
    "$@" \
    "${IMAGE}:${tag}"
}

# Shared agent-tuned settings; the pool is passed in so the vision variant can
# ask for less (the tower needs host RAM the pool would otherwise take).
optimal_env() {
  local pool="$1"
  printf '%s\n' \
    -e HALOGEN_CTX=262144 \
    -e "HALOGEN_KV_POOL_POSITIONS=${pool}" \
    -e HALOGEN_KV_SLOTS=4 \
    -e HALOGEN_MAX_TOK=16384 \
    -e HALOGEN_HOST_RESERVE_GIB=24 \
    -e HALOGEN_CACHE_BRANCHES=3 \
    -e HALOGEN_COMPOSABLE_CONTEXT=1 \
    -e HALOGEN_TEMPERATURE=1.0 \
    -e HALOGEN_MAX_TOKENS_DEFAULT=16384 \
    -e HALOGEN_MAX_TOKENS_CAP=32768 \
    -e HALOGEN_REASONING_EFFORT=xhigh \
    -e HALOGEN_MAX_THINKING_TOKENS=8192 \
    -e HALOGEN_ENGINE_WATCHDOG_S=0 \
    -e HALOGEN_CACHE_DIR=/cache \
    -e HALOGEN_CACHE_DISK_GIB=128 \
    -e HALOGEN_CACHE_PRUNE_OLD=1 \
    -e HALOGEN_KEEPALIVE_TIMEOUT=900 \
    -e HALOGEN_QUEUE_TIMEOUT=3600
}

run_optimal() {
  mkdir -p "${CACHE_DIR}"
  mapfile -t env_args < <(optimal_env 786432)
  run "$1" -v "${CACHE_DIR}:/cache" "${env_args[@]}"
}

run_optimal_vision() {
  mkdir -p "${CACHE_DIR}"
  mapfile -t env_args < <(optimal_env 524288)
  run "$1" -v "${CACHE_DIR}:/cache" "${env_args[@]}" \
    -e HALOGEN_VISION_TOWER=1 \
    -e HALOGEN_VISION_MAX_PIXELS=2073600
}

run_vision() {
  run "$1" \
    -e HALOGEN_VISION_TOWER=1
}

run_uncensored() {
  run "$1" \
    -e HALOGEN_CHECKPOINT=/models/qwen38-flash-next-uncensored-IQ4_XS.hgn \
    -e HALOGEN_TOKENIZER=/models/tokenizer \
    -e HALOGEN_MODEL_ID=qwen3.8-flash-uncensored \
    -e HALOGEN_VISION_TOWER=/models/qwen38-flash-next-vision.hgn
}

resolve() {
  if [[ -n "${VERSION:-}" ]]; then echo "$VERSION"; return; fi
  latest_tag 2>/dev/null || { echo "registry unreachable, using local image" >&2; local_latest_tag; }
}

case "${1:-run}" in
  run)
    tag=$(resolve); echo "using ${IMAGE}:${tag}" >&2; clean "$tag"
    run "$tag" ;;
  run-optimal)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal)" >&2; clean "$tag"
    run_optimal "$tag" ;;
  run-optimal-vision)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal + vision)" >&2; clean "$tag"
    run_optimal_vision "$tag" ;;
  run-vision)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (vision)" >&2; clean "$tag"
    run_vision "$tag" ;;
  run-uncensored)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (uncensored)" >&2; clean "$tag"
    run_uncensored "$tag" ;;
  clean)  tag=$(resolve); clean "$tag" ;;
  latest) resolve ;;
  *)      echo "usage: $0 [run|run-optimal|run-optimal-vision|run-vision|run-uncensored|clean|latest]   (VERSION=x.y.z to pin)" >&2; exit 1 ;;
esac
