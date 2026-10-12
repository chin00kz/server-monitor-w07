# Server Monitor

Server Monitor is a private operations dashboard for the Ubuntu homelab node `workstation-07-5d`. It combines live host telemetry, historical resource trends, PostgreSQL activity, Docker health, storage, network reachability, maintenance-awareness checks, and Telegram alert history in one authenticated console.

## Dashboard

The production UI uses a dark graphite and steel-blue industrial console layout with Ubuntu orange actions. Add an approved dashboard capture at the location below when one is available:

> Screenshot placeholder: `docs/dashboard-screenshot.png`

The dashboard is organized into these working sections:

- **Overview**: CPU, memory, temperature, fan, battery, uptime, route, storage, Docker, database, and maintenance-awareness summaries.
- **Performance**: CPU, memory, temperature, load, pressure, fan, battery, and historical charts.
- **Network**: interface addresses, traffic, usage, preferred route, failover state, and reachability checks.
- **Storage**: root filesystem capacity and storage history.
- **Docker**: PostgreSQL health followed by the monitored containers, including `server-monitor`, the five PostgreSQL containers, and the disposable `homelab-test` target.
- **Events**: Telegram alert history.

## Features

- Authenticated FastAPI dashboard with login sessions and protected monitoring endpoints.
- Live host sampling for CPU, memory, load, temperatures, fan, battery, network, and disk metrics.
- SQLite-backed historical series with selectable time ranges.
- PostgreSQL connection, activity, capacity, long-running query, idle-transaction, and database-size checks.
- Docker state, health, and restart-count checks for the monitored containers.
- Network interface telemetry, route preference, ISP reachability, Tailscale status, and estimated daily usage.
- Maintenance-awareness checks for PostgreSQL, SSH, and local sessions.
- Telegram alert delivery, Telegram control polling, heartbeat monitoring, and alert history.
- Refresh Now for a fresh application sample plus an acknowledged host collection covering PostgreSQL, Docker, storage, and network checks.
- Docker Control v1 for the five PostgreSQL containers and `homelab-test`: start, confirmed stop/restart, and capped read-only logs. `server-monitor` is Logs-only.
- Automatic polling continues independently of manual refreshes.

## Architecture

- **FastAPI and Uvicorn**: serves the authenticated API and static dashboard from `app/src`.
- **Vanilla JavaScript**: `app/src/web/index.html` renders the console, charts, navigation, polling, and refresh workflow without a frontend framework.
- **SQLite**: `app/src/store.py` stores application telemetry at `/data/metrics.db`.
- **Docker Compose**: `compose.yml` runs the dashboard in a restricted Python container with read-only root filesystem, dropped capabilities, resource limits, and `/proc`/`/sys` read-only mounts.
- **Host collectors**: `activity/collect.py` reads PostgreSQL, session, Docker, storage, and network state and writes an atomic snapshot to `data/activity/latest.json`.
- **Reliability checks**: `activity/reliability.py` contains read-only Docker, storage, and failover checks for host monitoring workflows.
- **Alert workers**: scripts in `alerts/` handle alert delivery, heartbeat checks, and Telegram controls using files under `secrets/` and `data/alerts/`.

The dashboard container and host collector share `data/`. The container writes a manual refresh request; the host-side systemd path unit starts the existing oneshot collector, which writes a request-matched acknowledgment.

Docker control uses a separate host-side controller and request/response files under `data/docker-control/`. The dashboard never receives Docker socket access. The controller runs with narrowly scoped Docker-group access, accepts only configured names and fixed actions, excludes `server-monitor`, serializes requests, limits logs, and records outcomes in `data/activity/docker-control.log`.

## Ubuntu deployment

Production deployment is intended for the Ubuntu host and must remain Tailscale-only. The repository includes the path unit and deployment notes in [activity/DEPLOYMENT.md](activity/DEPLOYMENT.md). The existing scheduled oneshot service and timer remain host-managed.

### Prerequisites

- Ubuntu with Docker Engine and the Compose plugin.
- Python 3 for host-side collectors.
- Docker access for the account running `activity/collect.py`.
- A Tailscale address for the dashboard bind address.
- Existing systemd service and timer configuration for the scheduled collector.

### Environment setup

Copy the non-secret template and replace the placeholders with values for the host:

```sh
cp .env.example .env
editor .env
```

Set:

- `BIND_IP`: the host's Tailscale address. Do not bind the published dashboard port to a public or unrestricted interface.
- `APP_UID` and `APP_GID`: the numeric UID and GID of the account that owns `data/` and `data/activity/`.

Create the excluded runtime directories and secret files outside Git. The application expects an Argon2 admin hash at `secrets/admin.hash`; the Telegram workers use `secrets/telegram-token` and `secrets/telegram-chat-id` when Telegram monitoring is enabled. Never place their contents in this repository.

