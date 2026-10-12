# Cloud status dashboard

This is a separate, read-only Vercel dashboard. It does not share the local dashboard frontend and has no Docker or administrative controls.

Configure Vercel server-side variables:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `CLOUD_NODE_ID`

Create individual users in Supabase Auth and add their UUIDs to `public.dashboard_members`. The Vercel API validates the Supabase session before querying the RLS-protected `node_status` row.

Run the local tests with:

```sh
npm test
```
