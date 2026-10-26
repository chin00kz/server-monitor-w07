# Ubuntu deployment

Production keeps the existing `server-activity-monitor.service` oneshot and its scheduled timer. Do not install the removed daemon service.

1. Set the compose user to the account that owns the data directory. In `.env`, use `APP_UID=$(id -u chinookz)` and `APP_GID=$(id -g chinookz)`, then verify `data/` and `data/activity/` are owned by `chinookz:chinookz` with mode `775`.
2. Install `server-activity-monitor.path` as `/etc/systemd/system/server-activity-monitor.path`. The existing service must run `activity/collect.py`, retain `TimeoutStartSec=50`, and remain paired with its existing timer.
3. Reload and enable the path watcher without touching the running containers or PostgreSQL services:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now server-activity-monitor.path
   ```

The dashboard container writes `data/activity/refresh.request`; the path unit starts the existing oneshot. The collector removes the request only after it has acquired its bounded lock, so a scheduled/manual overlap is picked up by the next service run.

## Docker control v1

Docker control is separate from the dashboard container. Do not mount `/var/run/docker.sock` into Compose. Install the host controller and path unit only during an approved maintenance window:

```sh
sudo install -m 0755 activity/docker_control.py /home/chinookz/homelab/docker/server-monitor/activity/docker_control.py
sudo install -m 0644 activity/server-docker-control.service /etc/systemd/system/server-docker-control.service
sudo install -m 0644 activity/server-docker-control.path /etc/systemd/system/server-docker-control.path
sudo install -d -o chinookz -g chinookz -m 775 data/docker-control
sudo usermod -aG docker chinookz
sudo systemctl daemon-reload
sudo systemctl enable --now server-docker-control.path
```

The service runs as `chinookz` with only supplementary Docker-group access and is explicitly configured for the five PostgreSQL containers plus `homelab-test` and `homelab-ollama`. `server-monitor` is enforced as Logs-only; its state-changing actions remain rejected by the controller. The controller writes status to `data/docker-control/status.json` and audit records to `data/activity/docker-control.log`; both paths must be writable by `chinookz`. Validate Docker control with the disposable `homelab-test` configuration before any approved live test. No control action is required for normal dashboard operation.

The reusable disposable test container is separate from production Compose:

```sh
docker compose -f compose.test.yml up -d
docker compose -f compose.test.yml ps
docker compose -f compose.test.yml down
```

It has no published ports, volumes, or restart policy. During testing, verify `homelab-test` state and health before control actions. Do not perform live PostgreSQL or `server-monitor` state changes until explicitly approved.

## Backup status and manual backup control

The dashboard does not receive host filesystem access or the rclone configuration. A restricted host-side controller reads the existing backup folders, verifies both `SUCCESS` and `manifest.txt` in Google Drive, reads quota with `rclone about gdrive:`, and invokes only the existing fixed backup script. Install it during an approved maintenance window:

```sh
sudo install -m 0755 activity/backup_control.py /home/chinookz/homelab/docker/server-monitor/activity/backup_control.py
sudo install -m 0644 activity/server-backup-control.service /etc/systemd/system/server-backup-control.service
sudo install -m 0644 activity/server-backup-control.path /etc/systemd/system/server-backup-control.path
sudo install -m 0644 activity/server-backup-status.timer /etc/systemd/system/server-backup-status.timer
sudo install -d -o chinookz -g chinookz -m 775 data/backup-control
sudo systemctl daemon-reload
sudo systemctl enable --now server-backup-control.path server-backup-status.timer
```

The controller runs as `chinookz`, so it reuses that account's existing rclone configuration without exposing it to the dashboard container. Status refreshes run once per minute. Manual requests are serialized by the controller lock, reject an already-running `backup.sh` process, and invoke the fixed script through `flock` at `backups/.server-monitor-backup.lock`. Local backup downloads are restricted to timestamp-named direct children of the backup directory. ZIP files are generated under `data/backup-control/archives`, removed after transfer, and expired after one hour if a transfer is abandoned. The existing cron schedule, backup script, PostgreSQL containers, rclone configuration, and Drive contents are not changed by this integration.

## Sync IMFS to Oracle (dashboard)

Uses the **same** backup-control PathChanged oneshot (`server-backup-control.path` / `.service`). No second systemd stack.

| Action | Effect |
|---|---|
| `oracle_prepare` | Health → Dell `export-integration.sh` → SCP dumps → checksums → `imfs-db-restore stage-dumps`. Stops at `awaiting_confirmation`. Does **not** replace live Oracle DBs. |
| `oracle_apply` | Re-checks Dell `boot_id`, re-verifies staged dump SHA256 on Oracle, then runs fixed `imfs-db-restore apply CONFIRM-REPLACE-IMFS` once. |

Dashboard phrase: `REPLACE-ORACLE-IMFS` (API only; never logged). Oracle argv remains `CONFIRM-REPLACE-IMFS` (same as live `deploy.sh` after the prompt).

Status/log: `data/backup-control/oracle-sync-status.json` and `oracle-sync.log` (separate from local/cloud backup status). Controller lock is held only while prepare/apply/backup/archive runs — **not** while waiting for human confirmation. Local Backup Now remains available during `awaiting_confirmation`, but prepare/apply conflict with an in-flight backup and with each other.

**Live apply behavior (do not hide):** `imfs-apply-dumps` runs `dropdb --if-exists` on `imfs_next_*` before recreate. After a failed or partial cutover, do **not** auto-retry apply. Do not delete staged files, `imfs_prev_*` recovery DBs, or Oracle recovery SQL from the dashboard.

### Execution (default on)

Prepare and apply run on the **Dell host** via `server-backup-control.service` (not inside the dashboard container). They use the existing fixed paths:

- `~/imfs-oracle/export-integration.sh`
- SSH key `~/.ssh/imfs_oracle_deploy` → `chinookz@144.24.152.14`
- `sudo -n /usr/local/sbin/imfs-db-restore` (`health`, `stage-dumps`, `apply CONFIRM-REPLACE-IMFS`)

**Destructive cutover** only happens after the dashboard phrase `REPLACE-ORACLE-IMFS` queues `oracle_apply`. Prepare alone exports, transfers, and stages; it does not rename live Oracle DBs. Apply replaces `imfs_central` / `imfs_tenant_apex` (recovery backup + `imfs_prev_*`; also `dropdb --if-exists` on `imfs_next_*`).

Emergency kill-switch (optional): set `Environment=ORACLE_SYNC_DISABLE=1` on `server-backup-control.service`, then `daemon-reload`. Default is unset so Sync runs.

### Prerequisites on Dell (before first Sync)

```sh
test -x "$HOME/imfs-oracle/export-integration.sh" && echo export_ok
test -f "$HOME/.ssh/imfs_oracle_deploy" && echo key_ok
ssh -i "$HOME/.ssh/imfs_oracle_deploy" -o BatchMode=yes chinookz@144.24.152.14 \
  'sudo -n /usr/local/sbin/imfs-db-restore health'
systemctl is-active server-backup-control.path
```

Expect `healthy` from Oracle health. Re-install host controller after code updates:

```sh
sudo install -m 0755 activity/backup_control.py \
  /home/chinookz/homelab/docker/server-monitor/activity/backup_control.py
```

Rebuild/restart only `monitor` for API/UI changes. Do not set `ORACLE_SYNC_DISABLE` unless intentionally blocking sync.

### Apply failure

On cutover failure: preserve logs and artifacts; do **not** blindly re-run apply.