The shared data directories must be writable by the configured application/collector account. On the production host, keep `data/` and `data/activity/` owned by the service account with mode `775`.

### Build and start

From the repository root:

```sh
docker compose build
docker compose up -d

docker compose ps
```

The dashboard listens on container port `8080` and is published on host port `8088` only at `BIND_IP`. The container health endpoint is `/api/health`; the authenticated dashboard is `/`.

Do not deploy by restarting PostgreSQL containers. Update the dashboard container and host collector only during an approved maintenance window, and verify the dashboard health check before making further changes.

### Host monitoring and Refresh Now

The scheduled collector remains a systemd oneshot with its existing timer and a 50-second service timeout. Install and enable the manual-refresh path watcher once on the host:

```sh
sudo install -m 0644 activity/server-activity-monitor.path /etc/systemd/system/server-activity-monitor.path
sudo systemctl daemon-reload
sudo systemctl enable --now server-activity-monitor.path
```

The existing `server-activity-monitor.service` must run `activity/collect.py`. Keep its existing timer enabled. Do not install the removed daemon service; on-demand collection uses the path unit instead.

For Docker Control v1, install `activity/docker_control.py`, `activity/server-docker-control.service`, and `activity/server-docker-control.path` on the host as described in [activity/DEPLOYMENT.md](activity/DEPLOYMENT.md). The controller service allowlist contains the five PostgreSQL containers and `homelab-test`; `server-monitor` is enforced as Logs-only. It requires the host account to have Docker-group access. Do not mount the Docker socket into the dashboard container.

Refresh Now performs two authenticated actions: it samples application hardware metrics immediately, then submits a host request. The host collector serializes scheduled and manual runs with a bounded lock, collects live PostgreSQL, Docker, storage, and network data, writes the snapshot atomically, and acknowledges the matching request ID. Network probes are rate-limited during manual refreshes.

## Security design

- The dashboard is published only on the configured Tailscale address.
- Login-protected API routes require the `sm_session` cookie; password verification uses Argon2.
- Password hashes and Telegram credentials are mounted from the ignored `secrets/` directory.
- The container runs as the configured non-root UID/GID, uses a read-only root filesystem, drops all capabilities, enables `no-new-privileges`, and has CPU, memory, and process limits.
- `/proc` and `/sys` are mounted read-only for telemetry. The application does not expose those mounts directly.
- Responses use no-store security headers and the application disables FastAPI documentation endpoints.
- Runtime data, databases, backups, Python caches, virtual environments, secrets, and `.env` files are excluded by `.gitignore`.
- Docker management requires an existing authenticated session and same-origin POST headers. v1 has no separate role database, so an authenticated dashboard user is treated as an authorized operator; review access to the dashboard accordingly.

Tailscale access is an access boundary, not a replacement for authentication. Keep the dashboard password and host access controls managed separately.

## Troubleshooting and maintenance

### Dashboard is unreachable

1. Check `docker compose ps` and the container health status.
2. Confirm `.env` has the correct Tailscale `BIND_IP` and that the client is on the same authorized tailnet.
3. Inspect `docker compose logs --tail=100 monitor`.
4. Check `/api/health` locally on the host without exposing the port publicly.

### Host readings are stale

1. Check the existing collector service and timer status with `systemctl status`.
2. Check `server-activity-monitor.path` is enabled and watching the production `data/activity/refresh.request` path.
3. Confirm `data/activity/` ownership and write permissions.
4. Run the collector manually during maintenance if a direct diagnostic is needed: `python3 activity/collect.py`.

### Refresh Now remains pending

The dashboard waits for the host-side request acknowledgment. Check the path unit, the existing oneshot service, the collector lock, and the files `data/activity/refresh.request` and `data/activity/refresh.status`. A pending result does not mean the previous snapshot is fresh.

### Telegram alerts are unavailable

Confirm the three required secret files, network access from the alert worker, and the relevant host-managed service status. Alert history is read from `data/alerts/events.log` and does not itself send messages.

For maintenance, keep Docker images, Python dependencies, Ubuntu packages, and systemd unit definitions reviewed separately. Preserve `data/`, `secrets/`, SQLite files, and alert offsets during upgrades.

## Repository structure

```text
.
├── activity/
│   ├── collect.py
│   ├── reliability.py
│   ├── server-activity-monitor.path
│   └── DEPLOYMENT.md
├── alerts/
│   ├── alert_service.py
│   ├── control_service.py
│   ├── heartbeat.py
│   └── telegram_controls.py
├── app/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
│       ├── hashpw.py
│       ├── host.py
│       ├── main.py
│       ├── store.py
│       └── web/
│           ├── index.html
│           ├── index.staging.html
│           └── login.html
├── compose.yml
├── compose.test.yml
├── .env.example
├── tests/
│   └── test_docker_control.py
└── .gitignore
```

Runtime directories such as `data/` and `secrets/` are intentionally absent from Git.
