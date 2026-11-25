-- Zurich events: run this once in Supabase → SQL Editor → New query → Run.

create table if not exists sources (
  id            text primary key,
  name          text not null,
  category      text not null,
  url           text not null,
  kind          text not null default 'page',
  last_checked  timestamptz,
  last_ok       timestamptz,
  last_error    text,
  fingerprint   text,
  upcoming      int default 0
);

create table if not exists events (
  id                     text primary key,          -- hash of source + title + start
  source_id              text references sources(id) on delete cascade,
  kind                   text not null default 'event', -- 'event' | 'offer'
  category               text not null,
  title                  text not null,
  start_at               timestamptz,
  end_at                 timestamptz,
  all_day                boolean default false,
  venue                  text,
  price_min              numeric,                   -- CHF; 0 = free; null = not listed
  price_max              numeric,
  price_text             text,
  registration_deadline  timestamptz,
  url                    text,
  summary                text,
  first_seen             timestamptz default now(),
  last_seen              timestamptz default now()
);
create index if not exists events_start_idx on events (start_at);

create table if not exists swipes (
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  event_id   text not null references events(id) on delete cascade,
  decision   text not null check (decision in ('saved','discarded')),
  at         timestamptz default now(),
  primary key (user_id, event_id)
);

create table if not exists prefs (
  user_id            uuid primary key default auth.uid() references auth.users(id) on delete cascade,
  hidden_sources     text[] default '{}',
  hidden_categories  text[] default '{}',
  price_max          numeric,          -- null = no limit
  show_unpriced      boolean default true,
  updated_at         timestamptz default now()
);

create table if not exists runs (
  id              bigint generated always as identity primary key,
  started_at      timestamptz default now(),
  finished_at     timestamptz,
  sources_ok      int,
  sources_failed  int,
  new_events      int,
  notes           text
);

-- Row level security: events, sources and runs are readable by anyone with the
-- site's public key and writable only by the daily job (service role bypasses RLS).
-- Swipes and preferences belong to the signed-in user only.
alter table sources enable row level security;
alter table events  enable row level security;
alter table runs    enable row level security;
alter table swipes  enable row level security;
alter table prefs   enable row level security;

drop policy if exists "read sources" on sources;
create policy "read sources" on sources for select using (true);
drop policy if exists "read events" on events;
create policy "read events" on events for select using (true);
drop policy if exists "read runs" on runs;
create policy "read runs" on runs for select using (true);

drop policy if exists "own swipes" on swipes;
create policy "own swipes" on swipes for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "own prefs" on prefs;
create policy "own prefs" on prefs for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- API access (RLS above still decides which rows each role sees)
grant usage on schema public to anon, authenticated, service_role;
grant select on sources, events, runs to anon, authenticated;
grant select, insert, update, delete on swipes, prefs to authenticated;
grant all on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;

-- v2: event status, detail pass bookkeeping, ticket links, "from" date
alter table events add column if not exists status text default 'unknown';
alter table events add column if not exists checked_at timestamptz;
alter table events add column if not exists ticket_url text;
alter table prefs  add column if not exists from_date date;
