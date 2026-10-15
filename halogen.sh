#!/usr/bin/env bash
# halogen.sh — run halogen-flash-server in the configuration you ask for; prune old images.
#
#   ./halogen.sh run-optimal                          a named recipe (every old name still works)
#   ./halogen.sh run-optimal --vision on              the same recipe with a feature switched
#   ./halogen.sh serve --model swift15 --slots 8      or compose one from parts
#   ./halogen.sh run-optimal --dry-run                print the podman command, start nothing
#   ./halogen.sh help | list | config
#
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
#
# -- ADD API KEY
# umask 077
# mkdir -p ~/.config/halogen
# printf '%s' "hal-$(openssl rand -hex 24)" > ~/.config/halogen/api-key
# cat ~/.config/halogen/api-key; echo
#
set -euo pipefail

# ============================================================================
# CONFIGURATION
#
# Precedence, highest first:
#     command-line flags  >  environment (the few variables listed in _ENV_VARS)
#                         >  the config file  >  the defaults in this block
#
# Keep per-machine values in the config file so this script can be replaced without losing
# them. The file is plain bash, sourced after the defaults below:
#     ~/.config/halogen/halogen.conf          (or the path in $HALOGEN_SH_CONFIG)
# For example:
#     MODEL_DIR=/srv/models/halogen
#     CACHE_DIR=/srv/halogen-cache
#     MODEL_FILE[mine]="my-finetune.hgn"      # then: ./halogen.sh serve --model mine
# ============================================================================
CONFIG_FILE="${HALOGEN_SH_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/halogen/halogen.conf}"
_ENV_VARS=(REGISTRY REPO PORT KEEP_TAGS MODEL_DIR CACHE_DIR XRT_LIB_DIR)
declare -A _from_env=()
for _v in "${_ENV_VARS[@]}"; do
  if [[ -n "${!_v+x}" ]]; then _from_env[$_v]="${!_v}"; fi
done

# --- image -----------------------------------------------------------------
REGISTRY="ghcr.io"
REPO="peonist-ai/halogen-flash-server"
PORT=8731
# comma-separated tags `clean` must keep besides the current one
KEEP_TAGS=""

# --- where things live -----------------------------------------------------
# MODEL_DIR is mounted at /models, CACHE_DIR at /cache; XRT is for the NPU only; the API
# key file is used only if it exists.
MODEL_DIR="/mnt/data/models/halogen-qwen3.8-flash-next"
CACHE_DIR="/mnt/data/halogen-cache"
XRT_LIB_DIR="/usr/lib/x86_64-linux-gnu"
API_KEY_FILE="$HOME/.config/halogen/api-key"

# --- models: file names are relative to MODEL_DIR ----------------------------
# A model with an empty file name is the server's default checkpoint. Add your own in the
# config file with MODEL_FILE[name]="file.hgn".
NGRAM_FILE="qwen38-flash-next-ngram.hgn"
SERVED_MODEL_ID="halogen-qwen3.8-flash-next"    # the name clients see
declare -A MODEL_FILE=(
  [v2]=""
  [ht43]="qwen38-flash-next-ht43.hgn"
  [abliterated]="qwen38-flash-next-v2-abliterated.hgn"
  [swift15]="qwen38-flash-next-v2-swift15-abliterated.hgn"
)
# Models that also pin NGRAM_FILE and are served as SERVED_MODEL_ID, as the old recipes did.
MODELS_PINNED=" abliterated swift15 "

# --- optional features -------------------------------------------------------
VISION_TOWER="1"              # 1 = the sidecar beside the checkpoint; or /models/<file>
VISION_MAX_PIXELS=2073600     # 1080p; a 1440p image costs about twice the prefill time
NPU_MODELS="qwen3-embedding-0.6b,qwen3-reranker-0.6b,decider-0.8b,qwen3guard-gen-0.6b,qwen3.5-2b"
FORK_MAP="claude-code"        # HALOGEN_FORK_MAP when --fork on

