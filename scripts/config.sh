#!/usr/bin/env bash
# --- Deployment config (pre-filled to match your current rsync flags) ---

# Remote target
REMOTE_USER="nitesh_sinwar-v"
REMOTE_HOST="172.16.3.90"
REMOTE_DIR="~/ISB-AI-Server"   # remote project directory

# Local project root (these scripts live in ISB-AI-Server/scripts/)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Exclude file (same as your old command but now resolved inside project)
EXCLUDE_FILE="${PROJECT_ROOT}/rsync-exclude.txt"

# Extra SSH options if needed, e.g. "-p 2222 -i ~/.ssh/id_rsa"
SSH_OPTS=""

# Base rsync options: archive (-a), verbose (-v), compress (-z), delete, progress, human-readable
# EXACTLY mirrors your previous CLI flags
RSYNC_BASE_OPTS=(-avz --delete --progress --human-readable --exclude-from="${EXCLUDE_FILE}")

# Add itemized changes (-i) for dry-run and diffs
RSYNC_ITEMIZE_OPTS=(-i)

# Safety: stop if exclude file is missing
if [[ ! -f "${EXCLUDE_FILE}" ]]; then
  echo "ERROR: Exclude file not found at ${EXCLUDE_FILE}"
  echo "Create it or adjust EXCLUDE_FILE in scripts/config.sh"
  exit 2
fi

# Fully-qualified remote path for rsync
REMOTE="${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR%/}/"
