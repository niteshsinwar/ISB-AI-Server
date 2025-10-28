#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/config.sh"
cd "${PROJECT_ROOT}"

SNAP_DIR="${PROJECT_ROOT}/.remote_snapshot"
mkdir -p "${SNAP_DIR}"

echo "Fetching remote snapshot into ${SNAP_DIR} (safe mirror of server copy)..."
rsync "${RSYNC_BASE_OPTS[@]}" -e "ssh ${SSH_OPTS}" --delete "${REMOTE}" "${SNAP_DIR}/"

echo "Snapshot ready at: ${SNAP_DIR}"
