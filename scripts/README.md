# ISB-AI-Server Deployment Toolkit

This folder contains helper scripts for deploying and synchronizing the ISB-AI-Server project
between your local environment and the remote server.

## Folder Structure
```
ISB-AI-Server/
├── rsync-exclude.txt
├── scripts/
│   ├── config.sh
│   ├── deploy_dry_run.sh
│   ├── deploy.sh
│   ├── fetch_remote.sh
│   ├── diff_remote.sh
│   └── README.md
```

## Configuration
All editable parameters live in `scripts/config.sh`.

| Variable | Description |
|-----------|-------------|
| `REMOTE_USER` | SSH username (e.g. `nitesh_sinwar-v`) |
| `REMOTE_HOST` | Server hostname or IP |
| `REMOTE_DIR` | Remote deployment directory |
| `EXCLUDE_FILE` | Path to exclude file (rsync-exclude.txt) |
| `SSH_OPTS` | Extra SSH options, e.g. port or identity key |

The base rsync command used is identical to your manual workflow:
```bash
rsync -avz --delete --progress --human-readable --exclude-from='./ISB-AI-Server/rsync-exclude.txt'
```

## Usage
Run all scripts from inside the project root (`ISB-AI-Server/`).

### 1️⃣ Dry Run (no changes)
```bash
./scripts/deploy_dry_run.sh
```

### 2️⃣ Deploy (real sync)
```bash
./scripts/deploy.sh
```
With remote backup:
```bash
./scripts/deploy.sh backup
```

### 3️⃣ Fetch Remote Snapshot
```bash
./scripts/fetch_remote.sh
```
Creates `.remote_snapshot/` mirror of remote files locally.

### 4️⃣ Diff Remote vs Local
```bash
./scripts/diff_remote.sh
```
Shows rsync summary + unified diffs between local and remote snapshot.

## Duo / 2FA
First SSH login will prompt for Duo verification as usual.

## Tips
- Make scripts executable:
  ```bash
  chmod +x scripts/*.sh
  ```
- Optional aliases:
  ```bash
  alias deploy="./scripts/deploy.sh"
  alias dryrun="./scripts/deploy_dry_run.sh"
  ```
- Add `--checksum` to `RSYNC_BASE_OPTS` in config.sh for bitwise diffing.
