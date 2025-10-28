#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/config.sh"
cd "${PROJECT_ROOT}"

echo "=== DRY RUN: showing what WOULD change (no files written) ==="
echo "Local:  ${PROJECT_ROOT}/"
echo "Remote: ${REMOTE}"
echo "Exclude: ${EXCLUDE_FILE}"
echo

# --dry-run + --itemize-changes for precise preview
rsync "${RSYNC_BASE_OPTS[@]}" "${RSYNC_ITEMIZE_OPTS[@]}"   --dry-run -e "ssh ${SSH_OPTS}"   "${PROJECT_ROOT}/" "${REMOTE}" | sed 's/^/> /'

cat <<'INFO'

Legend (rsync itemize):
> f..t......  : file transferred (timestamp changed)
> >f.st...... : file sent (size & timestamp changed)
> *deleting   : will delete on remote
> cL/.. or cD/.. : creating symlink/dir
(See 'man rsync' “--itemize-changes” for full legend.)
INFO
