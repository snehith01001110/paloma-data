import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.7";

type Sql = ReturnType<typeof postgres>;

const SOURCE_LABELS: Record<string, string> = {
  ca_abc: "California ABC",
  fsq: "Foursquare OS",
  datasf: "DataSF registrations",
  datasf_neighborhoods: "SF civic neighborhoods",
  overture: "Overture (optional)",
};

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Cache-Control": "no-store",
};

function number(value: unknown): number {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

function boundedInt(value: string | null, fallback: number, min: number, max: number): number {
  const parsed = Number(value ?? fallback);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(parsed)));
}

function run(row: Record<string, unknown> | undefined) {
  if (!row) return null;
  return {
    id: row.id,
    source: row.source,
    label: SOURCE_LABELS[String(row.source)] ?? row.source,
    mode: row.mode,
    status: row.status,
    fetched: number(row.fetched_count),
    created: number(row.created_count),
    updated: number(row.updated_count),
    unchanged: number(row.unchanged_count),
    closed: number(row.closed_count),
    started_at: row.started_at,
    finished_at: row.finished_at,
    error: row.error_summary || null,
  };
}

Deno.serve(async (request: Request) => {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }
  if (request.method !== "GET") {
    return Response.json({ error: "method_not_allowed" }, { status: 405, headers: corsHeaders });
  }

  const databaseUrl = Deno.env.get("SUPABASE_DB_URL");
  if (!databaseUrl) {
    return Response.json({ error: "database_unavailable" }, { status: 500, headers: corsHeaders });
  }

  const url = new URL(request.url);
  const view = url.searchParams.get("view");
  const q = (url.searchParams.get("q") ?? "").trim().slice(0, 100);
  const limit = boundedInt(url.searchParams.get("limit"), 24, 1, 50);
  const offset = boundedInt(url.searchParams.get("offset"), 0, 0, 10000);
  const pattern = `%${q}%`;

  const sql = postgres(databaseUrl, {
    max: 1,
    idle_timeout: 5,
    connect_timeout: 5,
    prepare: false,
  });

  try {
    const schemaRows = await sql`
      select
        to_regclass('ingest.catalog_candidates') is not null as migration_applied,
        to_regclass('ingest.source_sync_state') is not null as sync_state_applied
    `;
    const migrationApplied = Boolean(schemaRows[0]?.migration_applied);
    if (!migrationApplied) {
      const payload = await legacyPayload(sql);
      return Response.json(payload, {
        headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8" },
      });
    }

    const payload: Record<string, unknown> = await v2Payload(sql);
    if (view === "live") {
      const [countRows, items] = await Promise.all([
        sql`
          select count(*)::bigint as total
          from public.establishments e
          where e.publication_state = 'published'
            and e.status = 'open'
            and e.catalog_candidate_id is not null
            and e.verification_expires_at > now()
            and (
              ${q === ""} or e.name ilike ${pattern} or e.address ilike ${pattern}
              or e.city ilike ${pattern} or coalesce(e.neighborhood, '') ilike ${pattern}
            )
        `,
        sql`
          select e.id::text, e.name, e.address, e.city, e.region, e.neighborhood,
                 e.phone_e164, e.website_url, e.hours is not null as has_hours,
                 e.price_level is not null as has_price,
                 e.verification_tier, e.last_verified_at, e.verification_expires_at,
                 pt.slug as primary_type_slug, pt.name as primary_type_name,
                 exists (
                   select 1 from public.establishment_settings es
                   where es.establishment_id = e.id
                 ) as has_settings
          from public.establishments e
          left join public.primary_types pt on pt.id = e.primary_type_id
          where e.publication_state = 'published'
            and e.status = 'open'
            and e.catalog_candidate_id is not null
            and e.verification_expires_at > now()
            and (
              ${q === ""} or e.name ilike ${pattern} or e.address ilike ${pattern}
              or e.city ilike ${pattern} or coalesce(e.neighborhood, '') ilike ${pattern}
            )
          order by e.name, e.id
          limit ${limit} offset ${offset}
        `,
      ]);
      payload.list = {
        view: "live",
        total: number(countRows[0]?.total),
        offset,
        limit,
        items,
      };
    }

    return Response.json(payload, {
      headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8" },
    });
  } catch (error) {
    console.error("paloma-data-progress", error);
    return Response.json({ error: "progress_query_failed" }, { status: 500, headers: corsHeaders });
  } finally {
    await sql.end({ timeout: 1 });
  }
});

