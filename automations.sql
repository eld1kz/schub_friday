-- Automations ("recipes"): user-defined trigger -> action rules.
-- Mirrors the reminders/memories pattern: RLS enabled, service_role granted.
-- The bot connects with the service_role key, so it bypasses RLS; RLS is the
-- lock-down for any anon/authenticated access.

create table if not exists public.automations (
    id             uuid primary key default gen_random_uuid(),
    user_id        text        not null,                 -- Telegram user id (string)
    description    text        not null,                 -- original plain-language rule
    trigger_type   text        not null
                   check (trigger_type in ('schedule', 'event', 'keyword')),
    trigger_config jsonb       not null default '{}'::jsonb,  -- cron/time | event | phrase
    condition      text,                                 -- optional natural-language condition
    action         jsonb       not null,                 -- {"tool": "...", "input": {...}}
    enabled        boolean     not null default true,
    last_run       timestamptz,
    created_at     timestamptz not null default now()
);

create index if not exists automations_user_id_idx      on public.automations (user_id);
create index if not exists automations_trigger_type_idx on public.automations (trigger_type) where enabled;

alter table public.automations enable row level security;

grant all on public.automations to service_role;
