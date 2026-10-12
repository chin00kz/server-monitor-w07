create table if not exists public.dashboard_members (
  user_id uuid primary key references auth.users(id) on delete cascade
);

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

alter table public.dashboard_members enable row level security;
alter table public.node_status enable row level security;

drop policy if exists dashboard_members_self_read on public.dashboard_members;
create policy dashboard_members_self_read
on public.dashboard_members for select
to authenticated
using (user_id = auth.uid());

drop policy if exists node_status_team_read on public.node_status;
create policy node_status_team_read
on public.node_status for select
to authenticated
using (exists (
  select 1 from public.dashboard_members
  where user_id = auth.uid()
));

revoke all on public.node_status from anon;
revoke all on public.node_status from authenticated;
grant select on public.node_status to authenticated;
revoke all on public.dashboard_members from anon;
revoke all on public.dashboard_members from authenticated;
grant select on public.dashboard_members to authenticated;
