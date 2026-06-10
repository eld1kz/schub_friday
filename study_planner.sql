-- Smart Study/Dev Planner: weekly plans, pipeline tasks, study logs, learning profile.
-- Same deployment pattern as the existing SQL scripts: run this in Supabase SQL editor.

create table if not exists public.study_plans (
    id          uuid primary key default gen_random_uuid(),
    user_id     text not null,
    title       text not null,
    focus       text,
    milestones  jsonb not null default '[]',
    tips        jsonb not null default '[]',
    status      text not null default 'active'
                check (status in ('active', 'archived')),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists study_plans_user_status_idx
    on public.study_plans (user_id, status, created_at desc);

create table if not exists public.study_tasks (
    id           uuid primary key default gen_random_uuid(),
    plan_id      uuid not null references public.study_plans (id) on delete cascade,
    user_id      text not null,
    course       text not null,
    title        text not null,
    day          text,
    est_minutes  integer,
    priority     integer not null default 2,
    hook         text,
    status       text not null default 'idea'
                 check (status in ('idea', 'researched', 'coded', 'tested', 'reviewed', 'done')),
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);
create index if not exists study_tasks_plan_idx
    on public.study_tasks (plan_id, priority);
create index if not exists study_tasks_user_status_idx
    on public.study_tasks (user_id, status);

create table if not exists public.study_logs (
    id        uuid primary key default gen_random_uuid(),
    user_id   text not null,
    course    text,
    task_ref  text,
    metric    text,
    worked    text,
    failed    text,
    mood      text,
    raw_text  text not null,
    logged_at timestamptz not null default now()
);
create index if not exists study_logs_user_time_idx
    on public.study_logs (user_id, logged_at desc);

create table if not exists public.learning_profile (
    id             uuid primary key default gen_random_uuid(),
    user_id        text not null,
    kind           text not null check (kind in ('technique', 'strength', 'weakness', 'schedule')),
    insight        text not null,
    confidence     double precision not null default 0.5,
    is_active      boolean not null default true,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);
create index if not exists learning_profile_user_active_idx
    on public.learning_profile (user_id, is_active);

alter table public.study_plans enable row level security;
alter table public.study_tasks enable row level security;
alter table public.study_logs enable row level security;
alter table public.learning_profile enable row level security;

grant all on public.study_plans to service_role;
grant all on public.study_tasks to service_role;
grant all on public.study_logs to service_role;
grant all on public.learning_profile to service_role;
