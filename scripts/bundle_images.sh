#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
# shellcheck disable=SC1091,SC2154  # sources vendored scripts/bundle-lib.sh (sets BUNDLE_*)
. scripts/bundle-lib.sh

[[ -n "${BUNDLE_DEV:-}" ]] || bundle_checkout_release obs-plane
bundle_version obs-plane; VER="$BUNDLE_VERSION"

# --profile gpu: the airgap bundle must include dcgm-exporter even when built
# on a non-GPU host (`config --images` omits profile-gated services otherwise).
COMPOSE=(docker compose --env-file .env --profile gpu -f docker/compose.yaml)
"${COMPOSE[@]}" pull
bundle_collect_pulled < <("${COMPOSE[@]}" config --images)

if (( ${#BUNDLE_PULLED[@]} == 0 )); then
  echo "No images resolved." >&2
  exit 1
fi
echo "Saving images: ${BUNDLE_PULLED[*]}"
docker save "${BUNDLE_PULLED[@]}" | gzip > "obs-plane-pulled-${VER}.tar.gz"
echo "Wrote: obs-plane-pulled-${VER}.tar.gz"
