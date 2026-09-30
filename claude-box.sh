#!/usr/bin/env bash
# claude-box.sh — run Claude Code in a podman container against the halogen
# server on the host.
#
#   ./claude-box.sh                 # sandbox the current directory
#   ./claude-box.sh /path/to/repo   # sandbox a specific directory
#   ./claude-box.sh --shell         # drop to a shell instead of launching claude
#   ./claude-box.sh --build         # (re)build the image
#
# The container gets: the project at /workspace, a persistent home volume, and
# network access. It does NOT get your SSH keys, your gpg keys, your shell
# history, /mnt/data, or anything else on the host.
set -euo pipefail

IMAGE="claude-box"
CONTAINERFILE="$(dirname "$0")/Containerfile.claude"
HOME_VOL="claude-box-home"          # persists ~/.claude, npm globals, etc.
HALOGEN_PORT=8731
MEM_LIMIT="6g"                      # leave room for halogen's pinned weights

build() {
  podman build -t "${IMAGE}" \
    --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
    -f "${CONTAINERFILE}" "$(dirname "${CONTAINERFILE}")"
}

SHELL_MODE=false
PROJECT=""
for arg in "$@"; do
  case "$arg" in
    --build)  build; exit 0 ;;
    --shell)  SHELL_MODE=true ;;
    *)        PROJECT="$arg" ;;
  esac
done

PROJECT="$(realpath "${PROJECT:-$PWD}")"
[[ -d "$PROJECT" ]] || { echo "no such directory: $PROJECT" >&2; exit 1; }

podman image exists "${IMAGE}" || build
podman volume exists "${HOME_VOL}" || podman volume create "${HOME_VOL}" >/dev/null

# Reach the host's halogen. Podman publishes host.containers.internal for
# rootless networking; fall back to the default gateway if it is missing.
HOST_ALIAS="host.containers.internal"

echo "sandbox: ${PROJECT}  ->  /workspace" >&2
echo "model:   http://${HOST_ALIAS}:${HALOGEN_PORT}" >&2

exec podman run --rm -it \
  --name "claude-box-$$" \
  --userns=keep-id \
  --memory="${MEM_LIMIT}" --memory-swap="${MEM_LIMIT}" \
  --pids-limit=4096 \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  -v "${PROJECT}:/workspace" \
  -v "${HOME_VOL}:/home/dev" \
  -w /workspace \
  -e HOME=/home/dev \
  -e TERM="${TERM:-xterm-256color}" \
  -e ANTHROPIC_BASE_URL="http://${HOST_ALIAS}:${HALOGEN_PORT}" \
  -e ANTHROPIC_AUTH_TOKEN="local" \
  -e CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384 \
  -e API_TIMEOUT_MS=1800000 \
  -e CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
  -e DISABLE_TELEMETRY=1 \
  -e GIT_AUTHOR_NAME="$(git config --get user.name 2>/dev/null || echo dev)" \
  -e GIT_AUTHOR_EMAIL="$(git config --get user.email 2>/dev/null || echo dev@localhost)" \
  -e GIT_COMMITTER_NAME="$(git config --get user.name 2>/dev/null || echo dev)" \
  -e GIT_COMMITTER_EMAIL="$(git config --get user.email 2>/dev/null || echo dev@localhost)" \
  "${IMAGE}" \
  "$( $SHELL_MODE && echo bash || echo 'claude --dangerously-skip-permissions' )"
