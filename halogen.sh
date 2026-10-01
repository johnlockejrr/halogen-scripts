#!/usr/bin/env bash
# halogen.sh — run latest halogen-flash-server, prune old images
set -euo pipefail

REGISTRY="ghcr.io"
REPO="peonist-ai/halogen-flash-server"
IMAGE="${REGISTRY}/${REPO}"
MODEL_DIR="/mnt/data/models/halogen-qwen3.8-flash-next"
CACHE_DIR="/mnt/data/halogen-cache"
PORT=8731

compact_memory() {
  local before after
  before=$(awk '/Normal/{print $14; exit}' /proc/buddyinfo)
  sudo sysctl -q vm.compact_memory=1 2>/dev/null || {
    echo "compaction skipped (no sudo); order-9 blocks: ${before:-?}" >&2
    return 0
  }
  sleep 3
  after=$(awk '/Normal/{print $14; exit}' /proc/buddyinfo)
  echo "order-9 2MiB blocks: ${before:-?} -> ${after:-?}" >&2
}

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

  compact_memory

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
    -e HALOGEN_MTP_PREFILL=0 \
    -e HALOGEN_WEIGHTS_LOCK=1 \
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

run_swift_abliterated() {
  mkdir -p "${CACHE_DIR}-abl"
  mapfile -t env_args < <(optimal_env 524288)
  run "$1" -v "${CACHE_DIR}-abl:/cache" "${env_args[@]}" \
    -e HALOGEN_CHECKPOINT=/models/qwen38-flash-next-v2-swift15-abliterated.hgn \
    -e HALOGEN_NGRAM_TABLE=/models/qwen38-flash-next-ngram.hgn \
    -e HALOGEN_MODEL_ID=halogen-qwen3.8-flash-next \
    -e HALOGEN_SPEC_ADAPT=0 \
    -e HALOGEN_TOP_P=0.95 \
    -e HALOGEN_TOP_K=20 \
    -e HALOGEN_VISION_TOWER=1 \
    -e HALOGEN_VISION_MAX_PIXELS=2073600
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
  run-swift-abliterated)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (swift abliterated)" >&2; clean "$tag"
    run_swift_abliterated "$tag" ;;
  clean)  tag=$(resolve); clean "$tag" ;;
  latest) resolve ;;
  *)      echo "usage: $0 [run|run-optimal|run-optimal-vision|run-swift-abliterated|clean|latest]   (VERSION=x.y.z to pin)" >&2; exit 1 ;;
esac
