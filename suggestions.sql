-- Phase 2: auto-suggestion loop. Same pattern as automations.sql:
-- RLS enabled, service_role granted. The bot uses the service_role key.

-- 1. Lightweight activity log: one row per USER-initiated tool call.
create table if not exists public.activity_log (
    id         uuid primary key default gen_random_uuid(),
    user_id    text        not null,
    tool       text        not null,
    detail     text,
    created_at timestamptz not null default now()
);
create index if not exists activity_log_user_time_idx
    on public.activity_log (user_id, created_at);

-- 2. Proposed automations awaiting a tap. Dismissed rows stay forever so their
--    pattern_key acts as the do-not-suggest denylist.
create table if not exists public.automation_suggestions (
    id             uuid primary key default gen_random_uuid(),
    user_id        text        not null,
    pattern_key    text        not null,   -- stable signature; denylist key
    rationale      text,                   -- friendly one-liner shown to the user
    description    text        not null,
    trigger_type   text        not null,
    trigger_config jsonb       not null default '{}'::jsonb,
    condition      text,
    action         jsonb       not null,
    tier           text        not null default 'read_only',  -- 'read_only' | 'action'
    status         text        not null default 'pending'
                   check (status in ('pending', 'approved', 'dismissed')),
    created_at     timestamptz not null default now()
);
create index if not exists automation_suggestions_user_status_idx
    on public.automation_suggestions (user_id, status);

-- 3. Per-user toggle for proactive suggestions (absent row = enabled).
create table if not exists public.suggestion_prefs (
    user_id    text primary key,
    enabled    boolean     not null default true,
    updated_at timestamptz not null default now()
);

alter table public.activity_log            enable row level security;
alter table public.automation_suggestions  enable row level security;
alter table public.suggestion_prefs         enable row level security;

grant all on public.activity_log           to service_role;
grant all on public.automation_suggestions to service_role;
grant all on public.suggestion_prefs       to service_role;
