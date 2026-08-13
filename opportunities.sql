-- Opportunity scout: hackathons, internships, programs found by scheduled
-- web-search scans. Same pattern as automations.sql: RLS enabled,
-- service_role granted (the bot uses the service_role key).
-- Rows are kept forever so the url acts as the already-seen dedupe key.

create table if not exists public.opportunities (
    id         uuid primary key default gen_random_uuid(),
    user_id    text        not null,
    kind       text        not null
               check (kind in ('hackathon', 'internship', 'program')),
    title      text        not null,
    url        text        not null,
    deadline   date,                     -- application/registration deadline, if known
    location   text,                     -- e.g. 'Seoul', 'online', 'San Francisco (global)'
    summary    text,                     -- one-two lines: what it is, why it fits
    created_at timestamptz not null default now()
);
create unique index if not exists opportunities_user_url_idx
    on public.opportunities (user_id, url);

alter table public.opportunities enable row level security;
grant all on public.opportunities to service_role;
