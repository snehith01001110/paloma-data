import "jsr:@supabase/functions-js@2.5.0/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.7";

type Sql = ReturnType<typeof postgres>;

const SOURCE_LABELS: Record<string, string> = {
  ca_abc: "California ABC",
  fsq: "Foursquare OS",
  datasf: "DataSF registrations",
  datasf_neighborhoods: "SF civic neighborhoods",
  overture: "Overture (optional)",
};

const PROVIDER_META: Record<string, { label: string; detail_policy: string }> =
  {
    foursquare: {
      label: "Foursquare",
      detail_policy: "On demand · never server-cached",
    },
    yelp: {
      label: "Yelp",
      detail_policy: "On demand · shared cache up to 22 hours",
    },
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

function boundedInt(
  value: string | null,
  fallback: number,
  min: number,
  max: number,
): number {
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
    return Response.json({ error: "method_not_allowed" }, {
      status: 405,
      headers: corsHeaders,
    });
  }

  const databaseUrl = Deno.env.get("SUPABASE_DB_URL");
  if (!databaseUrl) {
    return Response.json({ error: "database_unavailable" }, {
      status: 500,
      headers: corsHeaders,
    });
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
        to_regclass('ingest.source_sync_state') is not null as sync_state_applied,
        to_regclass('runtime.runtime_provider_links') is not null
          and to_regclass('runtime.provider_match_state') is not null
          and to_regclass('runtime.provider_response_cache') is not null
          and to_regclass('runtime.provider_refresh_leases') is not null
          as runtime_applied
    `;
    const migrationApplied = Boolean(schemaRows[0]?.migration_applied);
    const runtimeApplied = Boolean(schemaRows[0]?.runtime_applied);
    if (!migrationApplied) {
      const payload = await legacyPayload(sql);
      return Response.json(payload, {
        headers: {
          ...corsHeaders,
          "Content-Type": "application/json; charset=utf-8",
        },
      });
    }

    const payload: Record<string, unknown> = await v2Payload(
      sql,
      runtimeApplied,
    );
    if (view === "live") {
      const [countRows, items] = await liveCatalogRows(
        sql,
        { q, pattern, limit, offset },
        runtimeApplied,
      );
      payload.list = {
        view: "live",
        total: number(countRows[0]?.total),
        offset,
        limit,
        items,
      };
    }

    return Response.json(payload, {
      headers: {
        ...corsHeaders,
        "Content-Type": "application/json; charset=utf-8",
      },
    });
  } catch (error) {
    console.error("paloma-data-progress", error);
    return Response.json({ error: "progress_query_failed" }, {
      status: 500,
      headers: corsHeaders,
    });
  } finally {
    await sql.end({ timeout: 1 });
  }
});

async function liveCatalogRows(
  sql: Sql,
  args: { q: string; pattern: string; limit: number; offset: number },
  runtimeApplied: boolean,
) {
  const countQuery = sql`
    select count(*)::bigint as total
    from public.establishments e
    join ingest.catalog_candidates c on c.id = e.catalog_candidate_id
    where e.publication_state = 'published'
      and e.status = 'open'
      and e.access_mode = 'walk_in'
      and e.verification_tier in ('open_evidence', 'provider', 'manual')
      and e.verification_expires_at > now()
      and c.candidate_state = 'published'
      and c.identity_confidence >= 0.96
      and c.decision_version = e.verification_version
      and (
        ${args.q === ""} or e.name ilike ${args.pattern}
        or e.address ilike ${args.pattern}
        or e.city ilike ${args.pattern}
        or coalesce(e.neighborhood, '') ilike ${args.pattern}
      )
  `;

  if (!runtimeApplied) {
    return await Promise.all([
      countQuery,
      sql`
        select e.id::text, e.name, e.address, e.city, e.region, e.neighborhood,
               e.phone_e164, e.website_url, e.hours is not null as has_hours,
               e.price_level is not null as has_price,
               e.access_mode, e.verification_tier, e.last_verified_at,
               e.verification_expires_at,
               pt.slug as primary_type_slug, pt.name as primary_type_name,
               exists (
                 select 1 from public.establishment_settings es
                 where es.establishment_id = e.id
               ) as has_settings,
               array[]::text[] as live_providers,
               false as has_warm_cache
        from public.establishments e
        join ingest.catalog_candidates c on c.id = e.catalog_candidate_id
        left join public.primary_types pt on pt.id = e.primary_type_id
        where e.publication_state = 'published'
          and e.status = 'open'
          and e.access_mode = 'walk_in'
          and e.verification_tier in ('open_evidence', 'provider', 'manual')
          and e.verification_expires_at > now()
          and c.candidate_state = 'published'
          and c.identity_confidence >= 0.96
          and c.decision_version = e.verification_version
          and (
            ${args.q === ""} or e.name ilike ${args.pattern}
            or e.address ilike ${args.pattern}
            or e.city ilike ${args.pattern}
            or coalesce(e.neighborhood, '') ilike ${args.pattern}
          )
        order by e.name, e.id
        limit ${args.limit} offset ${args.offset}
      `,
    ]);
  }

  return await Promise.all([
    countQuery,
    sql`
      select e.id::text, e.name, e.address, e.city, e.region, e.neighborhood,
             e.phone_e164, e.website_url, e.hours is not null as has_hours,
             e.price_level is not null as has_price,
             e.access_mode, e.verification_tier, e.last_verified_at,
             e.verification_expires_at,
             pt.slug as primary_type_slug, pt.name as primary_type_name,
             exists (
               select 1 from public.establishment_settings es
               where es.establishment_id = e.id
             ) as has_settings,
             coalesce(provider_links.providers, array[]::text[]) as live_providers,
             coalesce(provider_links.has_warm_cache, false) as has_warm_cache
      from public.establishments e
      join ingest.catalog_candidates c on c.id = e.catalog_candidate_id
      left join public.primary_types pt on pt.id = e.primary_type_id
      left join lateral (
        select
          array_agg(distinct link.provider order by link.provider) as providers,
          bool_or(cache.expires_at > now()) as has_warm_cache
        from runtime.runtime_provider_links link
        left join runtime.provider_response_cache cache
          on cache.provider_link_id = link.id
         and cache.provider = link.provider
        where link.establishment_id = e.id
          and link.retired_at is null
      ) provider_links on true
      where e.publication_state = 'published'
        and e.status = 'open'
        and e.access_mode = 'walk_in'
        and e.verification_tier in ('open_evidence', 'provider', 'manual')
        and e.verification_expires_at > now()
        and c.candidate_state = 'published'
        and c.identity_confidence >= 0.96
        and c.decision_version = e.verification_version
        and (
          ${args.q === ""} or e.name ilike ${args.pattern}
          or e.address ilike ${args.pattern}
          or e.city ilike ${args.pattern}
          or coalesce(e.neighborhood, '') ilike ${args.pattern}
        )
      order by e.name, e.id
      limit ${args.limit} offset ${args.offset}
    `,
  ]);
}

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
      next_action:
        "Apply the additive catalog v2 migration; do not trust or rebuild the legacy catalog.",
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

async function v2Payload(sql: Sql, runtimeApplied: boolean) {
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
    expansionRows,
  ] = await Promise.all([
    sql`
      with classified as (
        select
          e.*,
          c.candidate_state,
          c.identity_confidence as candidate_identity_confidence,
          c.decision_version as candidate_decision_version,
          (
            e.publication_state = 'published'
            and e.status = 'open'
          ) as is_app_visible,
          (
            e.publication_state = 'published'
            and e.status = 'open'
            and e.catalog_candidate_id is not null
            and e.access_mode = 'walk_in'
            and e.verification_tier in ('open_evidence', 'provider', 'manual')
            and e.verification_expires_at > now()
            and c.candidate_state = 'published'
            and c.identity_confidence >= 0.96
            and c.decision_version = e.verification_version
          ) as is_safe
        from public.establishments e
        left join ingest.catalog_candidates c on c.id = e.catalog_candidate_id
      )
      select
        count(*)::bigint as rows_total,
        count(*) filter (where is_app_visible)::bigint as app_visible,
        count(*) filter (where is_safe)::bigint as safe_live,
        count(*) filter (
          where is_app_visible and catalog_candidate_id is null
        )::bigint as unsafe_legacy_live,
        count(*) filter (
          where is_app_visible
            and catalog_candidate_id is not null
            and (verification_expires_at is null or verification_expires_at <= now())
        )::bigint as unsafe_expired_live,
        count(*) filter (
          where is_app_visible and not is_safe
        )::bigint as unsafe_app_visible,
        count(*) filter (where is_safe and phone_e164 is not null)::bigint as phone,
        count(*) filter (where is_safe and website_url is not null)::bigint as website,
        count(*) filter (where is_safe and neighborhood is not null)::bigint as neighborhood,
        count(*) filter (where is_safe and hours is not null)::bigint as hours,
        count(*) filter (where is_safe and price_level is not null)::bigint as price,
        count(*) filter (
          where is_safe and exists (
            select 1 from public.establishment_settings es
            where es.establishment_id = classified.id
          )
        )::bigint as settings,
        min(verification_expires_at) filter (where is_safe) as next_expiry,
        max(last_verified_at) filter (where is_safe) as last_verified_at,
        array_agg(distinct verification_version order by verification_version)
          filter (where is_safe and verification_version is not null) as decision_versions
      from classified
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
        count(*) filter (where candidate_state = 'needs_review')::bigint
          as candidates_needing_review,
        count(*) filter (where candidate_state = 'needs_verification')::bigint
          as candidates_needing_verification,
        count(*) filter (where candidate_state = 'verified')::bigint as ready_to_publish,
        count(*) filter (
          where candidate_state in ('verified', 'published')
            and verification_expires_at > now()
            and verification_expires_at <= now() + interval '14 days'
        )::bigint as leases_due,
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
      select e.verification_tier as tier, count(*)::bigint as count
      from public.establishments e
      join ingest.catalog_candidates c on c.id = e.catalog_candidate_id
      where e.publication_state = 'published' and e.status = 'open'
        and e.access_mode = 'walk_in'
        and e.verification_tier in ('open_evidence', 'provider', 'manual')
        and e.verification_expires_at > now()
        and c.candidate_state = 'published'
        and c.identity_confidence >= 0.96
        and c.decision_version = e.verification_version
      group by e.verification_tier
      order by count(*) desc, e.verification_tier
    `,
    sql`
      select pt.slug, pt.name, count(*)::bigint as count
      from public.establishments e
      join ingest.catalog_candidates c on c.id = e.catalog_candidate_id
      join public.primary_types pt on pt.id = e.primary_type_id
      where e.publication_state = 'published' and e.status = 'open'
        and e.access_mode = 'walk_in'
        and e.verification_tier in ('open_evidence', 'provider', 'manual')
        and e.verification_expires_at > now()
        and c.candidate_state = 'published'
        and c.identity_confidence >= 0.96
        and c.decision_version = e.verification_version
      group by pt.slug, pt.name
      order by count(*) desc, pt.name
    `,
    sql`
      select e.city, count(*)::bigint as count
      from public.establishments e
      join ingest.catalog_candidates c on c.id = e.catalog_candidate_id
      where e.publication_state = 'published' and e.status = 'open'
        and e.access_mode = 'walk_in'
        and e.verification_tier in ('open_evidence', 'provider', 'manual')
        and e.verification_expires_at > now()
        and c.candidate_state = 'published'
        and c.identity_confidence >= 0.96
        and c.decision_version = e.verification_version
      group by e.city
      order by count(*) desc, e.city
      limit 16
    `,
    sql`
      select event.release_id,
             event.scope_cities,
             event.maximum_new_publications,
             event.baseline_publications,
             event.minimum_healthy_refresh_weeks,
             event.expires_at,
             event.terms_version,
             count(establishment.id) filter (
               where establishment.publication_state = 'published'
                 and establishment.status = 'open'
             )::bigint as published
      from governance.catalog_expansion_release_events event
      left join public.establishments establishment
        on establishment.expansion_release_id = event.release_id
      where event.event_type = 'approved'
      group by event.id
      order by event.id desc
      limit 1
    `,
  ]);

  const runtimeRows = runtimeApplied
    ? await sql`
      with safe as (
        select e.id
        from public.establishments e
        join ingest.catalog_candidates c on c.id = e.catalog_candidate_id
        where e.publication_state = 'published'
          and e.status = 'open'
          and e.access_mode = 'walk_in'
          and e.verification_tier in ('open_evidence', 'provider', 'manual')
          and e.verification_expires_at > now()
          and c.candidate_state = 'published'
          and c.identity_confidence >= 0.96
          and c.decision_version = e.verification_version
      ), provider_names as (
        select link.provider
        from runtime.runtime_provider_links link
        join safe on safe.id = link.establishment_id
        where link.retired_at is null
        union
        select match.provider
        from runtime.provider_match_state match
        join safe on safe.id = match.establishment_id
        union
        select cache.provider from runtime.provider_response_cache cache
      ), link_stats as (
        select link.provider,
               count(distinct link.establishment_id)::bigint as active_links,
               min(link.match_confidence) as min_confidence,
               max(link.last_validated_at) as last_validated_at
        from runtime.runtime_provider_links link
        join safe on safe.id = link.establishment_id
        where link.retired_at is null
        group by link.provider
      ), match_stats as (
        select match.provider,
               count(*)::bigint as tracked,
               count(*) filter (where match.outcome = 'matched')::bigint as matched,
               count(*) filter (where match.outcome = 'rejected')::bigint as rejected,
               count(*) filter (where match.outcome = 'not_found')::bigint as not_found,
               count(*) filter (where match.outcome = 'error')::bigint as errors,
               count(*) filter (where match.outcome = 'pending')::bigint as pending,
               max(match.attempted_at) as last_attempted_at,
               min(match.retry_after) filter (
                 where match.outcome <> 'matched'
               ) as next_retry_at
        from runtime.provider_match_state match
        join safe on safe.id = match.establishment_id
        group by match.provider
      ), cache_stats as (
        select cache.provider,
               count(*)::bigint as cache_rows,
               count(*) filter (
                 where cache.expires_at > now()
                   and link.retired_at is null
                   and safe.id is not null
               )::bigint as fresh_cache_rows,
               count(distinct link.establishment_id) filter (
                 where cache.expires_at > now()
                   and link.retired_at is null
                   and safe.id is not null
               )::bigint as warm_establishments,
               min(cache.fetched_at) filter (
                 where cache.expires_at > now()
                   and link.retired_at is null
                   and safe.id is not null
               ) as oldest_fresh_fetch,
               max(cache.expires_at) filter (
                 where cache.expires_at > now()
                   and link.retired_at is null
                   and safe.id is not null
               ) as latest_expiry
        from runtime.provider_response_cache cache
        join runtime.runtime_provider_links link
          on link.id = cache.provider_link_id
         and link.provider = cache.provider
        left join safe on safe.id = link.establishment_id
        group by cache.provider
      ), lease_stats as (
        select lease.provider,
               count(*) filter (
                 where lease.lease_expires_at > now()
               )::bigint as active_leases
        from runtime.provider_refresh_leases lease
        group by lease.provider
      )
      select names.provider,
             coalesce(links.active_links, 0)::bigint as active_links,
             links.min_confidence,
             links.last_validated_at,
             coalesce(matches.tracked, 0)::bigint as tracked_matches,
             coalesce(matches.matched, 0)::bigint as matched,
             coalesce(matches.rejected, 0)::bigint as rejected,
             coalesce(matches.not_found, 0)::bigint as not_found,
             coalesce(matches.errors, 0)::bigint as errors,
             coalesce(matches.pending, 0)::bigint as pending,
             case when coalesce(matches.tracked, 0) > 0
               then (select count(*) from safe) - matches.tracked
               else null
             end::bigint as never_attempted,
             matches.last_attempted_at,
             matches.next_retry_at,
             coalesce(cache.cache_rows, 0)::bigint as cache_rows,
             coalesce(cache.fresh_cache_rows, 0)::bigint as fresh_cache_rows,
             coalesce(cache.warm_establishments, 0)::bigint as warm_establishments,
             cache.oldest_fresh_fetch,
             cache.latest_expiry,
             coalesce(leases.active_leases, 0)::bigint as active_leases,
             (
               select count(distinct link.establishment_id)
               from runtime.runtime_provider_links link
               join safe on safe.id = link.establishment_id
               where link.retired_at is null
             )::bigint as detail_ready,
             (
               select count(distinct warm_link.establishment_id)
               from runtime.provider_response_cache warm_cache
               join runtime.runtime_provider_links warm_link
                 on warm_link.id = warm_cache.provider_link_id
                and warm_link.provider = warm_cache.provider
               join safe warm_safe on warm_safe.id = warm_link.establishment_id
               where warm_cache.expires_at > now()
                 and warm_link.retired_at is null
             )::bigint as warm_detail_ready,
             (
               select count(*)
               from runtime.provider_response_cache all_cache
               join runtime.runtime_provider_links all_link
                 on all_link.id = all_cache.provider_link_id
                and all_link.provider = all_cache.provider
               left join safe all_safe on all_safe.id = all_link.establishment_id
               where all_cache.expires_at <= now()
                  or all_link.retired_at is not null
                  or all_safe.id is null
             )::bigint as stale_or_ineligible_cache_rows
      from provider_names names
      left join link_stats links on links.provider = names.provider
      left join match_stats matches on matches.provider = names.provider
      left join cache_stats cache on cache.provider = names.provider
      left join lease_stats leases on leases.provider = names.provider
      order by names.provider
    `
    : [];

  const overall = overallRows[0] ?? {};
  const safeLive = number(overall.safe_live);
  const unsafeLegacy = number(overall.unsafe_legacy_live);
  const unsafeExpired = number(overall.unsafe_expired_live);
  const unsafeAppVisible = number(overall.unsafe_app_visible);
  const decisionVersions = Array.isArray(overall.decision_versions)
    ? overall.decision_versions.filter(Boolean).map(String)
    : [];
  const decisionVersion = decisionVersions.length === 1
    ? decisionVersions[0]
    : decisionVersions.length > 1
    ? "mixed"
    : null;
  const providers = runtimeRows.map((row) => {
    const provider = String(row.provider);
    const meta = PROVIDER_META[provider] ?? {
      label: provider,
      detail_policy: "Reviewed provider policy",
    };
    return {
      provider,
      label: meta.label,
      detail_policy: meta.detail_policy,
      active_links: number(row.active_links),
      min_confidence: row.min_confidence === null
        ? null
        : number(row.min_confidence),
      last_validated_at: row.last_validated_at,
      match_tracking: number(row.tracked_matches) > 0,
      matched: number(row.matched),
      rejected: number(row.rejected),
      not_found: number(row.not_found),
      errors: number(row.errors),
      pending: number(row.pending),
      never_attempted: row.never_attempted === null
        ? null
        : number(row.never_attempted),
      last_attempted_at: row.last_attempted_at,
      next_retry_at: row.next_retry_at,
      cache_rows: number(row.cache_rows),
      fresh_cache_rows: number(row.fresh_cache_rows),
      warm_establishments: number(row.warm_establishments),
      oldest_fresh_fetch: row.oldest_fresh_fetch,
      latest_expiry: row.latest_expiry,
      active_leases: number(row.active_leases),
    };
  });
  const runtimeSummary = runtimeRows[0] ?? {};
  const detailReady = number(runtimeSummary.detail_ready);
  const providerErrors = providers.reduce((sum, row) => sum + row.errors, 0);
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
          number(row.freshness_days) * 86_400_000,
    ),
  }));
  const requiredReady = sources
    .filter((source) => source.required)
    .every((source) => source.fresh && source.record_count > 0);
  const cutoverComplete = unsafeLegacy === 0;
  const safeForUsers = cutoverComplete && unsafeAppVisible === 0 &&
    requiredReady;
  const expansion = expansionRows[0]
    ? {
        release_id: expansionRows[0].release_id,
        scope_cities: expansionRows[0].scope_cities ?? [],
        maximum_new_publications: number(expansionRows[0].maximum_new_publications),
        baseline_publications: number(expansionRows[0].baseline_publications),
        published: number(expansionRows[0].published),
        available_slots: Math.max(
          0,
          number(expansionRows[0].maximum_new_publications) -
            number(expansionRows[0].published),
        ),
        minimum_healthy_refresh_weeks: number(
          expansionRows[0].minimum_healthy_refresh_weeks,
        ),
        expires_at: expansionRows[0].expires_at,
        terms_version: expansionRows[0].terms_version,
      }
    : null;

  return {
    dashboard_version: "v3",
    schema_version: "catalog_v2",
    decision_version: decisionVersion,
    generated_at: new Date().toISOString(),
    readiness: {
      migration_applied: true,
      cutover_complete: cutoverComplete,
      required_sources_ready: requiredReady,
      safe_for_users: safeForUsers,
      next_action: nextAction({
        unsafeLegacy,
        unsafeExpired,
        unsafeAppVisible,
        requiredReady,
        safeLive,
        providerErrors,
        detailReady,
        work: workRows[0],
      }),
    },
    overall: {
      rows_total: number(overall.rows_total),
      app_visible: number(overall.app_visible),
      safe_live: safeLive,
      unsafe_legacy_live: unsafeLegacy,
      unsafe_expired_live: unsafeExpired,
      unsafe_app_visible: unsafeAppVisible,
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
    candidates: candidateRows.map((row) => ({
      state: row.state,
      count: number(row.count),
    })),
    blockers: blockerRows.map((row) => ({
      reason: row.reason,
      count: number(row.count),
    })),
    work_queue: {
      pending_match_reviews: number(workRows[0]?.pending_match_reviews),
      candidates_needing_review: number(workRows[0]?.candidates_needing_review),
      candidates_needing_verification: number(
        workRows[0]?.candidates_needing_verification,
      ),
      ready_to_publish: number(workRows[0]?.ready_to_publish),
      leases_due: number(workRows[0]?.leases_due),
      unexpired_verified: number(workRows[0]?.unexpired_verified),
    },
    runtime: {
      applied: runtimeApplied,
      eligible_establishments: safeLive,
      detail_ready: detailReady,
      warm_establishments: number(runtimeSummary.warm_detail_ready),
      fresh_cache_rows: providers.reduce(
        (sum, row) => sum + row.fresh_cache_rows,
        0,
      ),
      stale_or_ineligible_cache_rows: number(
        runtimeSummary.stale_or_ineligible_cache_rows,
      ),
      active_refresh_leases: providers.reduce(
        (sum, row) => sum + row.active_leases,
        0,
      ),
      providers,
    },
    expansion,
    sources,
    verification_tiers: tierRows.map((row) => ({
      tier: row.tier,
      count: number(row.count),
    })),
    published_types: typeRows.map((row) => ({
      slug: row.slug,
      name: row.name,
      count: number(row.count),
    })),
    published_cities: cityRows.map((row) => ({
      city: row.city,
      count: number(row.count),
    })),
    recent_runs: runRows.map((row) => run(row)),
  };
}

function nextAction(args: {
  unsafeLegacy: number;
  unsafeExpired: number;
  unsafeAppVisible: number;
  requiredReady: boolean;
  safeLive: number;
  providerErrors: number;
  detailReady: number;
  work: Record<string, unknown> | undefined;
}): string {
  if (!args.requiredReady) {
    return "Complete successful California ABC and Foursquare OS snapshots.";
  }
  if (args.unsafeExpired > 0) {
    return "Run catalog-sweep immediately; expired rows are still app-visible.";
  }
  if (args.unsafeAppVisible > 0) {
    return "Remove app-visible rows that no longer satisfy every publication invariant.";
  }
  if (args.unsafeLegacy > 0) {
    return "Quarantine legacy rows; destructive catalog cutover is no longer an operator path.";
  }
  if (args.detailReady < args.safeLive) {
    return "Restore live-detail routing for every existing published establishment.";
  }
  if (number(args.work?.ready_to_publish) > 0) {
    return "Keep verified candidates private until a bounded expansion release is fully authorized.";
  }
  if (number(args.work?.candidates_needing_review) > 0) {
    return "Review held identity conflicts when expanding coverage; the live catalog remains isolated.";
  }
  if (args.providerErrors > 0) {
    return "Retry provider identity errors after their cooldown; durable catalog truth is unaffected.";
  }
  if (args.safeLive === 0) {
    return "No venue currently passes every hard gate; leave the app catalog empty.";
  }
  return "No urgent action. Keep source snapshots and verification leases current.";
}
