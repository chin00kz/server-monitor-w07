# Cloud status deployment

Optional **read-only** external status path. It does **not** replace or modify the local Tailscale dashboard, Docker Control, activity collector, PostgreSQL containers, or their systemd units. No Ubuntu inbound port, reverse proxy, Tailscale funnel, or Docker socket is required for cloud access.

Production dashboard: https://server-monitor-cloud-dashboard.vercel.app

## How the path works

1. The existing host collector keeps writing `data/activity/latest.json` and SQLite metrics as today.
2. `cloud/publisher.py` (systemd timer on Dell) builds a **sanitized allowlisted** JSON snapshot: node id/uptime, Starlink/Dialog route reachability, battery percent + AC state, CPU/RAM/disk percents, and six service rows (five IMFS Postgres labels + Ollama from `homelab-ollama`).
3. Optionally, `cloud/publisher_oracle.py` on `ubsever02` publishes a second row (`CLOUD_NODE_ID=ubsever02`) for the allowlisted `imfs-pg-integration` container as **IMFS Integration Backup**.
4. Publishers upsert into Supabase `node_status` with the **service-role** key. A DB trigger stamps `received_at`.
5. RLS denies direct table reads. The Vercel app’s anon key may only execute `read_node_status`.
6. After shared-password login, the cloud UI polls `/api/status` (Dell + optional backup node merge), which classifies receipt age:
   - **LIVE** if younger than ~90s
   - **STALE** until ~180s
   - **OFFLINE** / disconnected beyond that

Publisher or Supabase outages fail independently: the local monitor keeps running.

## Supabase

1. Create a Supabase project.
2. Run `supabase/schema.sql` in the SQL editor.
3. Keep the Supabase **service-role** key only on Ubuntu (`/etc/server-monitor/cloud-publisher.env`). Never place it in Vercel, Compose, `.env`, or browser code.

The `node_status` table stores one latest row per node. Direct table reads are denied to API roles. The dashboard anon/publishable key can execute only the read-only `read_node_status` function; publisher writes use the service-role key and bypass RLS.

## Ubuntu publisher

The publisher reads `data/activity/latest.json` and SQLite metric samples. It has no Docker socket, no write access to monitoring data, and no public listener.

Install the service and timer (adjust paths to the host checkout):

```sh
sudo install -m 0755 cloud/publisher.py /home/chinookz/homelab/docker/server-monitor/cloud/publisher.py
sudo install -m 0644 cloud/server-monitor-cloud-publisher.service /etc/systemd/system/server-monitor-cloud-publisher.service
sudo install -m 0644 cloud/server-monitor-cloud-publisher.timer /etc/systemd/system/server-monitor-cloud-publisher.timer
sudo install -d -m 0750 /etc/server-monitor
sudo install -m 0600 /dev/null /etc/server-monitor/cloud-publisher.env
sudo editor /etc/server-monitor/cloud-publisher.env
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor-cloud-publisher.timer
```

