# Cloud status dashboard

Read-only Vercel app that shows the latest sanitized snapshot of `workstation-07-5d`. It has **no** Docker controls, reverse shell, or Ubuntu inbound access. Auth is one shared password (scrypt hash + signed cookie), not Supabase Auth users.

Production URL: https://server-monitor-cloud-dashboard.vercel.app

## How it works

```text
Ubuntu host                     Supabase                         Vercel
─────────────                   ────────                         ──────
activity/collect.py  ──►  data/activity/latest.json
SQLite metrics.db
        │
        ▼
cloud/publisher.py  ──HTTPS upsert──►  node_status (service role)
                                       received_at set by DB trigger
                                              │
                                              ▼
                                    read_node_status() RPC
                                    (anon key only)
                                              │
                                              ▼
                                    /api/status (session cookie)
                                              │
                                              ▼
                                    public/ dashboard UI
```

1. Host timer runs `cloud/publisher.py` on a schedule.
2. Publisher reads `latest.json` + SQLite, builds a fixed allowlisted payload (network, battery, CPU/RAM/disk, six services including Ollama), upserts with the **service-role** key.
3. Browser users sign in with the shared password. `/api/auth/login` verifies scrypt hash, rate-limits attempts, sets an HttpOnly signed cookie.
4. `/api/status` requires that cookie, calls `read_node_status` with the **anon/readonly** key, classifies receipt age as live / stale / disconnected, and returns a sanitized view model.
5. The static UI polls `/api/status` about every 30 seconds. Presentation logic lives in `public/dashboard.js` (also unit-tested).

Failure of the cloud path does not affect the local Tailscale dashboard or Docker Control.

## UI (final layout)

- **Header**: Cloud status, node id, LIVE/STALE/OFFLINE badge, uptime, last update, Sign out.
- **Top row (3 panels)**: Connectivity (Starlink / Dialog), Host power (battery), Utilization (CPU / RAM / disk) — industrial outline icons.
- **Bottom**: Workloads service tiles (3×N) with PostgreSQL / AI icons and green/amber/red top borders. Oracle backup is a seventh tile with a muted **Backup** chip.

Services shown:

| Label | Source |
| --- | --- |
| IMFS Integration | Dell `imfs-pg-integration` |
| IMFS M1–M4 | Dell `imfs-pg-m1` … `imfs-pg-m4` |
| Ollama | Dell `homelab-ollama` — `no_check` displays as **Running** and counts toward healthy |
| IMFS Integration Backup | Oracle `ubsever02` `imfs-pg-integration` via `publisher_oracle.py` (merged when `CLOUD_BACKUP_NODE_ID` is set) |

## Project layout

```text
cloud-dashboard/
├── api/
│   ├── auth/login.js      # shared-password login
│   ├── auth/logout.js
│   └── status.js          # cookie + Supabase RPC
├── lib/
│   ├── auth.js            # scrypt verify, signed cookie, rate limit
│   └── status.js          # receipt age + payload sanitizer
├── public/
│   ├── index.html
│   ├── dashboard.css
│   ├── dashboard.js       # view-model + DOM render
│   └── favicon.svg
├── tests/
├── vercel.json
└── package.json
```

## Environment variables (Vercel, server-side only)

| Variable | Purpose |
| --- | --- |
| `SUPABASE_URL` | Project URL |
| `SUPABASE_READONLY_KEY` | Anon/publishable key — can call `read_node_status` only |
| `CLOUD_NODE_ID` | Must match Dell publisher (`workstation-07-5d`) |
| `CLOUD_BACKUP_NODE_ID` | Optional Oracle node id (`ubsever02`) for the backup Postgres tile |
| `DASHBOARD_PASSWORD_HASH` | `scrypt$N$r$p$salt$hash` (base64url; min `16384$8$1`) |
| `DASHBOARD_SESSION_SECRET` | ≥32 random bytes for cookie HMAC |

Generate a password hash (do not commit the plaintext):

```js
const {randomBytes, scryptSync} = require("node:crypto");
const password = "replace-in-memory-only";
const salt = randomBytes(16);
console.log(`scrypt$16384$8$1$${salt.toString("base64url")}$${scryptSync(password, salt, 64).toString("base64url")}`);
```

Session secret: `openssl rand -base64 48`.

Never put the publisher **service-role** key in Vercel or the browser.

## Local tests

```sh
cd cloud-dashboard
npm test
```

## Deploy

From `cloud-dashboard/` (project already linked via `.vercel/`):

```sh
# Preview (review URL, does not change production)
npx vercel --yes

# Production (updates https://server-monitor-cloud-dashboard.vercel.app)
npx vercel --prod --yes
```

Remove old deployment URLs after a final ship so only the current production deployment remains:

```sh
npx vercel ls server-monitor-cloud-dashboard
npx vercel rm <old-deployment-url> ... --yes --safe
```

Protected previews may return 403 to anonymous `curl`; use `npx vercel curl <url>` when inspecting.

Full host + Supabase setup: [../cloud/DEPLOYMENT.md](../cloud/DEPLOYMENT.md).
