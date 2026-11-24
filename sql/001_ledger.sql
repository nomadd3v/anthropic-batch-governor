-- ───────────────────────────────────────────────────────────────────────────
-- Migration:   001_ledger.sql
-- Date:        2026-04-17
-- Purpose:     Create the `governor` schema that holds the batch-governor
--              audit ledger: every pre-submit decision and every batch
--              lifecycle row. Both tables are service-role-only under RLS.
--
-- Reversibility: fully reversible via
--   drop schema governor cascade;
-- The schema is self-contained — no foreign keys leave it, no other schema
-- depends on it. Dropping it loses audit history only.
--
-- Why Supabase (and not Upstash): see ADR-003. Audit rows must outlive TTLs
-- and be queryable with SQL for variance analysis.
-- ───────────────────────────────────────────────────────────────────────────

create schema if not exists governor;

-- ─── governor.decisions ────────────────────────────────────────────────────
-- One row per POST /v1/check call. Captures the approve/deny verdict, the
-- structured reasons on denial, the projected cost at approval time, and the
-- fingerprint so duplicate-submit protection can be audited after the fact.
create table if not exists governor.decisions (
  id                    bigserial primary key,
  ts                    timestamptz not null default now(),
  app                   text not null,
  approval_id           uuid unique,
  decision              text not null check (decision in ('APPROVED', 'DENIED')),
  reasons               jsonb,
  fingerprint           text,
  items                 int,
  model                 text,
  projected_p50_usd     numeric(10, 4),
  projected_p95_usd     numeric(10, 4),
  actor                 text
);

-- ─── governor.batches ──────────────────────────────────────────────────────
-- One row per batch that was actually submitted to Anthropic. Updated by the
-- monitor as the batch progresses (status, running_usd) and again at end of
-- life (actual_usd, variance_pct). `approval_id` links back to the decision
-- row that authorised the submit.
create table if not exists governor.batches (
  batch_id              text primary key,
  approval_id           uuid references governor.decisions(approval_id),
  app                   text not null,
  model                 text,
  submitted_at          timestamptz default now(),
  ended_at              timestamptz,
  status                text check (status in (
                          'submitted', 'in_progress', 'ended',
                          'canceled', 'killed', 'rogue'
                        )),
  running_usd           numeric(10, 4),
  actual_usd            numeric(10, 4),
  variance_pct          numeric(6, 2),
  kill_reason           text
);

-- Indexes support the two most common operator queries: "show me what app X
-- did recently" and "show me recent batches for app X". Both are descending
-- by time because operators always ask "what happened lately?".
create index if not exists decisions_app_ts_idx
  on governor.decisions (app, ts desc);

create index if not exists batches_app_submitted_idx
  on governor.batches (app, submitted_at desc);

-- ─── Row Level Security ────────────────────────────────────────────────────
-- BUSINESS RULE: Only the service role may read or write the ledger. Anon
-- users and authenticated users get nothing; dashboard access is a future
-- addition that must go through signed JWTs (see ADR-003).
alter table governor.decisions enable row level security;
alter table governor.batches   enable row level security;

drop policy if exists service_all_decisions on governor.decisions;
create policy service_all_decisions
  on governor.decisions
  for all
  using (auth.role() = 'service_role');

drop policy if exists service_all_batches on governor.batches;
create policy service_all_batches
  on governor.batches
  for all
  using (auth.role() = 'service_role');

-- ─── PostgREST exposure + service_role grants ──────────────────────────────
-- Without these, supabase-py writes fail with PGRST106 ("Invalid schema") or
-- 42501 ("permission denied"). The dashboard's Data API settings page stores
-- the same `pgrst.db_schemas` value, so setting it in SQL converges with the
-- UI. Grants are mandatory regardless of where exposure is managed.
grant usage on schema governor to service_role;
grant all on all tables    in schema governor to service_role;
grant all on all sequences in schema governor to service_role;
alter default privileges in schema governor grant all on tables    to service_role;
alter default privileges in schema governor grant all on sequences to service_role;

-- If the `governor` schema is NOT already in the authenticator role's
-- `pgrst.db_schemas` list, append it. This mirrors the dashboard's
-- Settings → Integrations → Data API → Exposed Schemas form.
do $$
declare
  current_list text;
begin
  select coalesce(
    (select s from unnest(rolconfig) s where s like 'pgrst.db_schemas=%' limit 1),
    'pgrst.db_schemas=public'
  ) into current_list
  from pg_roles where rolname = 'authenticator';

  if position('governor' in current_list) = 0 then
    execute format(
      'alter role authenticator set pgrst.db_schemas = %L',
      trim(both from replace(current_list, 'pgrst.db_schemas=', '')) || ', governor'
    );
  end if;
end$$;

-- Force PostgREST to reload its config + schema cache so the new schema,
-- tables, and grants all become visible without a project restart.
notify pgrst, 'reload config';
notify pgrst, 'reload schema';