Set these values in `/etc/server-monitor/cloud-publisher.env`:

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=replace-with-secret
CLOUD_NODE_ID=workstation-07-5d
ACTIVITY_SNAPSHOT=/home/chinookz/homelab/docker/server-monitor/data/activity/latest.json
DB_PATH=/home/chinookz/homelab/docker/server-monitor/data/metrics.db
HOST_PROC=/proc
```

After changing `publisher.py` on the host, reinstall the file and run one shot to verify:

```sh
sudo systemctl start server-monitor-cloud-publisher.service
journalctl -u server-monitor-cloud-publisher.service -n 50 --no-pager
```

Check `systemctl status server-monitor-cloud-publisher.timer` during setup. Do not add the service key to `.env`, Compose, the Vercel project, or frontend code.

### Payload notes

- Service names are fixed in `publisher.py` (`SERVICE_NAMES`). Ollama without a Docker healthcheck is published as `no_check`; the cloud UI shows **Running** and counts it as healthy.
- Battery uses SQLite `bat.percent` / `bat.ac` (and status when present). Missing samples surface as unavailable in the UI rather than inventing values.
- Oversized or unexpected service lists are capped/sanitized again in `cloud-dashboard/lib/status.js` (`MAX_SERVICE_ITEMS`).

## Oracle publisher (`ubsever02`)

Separate host from Dell. Publishes only the backup Postgres container via `docker inspect` (no activity collector required).

1. Copy `cloud/publisher_oracle.py` to the Oracle host (example path used by the unit file):

```sh
mkdir -p /home/chinookz/server-monitor-oracle/cloud
install -m 0755 cloud/publisher_oracle.py /home/chinookz/server-monitor-oracle/cloud/publisher_oracle.py
```

2. Install systemd units and env (same Supabase project; **service-role** key on Oracle only):

```sh
sudo install -m 0644 cloud/server-monitor-oracle-cloud-publisher.service /etc/systemd/system/server-monitor-oracle-cloud-publisher.service
sudo install -m 0644 cloud/server-monitor-oracle-cloud-publisher.timer /etc/systemd/system/server-monitor-oracle-cloud-publisher.timer
sudo install -d -m 0750 /etc/server-monitor
sudo install -m 0600 /dev/null /etc/server-monitor/oracle-cloud-publisher.env
sudo editor /etc/server-monitor/oracle-cloud-publisher.env
```

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=replace-with-secret
CLOUD_NODE_ID=ubsever02
ORACLE_PG_CONTAINER=imfs-pg-integration
HOST_PROC=/proc
```

3. The `chinookz` user (or the unit `User=`) must be able to run `docker inspect` without a TTY (docker group). Adjust `WorkingDirectory` / `ExecStart` paths in the unit if your checkout differs.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor-oracle-cloud-publisher.timer
sudo systemctl start server-monitor-oracle-cloud-publisher.service
journalctl -u server-monitor-oracle-cloud-publisher.service -n 50 --no-pager
```

The dashboard merges this row when Vercel has `CLOUD_BACKUP_NODE_ID=ubsever02`. The card label is **IMFS Integration Backup** (role chip **Backup**); it does not replace Dell’s primary **IMFS Integration** tile.

## Vercel dashboard

Deploy `cloud-dashboard/` as a separate Vercel project (this repo’s project name: `server-monitor-cloud-dashboard`).

Configure these **server-side** environment variables:

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_READONLY_KEY=replace-with-anon-or-publishable-key
CLOUD_NODE_ID=workstation-07-5d
CLOUD_BACKUP_NODE_ID=ubsever02
DASHBOARD_PASSWORD_HASH=scrypt$16384$8$1$replace-with-base64url-salt$replace-with-base64url-hash
DASHBOARD_SESSION_SECRET=replace-with-at-least-32-random-bytes
```

`CLOUD_BACKUP_NODE_ID` is optional. When set, `/api/status` also reads that node and appends the **IMFS Integration Backup** service tile.

All variables are server-side only. The Supabase key is not a publisher credential and is restricted by database grants to the read-only status RPC. The dashboard verifies the shared password against its scrypt hash on the server, applies a small in-process login limiter, and issues signed one-hour cookies. Every status request verifies that cookie before accessing Supabase.

### Ship a UI change

From `cloud-dashboard/`:

```sh
npm test
npx vercel --yes          # Preview first when iterating
npx vercel --prod --yes   # Production alias
```

UI details, env hash generation, and pruning old deployments: [../cloud-dashboard/README.md](../cloud-dashboard/README.md).

### Final UI shape (do not regress)

Keep this hybrid unless deliberately redesigning:

- Simple top bar (node + LIVE badge + uptime/updated + Sign out).
- Three status panels: Connectivity, Host power, Utilization (with icons).
- Workloads as a **service card grid** (DB icon for Postgres services, AI icon for Ollama; seventh tile **IMFS Integration Backup** with a restrained Backup chip when Oracle publisher is configured).

No Ubuntu port, reverse proxy, Tailscale route, Docker socket, or local dashboard change is required for Vercel access.
