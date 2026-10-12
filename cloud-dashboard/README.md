# Cloud status dashboard

This is a separate, strictly read-only Vercel dashboard. It has no Docker or administrative controls and uses one shared password rather than Supabase Auth users.

Configure these Vercel server-side environment variables:

- `DASHBOARD_PASSWORD_HASH`: a scrypt hash in `scrypt$N$r$p$salt$hash` format (base64url salt and 64-byte hash; minimum parameters `16384`, `8`, `1`).
- `DASHBOARD_SESSION_SECRET`: at least 32 random bytes used to sign one-hour session cookies.
- `SUPABASE_URL`
- `SUPABASE_READONLY_KEY`: the Supabase anon/publishable key. It can execute only `read_node_status`; it has no table privileges.
- `CLOUD_NODE_ID`

Generate the password hash locally without storing the plaintext in the project:

```js
const {randomBytes, scryptSync} = require("node:crypto");
const password = "replace-in-memory-only";
const salt = randomBytes(16);
console.log(`scrypt$16384$8$1$${salt.toString("base64url")}$${scryptSync(password, salt, 64).toString("base64url")}`);
```

Generate the session secret with `openssl rand -base64 48`. Never use the publisher's service-role key in Vercel or browser code. The session cookie is signed, `HttpOnly`, `Secure`, `SameSite=Strict`, and expires after one hour. A small in-process limiter rejects more than five login attempts per source address within 15 minutes.

Run the local tests with:

```sh
npm test
```
