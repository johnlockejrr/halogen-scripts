#!/usr/bin/env bash
# halogen.sh — run latest halogen-flash-server, prune old images
# To allow this specific command to run without a password while
# keeping password prompts for all other sudo commands, you need
# to add a highly specific rule to your sudoers configuration.
# ```bash
# sudo visudo -f /etc/sudoers.d/compact_memory
# ```
# your_username ALL=(root) NOPASSWD: /usr/sbin/sysctl -q vm.compact_memory=1
#
# sudo -k
#
# sudo /usr/sbin/sysctl -q vm.compact_memory=1
set -euo pipefail

REGISTRY="ghcr.io"
REPO="peonist-ai/halogen-flash-server"
IMAGE="${REGISTRY}/${REPO}"
MODEL_DIR="/mnt/data/models/halogen-qwen3.8-flash-next"
CACHE_DIR="/mnt/data/halogen-cache"
PORT=8731
KEEP_TAGS="${KEEP_TAGS:-0.15.3}"
XRT_LIB_DIR="${XRT_LIB_DIR:-/usr/lib/x86_64-linux-gnu}"

xrt_mounts() {
  local f real
  for f in libxrt_coreutil.so.2 libxrt_core.so.2 libxrt_driver_xdna.so.2; do
    real=$(readlink -f "${XRT_LIB_DIR}/${f}") || return 1
    [[ -e "$real" ]] || { echo "missing XRT library: ${XRT_LIB_DIR}/${f}" >&2; return 1; }
    printf '%s\n' \
      -v "${real}:/opt/xilinx/xrt/lib/${f}:ro" \
      -v "${real}:${XRT_LIB_DIR}/${f}:ro" \
      -v "${real}:/opt/xilinx${XRT_LIB_DIR#/usr}/${f}:ro"
  done
}

# Everything the NPU profiles need, checked before the image is pulled so a
# missing piece costs a second rather than a four-minute weight load.
require_npu() {
  local ok=1 f

  if [[ ! -e /dev/accel/accel0 ]]; then
    echo "NPU: /dev/accel/accel0 is missing." >&2
    if grep -qw 'amd_iommu=off' /proc/cmdline; then
      echo "     amd_iommu=off is on the kernel command line; the amdxdna driver needs an IOMMU." >&2
      echo "     sudo kernelstub -d 'amd_iommu=off' && sudo kernelstub -a 'iommu=pt' && sudo reboot" >&2
    else
      echo "     The amdxdna driver is not loaded. Check: modinfo amdxdna; dmesg | grep -i amdxdna" >&2
    fi
    ok=0
  fi

  for f in libxrt_coreutil.so.2 libxrt_core.so.2 libxrt_driver_xdna.so.2; do
    if [[ ! -e "${XRT_LIB_DIR}/${f}" ]]; then
      echo "NPU: XRT library ${XRT_LIB_DIR}/${f} not found." >&2
      echo "     Install XRT with its NPU plugin, or set XRT_LIB_DIR to where yours lives." >&2
      ok=0
    fi
  done

  # The fabric clock must be held, or GPU and NPU work together can hang the box.
  if command -v halogen-fabric-clock >/dev/null 2>&1; then
    if ! halogen-fabric-clock status 2>/dev/null | grep -q held; then
      echo "NPU: the GPU fabric clock is not held — the server will refuse to start." >&2
      echo "     sudo systemctl enable --now halogen-fabric-clock.service" >&2
      ok=0
    fi
  else
    echo "NPU: halogen-fabric-clock not installed; the server may refuse to start." >&2
    echo "     See deploy/host/ in the halogen-flash-server repo." >&2
  fi

  # The device is root:render; rootless podman reaches it through keep-groups.
  if [[ -e /dev/accel/accel0 ]] && ! id -nG | tr ' ' '\n' | grep -qx render; then
    echo "NPU: your user is not in the 'render' group; --group-add keep-groups will not reach the device." >&2
    echo "     sudo usermod -aG render \$USER   (then log out and back in)" >&2
    ok=0
  fi

  [[ "$ok" == 1 ]] || { echo "NPU preflight failed; refusing to start." >&2; exit 1; }
}