# --- engine tuning (what run-optimal measured best; see README) -------------
CTX=262144
KV_POOL=786432                # positions: 3 full-length conversations
NPU_POOL=524288               # the NPU recipes leave more host memory for the NPU models
KV_SLOTS=4
MAX_TOK=16384
HOST_RESERVE_GIB=24
CACHE_BRANCHES=3
TEMPERATURE=1.0
MAX_TOKENS_DEFAULT=16384
MAX_TOKENS_CAP=16384          # was 32768; lowered so Claude Code's output request fits the window
REASONING_EFFORT=medium
MAX_THINKING_TOKENS=8192
CACHE_DISK_GIB=128
KEEPALIVE_TIMEOUT=900
QUEUE_TIMEOUT=3600

# --- the config file, then values from the environment ----------------------
if [[ -r "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi
for _v in "${!_from_env[@]}"; do printf -v "$_v" '%s' "${_from_env[$_v]}"; done
[[ -n "${HALOGEN_API_KEY_FILE:-}" ]] && API_KEY_FILE="$HALOGEN_API_KEY_FILE"
IMAGE="${REGISTRY}/${REPO}"
# ============================================================================

die() { echo "halogen.sh: $*" >&2; exit 1; }

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
  (( DRY )) && return 0
  local keep="$1"
  local -a protect=("$keep")
  IFS=, read -ra extra <<< "${KEEP_TAGS}"
  protect+=("${extra[@]}")
  podman images --filter "reference=${IMAGE}" --format '{{.Tag}}' \
    | grep -vxF -f <(printf '%s\n' "${protect[@]}") \
    | xargs -r -I{} podman rmi "${IMAGE}:{}" || true
  podman image prune -f >/dev/null
}

api_key_args() {
  [[ -r "$API_KEY_FILE" ]] || return 0
  printf '%s\n' -v "${API_KEY_FILE}:/run/halogen-api-key:ro" -e HALOGEN_API_KEY_FILE=/run/halogen-api-key
}

# ---------------------------------------------------------------------------
# Flags. They fill these variables; an empty value means "not given", so a recipe may supply
# its own default and an explicit flag always wins.
# ---------------------------------------------------------------------------
CMD=""; MODEL=""; VISION=""; NPU=""; FORK=""; SLOTS=""; POOLV=""; EFFORT=""; CACHE_ARG=""
DRY=0; EXTRA_ENV=(); STOCK_FLAGS=0

onoff() {   # onoff <value> <flag>
  case "${1:-}" in
    on|yes|true|1)  echo on ;;
    off|no|false|0) echo off ;;
    *) die "$2 takes on or off (got '${1:-}')" ;;
  esac
}

parse_args() {
  while (( $# )); do
    case "$1" in
      --model)     [[ -n "${2:-}" ]] || die "--model needs a name"; MODEL="$2"; STOCK_FLAGS=1; shift 2 ;;
      --vision)    VISION=$(onoff "${2:-}" --vision); STOCK_FLAGS=1; shift 2 ;;
      --npu)       NPU=$(onoff "${2:-}" --npu); STOCK_FLAGS=1; shift 2 ;;
      --fork)      FORK=$(onoff "${2:-}" --fork); STOCK_FLAGS=1; shift 2 ;;
      --slots)     [[ "${2:-}" =~ ^[0-9]+$ ]] || die "--slots needs a number"; SLOTS="$2"; STOCK_FLAGS=1; shift 2 ;;
      --pool)      [[ "${2:-}" =~ ^[0-9]+$ ]] || die "--pool needs a number of positions"; POOLV="$2"; STOCK_FLAGS=1; shift 2 ;;
      --effort)    [[ -n "${2:-}" ]] || die "--effort needs a level"; EFFORT="$2"; STOCK_FLAGS=1; shift 2 ;;
      --cache-dir) [[ -n "${2:-}" ]] || die "--cache-dir needs a path"; CACHE_ARG="$2"; STOCK_FLAGS=1; shift 2 ;;
      --set)       [[ "${2:-}" =~ ^[A-Za-z_][A-Za-z0-9_]*=.*$ ]] || die "--set needs NAME=VALUE"
                   EXTRA_ENV+=("$2"); shift 2 ;;
      -n|--dry-run) DRY=1; shift ;;
      -h|--help)   CMD="help"; shift ;;
      -*)          die "unknown option $1 (try: $0 help)" ;;
      *)           [[ -z "$CMD" ]] || die "unexpected argument '$1'"; CMD="$1"; shift ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# Building and running the container
