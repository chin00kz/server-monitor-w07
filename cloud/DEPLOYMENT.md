# Cloud status deployment

This is an optional read-only cloud status path. It does not replace or modify the local dashboard, Docker Control, activity collector, PostgreSQL containers, or their systemd units.

## Supabase

1. Create a Supabase project.
2. Run `supabase/schema.sql` in the SQL editor.
3. Keep the Supabase service-role key only on Ubuntu. Never place it in Vercel or browser code.

The `node_status` table stores one latest row per node. The database trigger sets `received_at` from the Supabase clock on every insert or update. Direct table reads are denied to API roles. The dashboard's anon/publishable key can execute only the read-only `read_node_status` function; publisher writes use the service-role key and bypass RLS.

## Ubuntu publisher

The publisher reads the existing `data/activity/latest.json` snapshot and existing SQLite metric samples. It has no Docker socket, no write access to monitoring data, and no public listener.

Install the service and timer:

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

The publisher fails independently when Supabase is unavailable. Check `systemctl status server-monitor-cloud-publisher.timer` and `journalctl -u server-monitor-cloud-publisher.service` during setup. Do not add the service key to `.env`, Compose, the Vercel project, or frontend code.

## Vercel

Deploy `cloud-dashboard/` as a separate Vercel project. Configure these server-side environment variables:

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_READONLY_KEY=replace-with-anon-or-publishable-key
CLOUD_NODE_ID=workstation-07-5d
DASHBOARD_PASSWORD_HASH=scrypt$16384$8$1$replace-with-base64url-salt$replace-with-base64url-hash
DASHBOARD_SESSION_SECRET=replace-with-at-least-32-random-bytes
```

All variables are server-side only. The Supabase key is not a publisher credential and is restricted by database grants to the read-only status RPC. The dashboard verifies the shared password against its scrypt hash on the server, applies a small in-process login limiter, and issues signed one-hour cookies. Every status request verifies that cookie before accessing Supabase.

No Ubuntu port, reverse proxy, Tailscale route, Docker socket, or local dashboard change is required for Vercel access.
