#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/config.sh"
cd "${PROJECT_ROOT}"

SNAP_DIR="${PROJECT_ROOT}/.remote_snapshot"
if [[ ! -d "${SNAP_DIR}" ]]; then
  echo "No snapshot found. Creating one now..."
  "${PROJECT_ROOT}/scripts/fetch_remote.sh"
fi

echo "=== High-level change list (rsync-style) ==="
rsync "${RSYNC_BASE_OPTS[@]}" "${RSYNC_ITEMIZE_OPTS[@]}"   --dry-run -e "ssh ${SSH_OPTS}"   "${PROJECT_ROOT}/" "${REMOTE}" | sed 's/^/> /'

echo
echo "=== Unified diffs (local vs .remote_snapshot) ==="
echo "(This compares file contents. Files existing on only one side are shown with -N semantics.)"
echo

# Best-effort mapping of rsync excludes to diff excludes
DIFF_EXCLUDES=()
while IFS= read -r pat; do
  [[ -z "$pat" || "$pat" =~ ^# ]] && continue
  DIFF_EXCLUDES+=( "--exclude=${pat}" )
done < "${EXCLUDE_FILE}"

diff -ruN "${DIFF_EXCLUDES[@]}" "${PROJECT_ROOT}/" "${SNAP_DIR}/" || true
