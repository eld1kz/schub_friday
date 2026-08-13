-- Quick-capture inbox + deadline tracker. Same pattern as automations.sql:
-- RLS enabled, service_role granted (the bot uses the service_role key).

-- 1. Inbox: stray ideas/tasks/notes captured from chat, fed into day planning.
create table if not exists public.inbox_items (
    id         uuid primary key default gen_random_uuid(),
    user_id    text        not null,
    text       text        not null,
    kind       text        not null default 'note'
               check (kind in ('idea', 'task', 'note')),
    status     text        not null default 'open'
               check (status in ('open', 'done')),
    created_at timestamptz not null default now()
);
create index if not exists inbox_items_user_status_idx
    on public.inbox_items (user_id, status);

-- 2. Deadlines: one place for everything with a due date (assignments,
--    hackathon registrations, applications). Escalating warnings at
--    7/3/1/0 days; last_warned_days prevents duplicate warnings.
create table if not exists public.deadlines (
    id               uuid primary key default gen_random_uuid(),
    user_id          text        not null,
    title            text        not null,
    due_date         date        not null,
    source           text        not null default 'manual',   -- 'manual' | 'opportunity'
    status           text        not null default 'open'
                     check (status in ('open', 'done')),
    last_warned_days integer,                                  -- 7, 3, 1, or 0
    created_at       timestamptz not null default now()
);
create index if not exists deadlines_user_status_idx
    on public.deadlines (user_id, status, due_date);

alter table public.inbox_items enable row level security;
alter table public.deadlines   enable row level security;
grant all on public.inbox_items to service_role;
grant all on public.deadlines   to service_role;
