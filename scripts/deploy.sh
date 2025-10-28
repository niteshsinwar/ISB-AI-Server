#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/config.sh"
cd "${PROJECT_ROOT}"

BACKUP="${1:-}"  # pass "backup" to create a remote .tar.gz before deploy

if [[ "${BACKUP}" == "backup" ]]; then
  TS=$(date +%Y%m%d_%H%M%S)
  REMOTE_BK="${REMOTE_DIR%/}_backup_${TS}.tar.gz"
  echo "Creating remote backup at ${REMOTE_BK} ..."
  ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}"     "tar -czf ${REMOTE_BK} -C $(dirname ${REMOTE_DIR}) $(basename ${REMOTE_DIR})"
  echo "Remote backup done."
fi

echo "Deploying to ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"
rsync "${RSYNC_BASE_OPTS[@]}" -e "ssh ${SSH_OPTS}" "${PROJECT_ROOT}/" "${REMOTE}"
echo "Deploy complete."
