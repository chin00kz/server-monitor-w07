create table if not exists public.node_status (
  node_id text primary key check (node_id ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
  observed_at timestamptz,
  received_at timestamptz not null default now(),
  payload jsonb not null check (jsonb_typeof(payload) = 'object')
);

create or replace function public.touch_node_status_received_at()
returns trigger
language plpgsql
security invoker
as $$
begin
  new.received_at = now();
  return new;
end;
$$;

drop trigger if exists node_status_received_at on public.node_status;
create trigger node_status_received_at
before insert or update on public.node_status
for each row execute function public.touch_node_status_received_at();

alter table public.node_status enable row level security;

drop policy if exists node_status_team_read on public.node_status;
revoke all on public.node_status from anon;
revoke all on public.node_status from authenticated;
grant select, insert, update on public.node_status to service_role;

create or replace function public.read_node_status(requested_node_id text)
returns table (node_id text, observed_at timestamptz, received_at timestamptz, payload jsonb)
language sql
stable
security definer
set search_path = ''
as $$
  select ns.node_id, ns.observed_at, ns.received_at, ns.payload
  from public.node_status as ns
  where ns.node_id = requested_node_id
  limit 1
$$;

revoke all on function public.read_node_status(text) from public;
revoke all on function public.read_node_status(text) from authenticated;
revoke all on function public.read_node_status(text) from service_role;
grant usage on schema public to anon;
grant execute on function public.read_node_status(text) to anon;
