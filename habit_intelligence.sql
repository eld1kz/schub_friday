-- Habit Intelligence: location visits, habit suggestions, watcher rules, nudges.
-- Same deployment pattern as the existing SQL scripts: run this in Supabase SQL editor.

create table if not exists public.user_location_settings (
    user_id                    text primary key,
    tracking_enabled           boolean not null default true,
    nudges_enabled             boolean not null default true,
    habit_suggestions_enabled  boolean not null default true,
    timezone                   text not null default 'Asia/Seoul',
    daily_nudge_limit          integer not null default 3,
    last_location_at           timestamptz,
    created_at                 timestamptz not null default now(),
    updated_at                 timestamptz not null default now()
);

create table if not exists public.location_updates (
    id              uuid primary key default gen_random_uuid(),
    user_id         text not null,
    latitude        double precision not null,
    longitude       double precision not null,
    accuracy_meters double precision,
    received_at     timestamptz not null default now()
);
create index if not exists location_updates_user_time_idx
    on public.location_updates (user_id, received_at desc);

create table if not exists public.place_candidates (
    id                         uuid primary key default gen_random_uuid(),
    user_id                    text not null,
    provider_place_id          text not null,
    place_name                 text not null,
    normalized_brand           text,
    normalized_category        text not null,
    latitude                   double precision not null,
    longitude                  double precision not null,
    first_seen_at              timestamptz not null,
    last_seen_at               timestamptz not null,
    accumulated_dwell_seconds  integer not null default 0,
    status                     text not null default 'candidate'
                               check (status in ('candidate', 'confirmed', 'expired'))
);
create index if not exists place_candidates_user_status_idx
    on public.place_candidates (user_id, status, provider_place_id);

create table if not exists public.place_visits (
    id                  uuid primary key default gen_random_uuid(),
    user_id             text not null,
    provider_place_id   text not null,
    place_name          text not null,
    normalized_brand    text,
    normalized_category text not null,
    arrived_at          timestamptz not null,
    confirmed_at        timestamptz not null,
    last_seen_at        timestamptz not null,
    source              text not null default 'telegram_live_location'
);
create index if not exists place_visits_user_category_time_idx
    on public.place_visits (user_id, normalized_category, confirmed_at desc);

create table if not exists public.category_policies (
    id                   uuid primary key default gen_random_uuid(),
    category             text not null unique,
    policy_kind          text not null check (policy_kind in ('neutral', 'review_if_repeated', 'user_goal_only')),
    default_window_days  integer,
    default_threshold    integer,
    suggestion_template  text,
    source               text not null default 'seeded'
                         check (source in ('seeded', 'llm_classified', 'manually_configured')),
    confidence           double precision not null default 1.0,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

create table if not exists public.place_classification_cache (
    id                     uuid primary key default gen_random_uuid(),
    provider_place_id      text not null unique,
    place_name             text not null,
    normalized_brand       text,
    normalized_category    text not null,
    classification_source  text not null check (classification_source in ('local_mapping', 'places_api', 'llm_fallback')),
    confidence             double precision not null default 0.0,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now()
);

create table if not exists public.watcher_rules (
    id                uuid primary key default gen_random_uuid(),
    user_id           text not null,
    rule_type         text not null check (rule_type in (
                       'weekly_visit_limit', 'rolling_window_limit', 'near_category_reminder',
                       'near_place_reminder', 'inactivity_goal')),
    target_category   text,
    target_brand      text,
    target_place_id   text,
    threshold_count   integer,
    window_days       integer,
    reminder_text     text,
    is_active         boolean not null default true,
    cooldown_hours    integer not null default 24,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index if not exists watcher_rules_user_active_idx
    on public.watcher_rules (user_id, is_active);

create table if not exists public.habit_suggestions (
    id             uuid primary key default gen_random_uuid(),
    user_id        text not null,
    category       text not null,
    period_key     text not null,
    observed_count integer not null,
    status         text not null default 'pending'
                   check (status in ('pending', 'accepted', 'dismissed', 'muted')),
    suggested_at   timestamptz not null default now(),
    responded_at   timestamptz
);
create index if not exists habit_suggestions_user_category_idx
    on public.habit_suggestions (user_id, category, suggested_at desc);

create table if not exists public.nudge_history (
    id                  uuid primary key default gen_random_uuid(),
    user_id             text not null,
    watcher_rule_id     uuid,
    habit_suggestion_id uuid,
    place_visit_id      uuid,
    message             text not null,
    deduplication_key   text not null unique,
    sent_at             timestamptz not null default now()
);
create index if not exists nudge_history_user_time_idx
    on public.nudge_history (user_id, sent_at desc);

alter table public.user_location_settings enable row level security;
alter table public.location_updates enable row level security;
alter table public.place_candidates enable row level security;
alter table public.place_visits enable row level security;
alter table public.category_policies enable row level security;
alter table public.place_classification_cache enable row level security;
alter table public.watcher_rules enable row level security;
alter table public.habit_suggestions enable row level security;
alter table public.nudge_history enable row level security;

grant all on public.user_location_settings to service_role;
grant all on public.location_updates to service_role;
grant all on public.place_candidates to service_role;
grant all on public.place_visits to service_role;
grant all on public.category_policies to service_role;
grant all on public.place_classification_cache to service_role;
grant all on public.watcher_rules to service_role;
grant all on public.habit_suggestions to service_role;
grant all on public.nudge_history to service_role;
