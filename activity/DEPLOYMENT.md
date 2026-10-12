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
