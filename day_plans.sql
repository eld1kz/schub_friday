-- Planning layer: one row per user per day. plan_text is the morning
-- time-blocked plan; review_text is the evening check-in (done / rolled
-- forward), which feeds the next morning's plan as rollover context.
-- Same pattern as automations.sql: RLS enabled, service_role granted.

create table if not exists public.day_plans (
    id          uuid primary key default gen_random_uuid(),
    user_id     text        not null,
    plan_date   date        not null,
    plan_text   text        not null,
    review_text text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (user_id, plan_date)
);

alter table public.day_plans enable row level security;
grant all on public.day_plans to service_role;
