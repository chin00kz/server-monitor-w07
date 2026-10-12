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

The service runs as `chinookz` with only supplementary Docker-group access and is explicitly configured for `imfs-pg-m4`. Keep `server-monitor` excluded and do not add other containers to the allowlist without a separate review. The controller writes status to `data/docker-control/status.json` and audit records to `data/activity/docker-control.log`; both paths must be writable by `chinookz`. Docker control should be validated with a disposable container before any approved M4 test. No control action is required for normal dashboard operation.
