-- Daily Apple Health metrics, pushed from the iPhone via an Apple Shortcut.
-- Mirrors the reminders/automations pattern: RLS enabled, service_role granted.
-- The bot connects with the service_role key, so it bypasses RLS; RLS is the
-- lock-down for any anon/authenticated access.
--
-- One row per (user, day). The Shortcut POSTs to /health/ingest each morning;
-- a re-POST for the same day upserts (overwrites) that day's row.

create table if not exists public.health_metrics (
    id                  uuid primary key default gen_random_uuid(),
    user_id             text        not null,           -- Telegram user id (string)
    metric_date         date        not null,           -- the day these metrics describe

    -- readiness / recovery
    hrv                 numeric,                         -- ms (SDNN)
    resting_hr          numeric,                         -- bpm
    respiratory_rate    numeric,                         -- breaths/min
    sleep_hours         numeric,                         -- hours asleep

    -- training load
    active_energy       numeric,                         -- kcal
    exercise_minutes    numeric,                         -- min
    workout_distance_km numeric,                         -- km
    steps               integer,

    -- fitness / body
    vo2max              numeric,                         -- ml/kg/min
    body_weight_kg      numeric,                         -- kg
    blood_oxygen        numeric,                         -- %

    raw                 jsonb       not null default '{}'::jsonb,  -- full payload as sent
    created_at          timestamptz not null default now(),

    unique (user_id, metric_date)
);

create index if not exists health_metrics_user_date_idx
    on public.health_metrics (user_id, metric_date desc);

alter table public.health_metrics enable row level security;

grant all on public.health_metrics to service_role;