async function legacyPayload(sql: Sql) {
  const [publicRows, latestRunRows] = await Promise.all([
    sql`
      select count(*)::bigint as rows_total,
             count(*) filter (
               where publication_state = 'published' and status = 'open'
             )::bigint as app_visible
      from public.establishments
    `,
    sql`
      select distinct on (source)
        id::text, source, mode, status, fetched_count, created_count, updated_count,
        unchanged_count, closed_count, started_at, finished_at,
        left(coalesce(error_summary, ''), 220) as error_summary
      from ingest.ingestion_runs
      order by source, started_at desc
    `,
  ]);
  const overall = publicRows[0] ?? {};
  return {
    schema_version: "legacy",
    decision_version: null,
    generated_at: new Date().toISOString(),
    readiness: {
      migration_applied: false,
      cutover_complete: false,
      safe_for_users: false,
      next_action: "Apply the additive catalog v2 migration; do not trust or rebuild the legacy catalog.",
    },
    overall: {
      rows_total: number(overall.rows_total),
      app_visible: number(overall.app_visible),
      safe_live: 0,
      unsafe_legacy_live: number(overall.app_visible),
      unsafe_expired_live: 0,
    },
    sources: latestRunRows.map((row) => ({
      source: String(row.source),
      label: SOURCE_LABELS[String(row.source)] ?? row.source,
      required: ["ca_abc", "fsq"].includes(String(row.source)),
      record_count: 0,
      open_record_count: 0,
      completed_at: null,
      latest_run: run(row),
    })),
    candidates: [],
    blockers: [],
    verification_tiers: [],
    coverage: {},
    work_queue: {},
    published_types: [],
    published_cities: [],
    recent_runs: latestRunRows.map((row) => run(row)),
  };
}