# ---------------------------------------------------------------------------
engine_env() {
  printf '%s\n' \
    -e HALOGEN_SPEC_ADAPT=0 \
    -e HALOGEN_MTP_PREFILL=0 \
    -e HALOGEN_WEIGHTS_LOCK=1 \
    -e "HALOGEN_CTX=${CTX}" \
    -e "HALOGEN_KV_POOL_POSITIONS=${POOLV}" \
    -e "HALOGEN_KV_SLOTS=${SLOTS}" \
    -e "HALOGEN_MAX_TOK=${MAX_TOK}" \
    -e "HALOGEN_HOST_RESERVE_GIB=${HOST_RESERVE_GIB}" \
    -e "HALOGEN_CACHE_BRANCHES=${CACHE_BRANCHES}" \
    -e HALOGEN_COMPOSABLE_CONTEXT=1 \
    -e "HALOGEN_TEMPERATURE=${TEMPERATURE}" \
    -e "HALOGEN_MAX_TOKENS_DEFAULT=${MAX_TOKENS_DEFAULT}" \
    -e "HALOGEN_MAX_TOKENS_CAP=${MAX_TOKENS_CAP}" \
    -e "HALOGEN_REASONING_EFFORT=${EFFORT}" \
    -e "HALOGEN_MAX_THINKING_TOKENS=${MAX_THINKING_TOKENS}" \
    -e HALOGEN_ENGINE_WATCHDOG_S=0 \
    -e HALOGEN_CACHE_DIR=/cache \
    -e "HALOGEN_CACHE_DISK_GIB=${CACHE_DISK_GIB}" \
    -e HALOGEN_CACHE_PRUNE_OLD=1 \
    -e "HALOGEN_KEEPALIVE_TIMEOUT=${KEEPALIVE_TIMEOUT}" \
    -e "HALOGEN_QUEUE_TIMEOUT=${QUEUE_TIMEOUT}"
}