compact_memory() {
  local before after
  before=$(awk '/Normal/{print $14; exit}' /proc/buddyinfo)
  sync
  # -n so it fails immediately instead of waiting on a password prompt; the UI
  # has no terminal to answer one. Grant with:
  #   /etc/sudoers.d/compact_memory
  #   <user> ALL=(root) NOPASSWD: /usr/sbin/sysctl -q vm.compact_memory=1
  sudo -n sysctl -q vm.compact_memory=1 2>/dev/null || {
    echo "compaction skipped (no passwordless sudo); order-9 blocks: ${before:-?}" >&2
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
  local -a protect=("$keep")
  IFS=, read -ra extra <<< "${KEEP_TAGS}"
  protect+=("${extra[@]}")
  podman images --filter "reference=${IMAGE}" --format '{{.Tag}}' \
    | grep -vxF -f <(printf '%s\n' "${protect[@]}") \
    | xargs -r -I{} podman rmi "${IMAGE}:{}" || true
  podman image prune -f >/dev/null
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
    -e HALOGEN_SPEC_ADAPT=0 \
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

run_optimal_slots8() {
  mkdir -p "${CACHE_DIR}-s8"
  mapfile -t env_args < <(optimal_env 786432)
  run "$1" -v "${CACHE_DIR}-s8:/cache" "${env_args[@]}" \
    -e HALOGEN_KV_SLOTS=8
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
    -e HALOGEN_TOP_P=0.95 \
    -e HALOGEN_TOP_K=20 \
    -e HALOGEN_VISION_TOWER=1 \
    -e HALOGEN_VISION_MAX_PIXELS=2073600
}

run_optimal_npu() {
  mkdir -p "${CACHE_DIR}"
  require_npu
  mapfile -t env_args < <(optimal_env 524288)
  mapfile -t xrt_args < <(xrt_mounts) || exit 1
  run "$1" \
    -v "${CACHE_DIR}:/cache" \
    --device /dev/accel/accel0 \
    "${xrt_args[@]}" \
    "${env_args[@]}" \
    -e HALOGEN_NPU_MODELS=qwen3-embedding-0.6b,qwen3-reranker-0.6b,decider-0.8b,qwen3guard-gen-0.6b,qwen3.5-2b \
    -e HALOGEN_VISION_TOWER=1 \
    -e HALOGEN_VISION_MAX_PIXELS=2073600
}

run_optimal_ht43() {
  mkdir -p "${CACHE_DIR}-ht43"
  mapfile -t env_args < <(optimal_env 786432)
  run "$1" -v "${CACHE_DIR}-ht43:/cache" "${env_args[@]}" \
    -e HALOGEN_CHECKPOINT=/models/qwen38-flash-next-ht43.hgn
}

run_optimal_npu_ht43() {
  mkdir -p "${CACHE_DIR}-ht43"
  require_npu
  mapfile -t env_args < <(optimal_env 786432)
  mapfile -t xrt_args < <(xrt_mounts) || exit 1
  run "$1" \
    -v "${CACHE_DIR}-ht43:/cache" \
    --device /dev/accel/accel0 \
    "${xrt_args[@]}" \
    "${env_args[@]}" \
    -e HALOGEN_CHECKPOINT=/models/qwen38-flash-next-ht43.hgn \
    -e HALOGEN_NPU_MODELS=qwen3-embedding-0.6b,qwen3-reranker-0.6b,decider-0.8b,qwen3guard-gen-0.6b,qwen3.5-2b
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
  run-optimal-slots8)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal slots8)" >&2; clean "$tag"
    run_optimal_slots8 "$tag" ;;
  run-optimal-vision)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal + vision)" >&2; clean "$tag"
    run_optimal_vision "$tag" ;;
  run-swift-abliterated)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (swift abliterated)" >&2; clean "$tag"
    run_swift_abliterated "$tag" ;;
  run-optimal-npu)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal + npu)" >&2; clean "$tag"
    run_optimal_npu "$tag" ;;
  run-optimal-ht43)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal + ht43)" >&2; clean "$tag"
    run_optimal_ht43 "$tag" ;;
  run-optimal-npu-ht43)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal + npu + ht43)" >&2; clean "$tag"
    run_optimal_npu_ht43 "$tag" ;;
  clean)  tag=$(resolve); clean "$tag" ;;
  latest) resolve ;;
  *)      echo "usage: $0 [run|run-optimal|run-optimal-slots8|run-optimal-vision|run-optimal-npu|run-optimal-ht43|run-optimal-npu-ht43|run-swift-abliterated|clean|latest]   (VERSION=x.y.z to pin)" >&2; exit 1 ;;
esac