async function v2Payload(sql: Sql) {
  const [
    overallRows,
    candidateRows,
    blockerRows,
    workRows,
    sourceRows,
    runRows,
    tierRows,
    typeRows,
    cityRows,
  ] = await Promise.all([
    sql`
      select
        count(*)::bigint as rows_total,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
        )::bigint as app_visible,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null and verification_expires_at > now()
        )::bigint as safe_live,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is null
        )::bigint as unsafe_legacy_live,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null
            and (verification_expires_at is null or verification_expires_at <= now())
        )::bigint as unsafe_expired_live,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null and verification_expires_at > now()
            and phone_e164 is not null
        )::bigint as phone,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null and verification_expires_at > now()
            and website_url is not null
        )::bigint as website,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null and verification_expires_at > now()
            and neighborhood is not null
        )::bigint as neighborhood,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null and verification_expires_at > now()
            and hours is not null
        )::bigint as hours,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null and verification_expires_at > now()
            and price_level is not null
        )::bigint as price,
        count(*) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null and verification_expires_at > now()
            and exists (
              select 1 from public.establishment_settings es
              where es.establishment_id = establishments.id
            )
        )::bigint as settings,
        min(verification_expires_at) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null and verification_expires_at > now()
        ) as next_expiry,
        max(last_verified_at) filter (
          where publication_state = 'published' and status = 'open'
            and catalog_candidate_id is not null
        ) as last_verified_at
      from public.establishments
    `,
    sql`
      select candidate_state as state, count(*)::bigint as count
      from ingest.catalog_candidates
      group by candidate_state
      order by candidate_state
    `,
    sql`
      select decision_reason as reason, count(*)::bigint as count
      from ingest.catalog_candidates
      where candidate_state not in ('verified', 'published')
      group by decision_reason
      order by count(*) desc, decision_reason
      limit 12
    `,
    sql`
      select
        (select count(*) from ingest.candidate_match_reviews where state = 'pending')::bigint
          as pending_match_reviews,
        count(*) filter (where candidate_state = 'verified')::bigint as ready_to_publish,
        count(*) filter (
          where candidate_state in ('needs_verification', 'needs_review')
             or (
               candidate_state in ('verified', 'published')
               and (verification_expires_at is null
                    or verification_expires_at <= now() + interval '14 days')
             )
        )::bigint as verification_due,
        count(*) filter (
          where candidate_state in ('verified', 'published')
            and verification_expires_at > now()
        )::bigint as unexpired_verified
      from ingest.catalog_candidates
    `,
    sql`
      with expected(source, label, required, freshness_days) as (
        values
          ('ca_abc', 'California ABC', true, 7),
          ('fsq', 'Foursquare OS', true, 45),
          ('datasf', 'DataSF registrations', false, 7),
          ('datasf_neighborhoods', 'SF civic neighborhoods', false, 45),
          ('overture', 'Overture (optional)', false, 45)
      ), latest as (
        select distinct on (source)
          id::text, source, mode, status, fetched_count, created_count, updated_count,
          unchanged_count, closed_count, started_at, finished_at,
          left(coalesce(error_summary, ''), 220) as error_summary
        from ingest.ingestion_runs
        order by source, started_at desc
      ), metrics as (
        select source,
               count(*) filter (where retired_at is null)::bigint as record_count,
               count(*) filter (
                 where retired_at is null and source_status = 'open'
               )::bigint as open_record_count
        from ingest.source_records
        group by source
      )
      select e.source, e.label, e.required, e.freshness_days,
             coalesce(m.record_count, s.record_count, 0)::bigint as record_count,
             coalesce(m.open_record_count, 0)::bigint as open_record_count,
             s.completed_at, s.release_id,
             l.id, l.mode, l.status, l.fetched_count, l.created_count, l.updated_count,
             l.unchanged_count, l.closed_count, l.started_at, l.finished_at, l.error_summary
      from expected e
      left join ingest.source_sync_state s on s.source = e.source
      left join metrics m on m.source = e.source
      left join latest l on l.source = e.source
      order by e.required desc, e.source
    `,
    sql`
      select id::text, source, mode, status, fetched_count, created_count, updated_count,
             unchanged_count, closed_count, started_at, finished_at,
             left(coalesce(error_summary, ''), 220) as error_summary
      from ingest.ingestion_runs
      order by started_at desc
      limit 18
    `,
    sql`
      select verification_tier as tier, count(*)::bigint as count
      from public.establishments
      where publication_state = 'published' and status = 'open'
        and catalog_candidate_id is not null and verification_expires_at > now()
      group by verification_tier
      order by count(*) desc, verification_tier
    `,
    sql`
      select pt.slug, pt.name, count(*)::bigint as count
      from public.establishments e
      join public.primary_types pt on pt.id = e.primary_type_id
      where e.publication_state = 'published' and e.status = 'open'
        and e.catalog_candidate_id is not null and e.verification_expires_at > now()
      group by pt.slug, pt.name
      order by count(*) desc, pt.name
    `,
    sql`
      select city, count(*)::bigint as count
      from public.establishments
      where publication_state = 'published' and status = 'open'
        and catalog_candidate_id is not null and verification_expires_at > now()
      group by city
      order by count(*) desc, city
      limit 16
    `,
  ]);

  const overall = overallRows[0] ?? {};
  const safeLive = number(overall.safe_live);
  const unsafeLegacy = number(overall.unsafe_legacy_live);
  const unsafeExpired = number(overall.unsafe_expired_live);
  const sources = sourceRows.map((row) => ({
    source: String(row.source),
    label: row.label,
    required: Boolean(row.required),
    freshness_days: number(row.freshness_days),
    record_count: number(row.record_count),
    open_record_count: number(row.open_record_count),
    completed_at: row.completed_at,
    release_id: row.release_id,
    latest_run: row.id ? run(row) : null,
    fresh: Boolean(
      row.completed_at &&
      Date.now() - new Date(String(row.completed_at)).getTime() <=
        number(row.freshness_days) * 86_400_000
    ),
  }));
  const requiredReady = sources
    .filter((source) => source.required)
    .every((source) => source.fresh && source.record_count > 0);
  const cutoverComplete = unsafeLegacy === 0;
  const safeForUsers = cutoverComplete && unsafeExpired === 0 && requiredReady;

  return {
    schema_version: "catalog_v2",
    decision_version: "v2",
    generated_at: new Date().toISOString(),
    readiness: {
      migration_applied: true,
      cutover_complete: cutoverComplete,
      required_sources_ready: requiredReady,
      safe_for_users: safeForUsers,
      next_action: nextAction({ unsafeLegacy, unsafeExpired, requiredReady, safeLive, work: workRows[0] }),
    },
    overall: {
      rows_total: number(overall.rows_total),
      app_visible: number(overall.app_visible),
      safe_live: safeLive,
      unsafe_legacy_live: unsafeLegacy,
      unsafe_expired_live: unsafeExpired,
      next_expiry: overall.next_expiry,
      last_verified_at: overall.last_verified_at,
    },
    coverage: {
      denominator: safeLive,
      phone: number(overall.phone),
      website: number(overall.website),
      neighborhood: number(overall.neighborhood),
      hours: number(overall.hours),
      price: number(overall.price),
      settings: number(overall.settings),
    },
    candidates: candidateRows.map((row) => ({ state: row.state, count: number(row.count) })),
    blockers: blockerRows.map((row) => ({ reason: row.reason, count: number(row.count) })),
    work_queue: {
      pending_match_reviews: number(workRows[0]?.pending_match_reviews),
      ready_to_publish: number(workRows[0]?.ready_to_publish),
      verification_due: number(workRows[0]?.verification_due),
      unexpired_verified: number(workRows[0]?.unexpired_verified),
    },
    sources,
    verification_tiers: tierRows.map((row) => ({ tier: row.tier, count: number(row.count) })),
    published_types: typeRows.map((row) => ({
      slug: row.slug,
      name: row.name,
      count: number(row.count),
    })),
    published_cities: cityRows.map((row) => ({ city: row.city, count: number(row.count) })),
    recent_runs: runRows.map((row) => run(row)),
  };
}

function nextAction(args: {
  unsafeLegacy: number;
  unsafeExpired: number;
  requiredReady: boolean;
  safeLive: number;
  work: Record<string, unknown> | undefined;
}): string {
  if (!args.requiredReady) return "Complete successful California ABC and Foursquare OS snapshots.";
  if (args.unsafeExpired > 0) return "Run catalog-sweep immediately; expired rows are still app-visible.";
  if (args.unsafeLegacy > 0) return "Run a bounded trial, approve it, then perform the one-time verified cutover.";
  if (number(args.work?.ready_to_publish) > 0) return "Review the ready set, then publish only verified candidates.";
  if (number(args.work?.pending_match_reviews) > 0) return "Resolve the pending identity conflicts; they remain hidden meanwhile.";
  if (args.safeLive === 0) return "No venue currently passes every hard gate; leave the app catalog empty.";
  return "No urgent action. Keep source snapshots and verification leases current.";
}