# Assemble the podman arguments (everything after `podman run --rm`) into the array CMDLINE.
# $1 = image tag, $2 = "stock" for the untuned profile.
build_cmdline() {
  local tag="$1" mode="${2:-tuned}" cache f
  local -a xrt_args key_args env_args

  if [[ "$mode" == "stock" ]]; then
    (( STOCK_FLAGS )) && die "'run' is the stock profile and takes no feature flags; use 'serve' (only --set and --dry-run apply)"
  else
    : "${MODEL:=v2}" "${VISION:=off}" "${NPU:=off}" "${FORK:=off}"
    : "${SLOTS:=$KV_SLOTS}" "${POOLV:=$KV_POOL}" "${EFFORT:=$REASONING_EFFORT}"
    [[ -n "${MODEL_FILE[$MODEL]+x}" ]] || die "unknown model '$MODEL'; known: ${!MODEL_FILE[*]}"
  fi

  mapfile -t key_args < <(api_key_args)
  CMDLINE=(-p "${PORT}:${PORT}"
           --device /dev/kfd --device /dev/dri --group-add keep-groups
           --ipc=host --ulimit memlock=-1:-1
           -v "${MODEL_DIR}:/models:ro")
  [[ ${#key_args[@]} -gt 0 ]] && CMDLINE+=("${key_args[@]}")

  if [[ "$mode" == "tuned" ]]; then
    cache="${CACHE_ARG:-$CACHE_DIR}"
    (( DRY )) || mkdir -p "$cache"
    CMDLINE+=(-v "${cache}:/cache")

    if [[ "$NPU" == on ]]; then
      (( DRY )) || require_npu
      mapfile -t xrt_args < <(xrt_mounts) || exit 1
      CMDLINE+=(--device /dev/accel/accel0 "${xrt_args[@]}")
    fi

    mapfile -t env_args < <(engine_env)
    CMDLINE+=("${env_args[@]}")

    f="${MODEL_FILE[$MODEL]}"
    [[ -n "$f" ]] && CMDLINE+=(-e "HALOGEN_CHECKPOINT=/models/${f}")
    if [[ "$MODELS_PINNED" == *" $MODEL "* ]]; then
      CMDLINE+=(-e "HALOGEN_NGRAM_TABLE=/models/${NGRAM_FILE}" -e "HALOGEN_MODEL_ID=${SERVED_MODEL_ID}")
    fi
    [[ "$NPU" == on ]]    && CMDLINE+=(-e "HALOGEN_NPU_MODELS=${NPU_MODELS}")
    [[ "$VISION" == on ]] && CMDLINE+=(-e "HALOGEN_VISION_TOWER=${VISION_TOWER}" -e "HALOGEN_VISION_MAX_PIXELS=${VISION_MAX_PIXELS}")
    [[ "$FORK" == on ]]   && CMDLINE+=(-e HALOGEN_FORK=1 -e "HALOGEN_FORK_MAP=${FORK_MAP}")
  fi

  for f in ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"}; do CMDLINE+=(-e "$f"); done   # last, so they win
  CMDLINE+=("${IMAGE}:${tag}")
}

launch() {   # launch <tag> [stock]
  local -a CMDLINE=()
  build_cmdline "$@"
  if (( DRY )); then
    printf 'podman run --rm'
    printf ' \\\n    %q' "${CMDLINE[@]}"
    printf '\n'
    return 0
  fi
  compact_memory
  podman pull -q "${IMAGE}:${1}" >/dev/null
  exec podman run --rm "${CMDLINE[@]}"
}

# ---------------------------------------------------------------------------
# Recipes. Each fills in defaults for the options it implies; a flag always wins.
# (halogen-ui.py reads the case arms below, so keep their shape.)
# ---------------------------------------------------------------------------
run_stock() {
  launch "$1" stock
}

run_serve() {
  launch "$1"
}

run_optimal() {
  launch "$1"
}

run_optimal_slots8() {
  : "${SLOTS:=8}"
  launch "$1"
}

run_optimal_vision() {
  : "${VISION:=on}"
  launch "$1"
}

run_optimal_fork() {
  : "${FORK:=on}"
  launch "$1"
}

run_abliterated() {
  : "${MODEL:=abliterated}" "${VISION:=on}"
  launch "$1"
}

run_swift_abliterated() {
  : "${MODEL:=swift15}" "${VISION:=on}"
  launch "$1"
}

run_optimal_npu() {
  : "${NPU:=on}" "${VISION:=on}" "${POOLV:=$NPU_POOL}"
  launch "$1"
}

run_optimal_ht43() {
  : "${MODEL:=ht43}"
  launch "$1"
}

run_optimal_npu_ht43() {
  : "${MODEL:=ht43}" "${NPU:=on}"
  launch "$1"
}

resolve() {
  if [[ -n "${VERSION:-}" ]]; then echo "$VERSION"; return; fi
  if (( DRY )); then local_latest_tag 2>/dev/null || echo "<tag>"; return; fi
  latest_tag 2>/dev/null || { echo "registry unreachable, using local image" >&2; local_latest_tag; }
}

usage() {
  cat <<EOF
usage: $0 <recipe> [options]        VERSION=x.y.z pins the image tag

recipes (the old names; options below override what a recipe implies):
  run                 stock defaults, nothing tuned
  serve               the tuned engine, no extras (same as run-optimal)
  run-optimal         v2, vision off, ${KV_SLOTS} slots, pool ${KV_POOL}
  run-optimal-slots8  ... with 8 slots
  run-optimal-vision  ... with the vision tower
  run-optimal-fork    ... with Claude Code sessions (HALOGEN_FORK)
  run-abliterated     abliterated v2 + vision
  run-swift-abliterated   swift15 abliterated + vision
  run-optimal-npu     NPU models + vision, pool ${NPU_POOL}
  run-optimal-ht43    the ht43 checkpoint
  run-optimal-npu-ht43    ht43 + NPU models
other commands:  clean | latest | list | config | help

options (any recipe except 'run'):
  --model NAME        one of: ${!MODEL_FILE[*]}
  --vision on|off     the vision tower
  --npu on|off        the NPU models (needs the driver, XRT and the fabric-clock unit)
  --fork on|off       Claude Code sessions
  --slots N           KV slots (concurrent conversations)
  --pool N            KV pool positions
  --effort LEVEL      default reasoning effort: low, medium or xhigh
  --cache-dir PATH    prompt cache directory (default: the main one)
  --set NAME=VALUE    any container variable, e.g. --set HALOGEN_MTP_PREFILL=1 (repeatable, last)
  -n, --dry-run       print the podman command and exit; no pull, no prune, no compaction
EOF
}

list_things() {
  echo "models (file under $MODEL_DIR):"
  local m
  for m in "${!MODEL_FILE[@]}"; do
    printf '  %-14s %s\n' "$m" "${MODEL_FILE[$m]:-(server default)}"
  done | sort
}

show_config() {
  local v
  echo "config file: $CONFIG_FILE $( [[ -r "$CONFIG_FILE" ]] && echo '(loaded)' || echo '(not present)' )"
  for v in REGISTRY REPO PORT KEEP_TAGS MODEL_DIR CACHE_DIR API_KEY_FILE XRT_LIB_DIR NGRAM_FILE \
           SERVED_MODEL_ID CTX KV_POOL NPU_POOL KV_SLOTS MAX_TOK MAX_TOKENS_DEFAULT MAX_TOKENS_CAP \
           REASONING_EFFORT MAX_THINKING_TOKENS CACHE_DISK_GIB; do
    printf '  %-22s %s\n' "$v" "${!v}"
  done
  [[ -r "$API_KEY_FILE" ]] && echo "  (API key file present: the key is passed to the container)" \
                           || echo "  (no API key file: the server starts open)"
}

parse_args "$@"

case "${CMD:-run}" in
  help)    usage ;;
  list)    list_things ;;
  config)  show_config ;;
  run)
    tag=$(resolve); echo "using ${IMAGE}:${tag}" >&2; clean "$tag"
    run_stock "$tag" ;;
  serve)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (serve)" >&2; clean "$tag"
    run_serve "$tag" ;;
  run-optimal)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal)" >&2; clean "$tag"
    run_optimal "$tag" ;;
  run-optimal-slots8)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal slots8)" >&2; clean "$tag"
    run_optimal_slots8 "$tag" ;;
  run-optimal-vision)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal + vision)" >&2; clean "$tag"
    run_optimal_vision "$tag" ;;
  run-optimal-fork)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (optimal fork)" >&2; clean "$tag"
    run_optimal_fork "$tag" ;;
  run-abliterated)
    tag=$(resolve); echo "using ${IMAGE}:${tag} (abliterated)" >&2; clean "$tag"
    run_abliterated "$tag" ;;
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
  *)      echo "usage: $0 [run|serve|run-optimal|run-optimal-slots8|run-optimal-vision|run-optimal-fork|run-optimal-npu|run-optimal-ht43|run-optimal-npu-ht43|run-abliterated|run-swift-abliterated|clean|latest|list|config|help] [options]   (VERSION=x.y.z to pin)" >&2; exit 1 ;;
esac
