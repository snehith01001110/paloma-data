import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.7";

const SOURCES = ["ca_abc", "datasf", "overture"] as const;
const SOURCE_LABELS: Record<string, string> = {
  ca_abc: "California ABC",
  datasf: "DataSF",
  overture: "Overture Maps",
  fsq: "Foursquare OS",
};
const PUBLICATION_STATES = ["published", "candidate", "review", "suppressed"] as const;

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
  const requestedState = url.searchParams.get("state");
  const publicationState = PUBLICATION_STATES.includes(
    requestedState as (typeof PUBLICATION_STATES)[number],
  ) ? requestedState : null;
  const requestedSource = url.searchParams.get("source");
  const source = SOURCES.includes(requestedSource as (typeof SOURCES)[number])
    ? requestedSource
    : null;
  const reason = (url.searchParams.get("reason") ?? "").trim().slice(0, 100) || null;
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
    const [
      latestRunRows,
      recentRunRows,
      sourceMetricRows,
      overallRows,
      publicationReasonRows,
      reviewReasonRows,
      typeRows,
      cityRows,
    ] = await Promise.all([
      sql`
        select distinct on (source)
          id::text, source, mode, status, fetched_count, created_count, updated_count,
          unchanged_count, review_count, closed_count, started_at, finished_at,
          left(coalesce(error_summary, ''), 280) as error_summary
        from ingest.ingestion_runs
        where source in ('ca_abc', 'datasf', 'overture')
        order by source, started_at desc
      `,
      sql`
        select id::text, source, mode, status, fetched_count, created_count, updated_count,
               unchanged_count, review_count, closed_count, started_at, finished_at,
               left(coalesce(error_summary, ''), 280) as error_summary
        from ingest.ingestion_runs
        where source in ('ca_abc', 'datasf', 'overture')
        order by started_at desc
        limit 18
      `,
      sql`
        select
          source,
          count(*)::bigint as record_count,
          count(*) filter (where latitude is not null and longitude is not null)::bigint as placed_count,
          count(*) filter (where consumer_facing and public_access = 'walk_in')::bigint
            as consumer_place_count,
          count(*) filter (where source_status = 'open')::bigint as open_record_count
        from ingest.source_records
        where source in ('ca_abc', 'datasf', 'overture')
        group by source
      `,
      sql`
        with provenance as (
          select establishment_id, count(distinct source)::int as source_count
          from ingest.establishment_sources
          group by establishment_id
        ), queue as (
          select
            count(distinct (source, source_record_id)) filter (
              where state = 'pending' and reason = 'needs_type_or_location_corroboration'
            )::bigint as evidence_waiting,
            count(distinct (source, source_record_id)) filter (
              where state = 'pending' and reason <> 'needs_type_or_location_corroboration'
            )::bigint as actionable_match_reviews
          from ingest.establishment_review_queue
        )
        select
          count(*)::bigint as total_establishments,
          count(*) filter (where p.establishment_id is not null)::bigint as ingestion_backed,
          count(*) filter (
            where e.publication_state = 'published' and e.status = 'open'
          )::bigint as published,
          count(*) filter (
            where e.publication_state = 'published' and e.status = 'open'
              and p.establishment_id is null
          )::bigint as curated_published,
          count(*) filter (
            where p.establishment_id is not null and e.publication_state = 'candidate'
          )::bigint as candidates,
          count(*) filter (
            where p.establishment_id is not null and e.publication_state = 'review'
          )::bigint as publication_reviews,
          count(*) filter (
            where p.establishment_id is not null and e.publication_state = 'suppressed'
          )::bigint as suppressed,
          count(*) filter (
            where p.establishment_id is not null and e.field_resolution_version = 'v4'
          )::bigint as resolver_current,
          count(*) filter (
            where p.establishment_id is not null and p.source_count >= 2
          )::bigint as multi_source,
          count(*) filter (
            where e.publication_state = 'published' and e.status = 'open'
              and e.public_access_verified_at >= now() - interval '550 days'
          )::bigint as access_evidence_current,
          max(e.publication_evaluated_at) as publication_evaluated_at,
          q.evidence_waiting,
          q.actionable_match_reviews
        from public.establishments e
        left join provenance p on p.establishment_id = e.id
        cross join queue q
        group by q.evidence_waiting, q.actionable_match_reviews
      `,
      sql`
        select e.publication_state as state,
               coalesce(e.publication_reason, 'not_evaluated') as reason,
               count(*)::bigint as count
        from public.establishments e
        where exists (
          select 1 from ingest.establishment_sources es where es.establishment_id = e.id
        )
        group by e.publication_state, coalesce(e.publication_reason, 'not_evaluated')
        order by count desc
      `,
      sql`
        select reason, source,
               count(distinct (source, source_record_id))::bigint as count
        from ingest.establishment_review_queue
        where state = 'pending'
          and source in ('ca_abc', 'datasf', 'overture')
        group by reason, source
        order by count desc
      `,
      sql`
        select pt.slug, pt.name, count(*)::bigint as count
        from public.establishments e
        join public.primary_types pt on pt.id = e.primary_type_id
        where e.publication_state = 'published' and e.status = 'open'
        group by pt.slug, pt.name
        order by count desc, pt.name
      `,
      sql`
        select city, count(*)::bigint as count
        from public.establishments
        where publication_state = 'published' and status = 'open'
        group by city
        order by count desc, city
        limit 12
      `,
    ]);

    const latestBySource = new Map(latestRunRows.map((row) => [String(row.source), row]));
    const metricsBySource = new Map(sourceMetricRows.map((row) => [String(row.source), row]));
    const sourceLinkRows = await sql`
      select es.source,
             count(*)::bigint as linked_record_count,
             count(distinct es.establishment_id)::bigint as linked_establishment_count,
             count(distinct es.establishment_id) filter (
               where e.publication_state = 'published' and e.status = 'open'
             )::bigint as published_establishment_count
      from ingest.establishment_sources es
      join public.establishments e on e.id = es.establishment_id
      where es.source in ('ca_abc', 'datasf', 'overture')
      group by es.source
    `;
    const linksBySource = new Map(sourceLinkRows.map((row) => [String(row.source), row]));

    const reviewBreakdown = new Map<
      string,
      { reason: string; count: number; sources: Array<{ source: string; label: string; count: number }> }
    >();
    for (const row of reviewReasonRows) {
      const reasonName = String(row.reason);
      const sourceName = String(row.source);
      const count = number(row.count);
      const entry = reviewBreakdown.get(reasonName) ?? { reason: reasonName, count: 0, sources: [] };
      entry.count += count;
      entry.sources.push({
        source: sourceName,
        label: SOURCE_LABELS[sourceName] ?? sourceName,
        count,
      });
      reviewBreakdown.set(reasonName, entry);
    }

    const run = (row: Record<string, unknown> | undefined) => row ? ({
      id: row.id,
      source: row.source,
      label: SOURCE_LABELS[String(row.source)] ?? row.source,
      mode: row.mode,
      status: row.status,
      fetched: number(row.fetched_count),
      created: number(row.created_count),
      updated: number(row.updated_count),
      unchanged: number(row.unchanged_count),
      review: number(row.review_count),
      closed: number(row.closed_count),
      started_at: row.started_at,
      finished_at: row.finished_at,
      error: row.error_summary || null,
    }) : null;

    const sources = SOURCES.map((sourceName) => {
      const metric = metricsBySource.get(sourceName);
      const links = linksBySource.get(sourceName);
      return {
        source: sourceName,
        label: SOURCE_LABELS[sourceName],
        record_count: number(metric?.record_count),
        placed_count: number(metric?.placed_count),
        consumer_place_count: number(metric?.consumer_place_count),
        open_record_count: number(metric?.open_record_count),
        linked_record_count: number(links?.linked_record_count),
        linked_establishment_count: number(links?.linked_establishment_count),
        published_establishment_count: number(links?.published_establishment_count),
        latest_run: run(latestBySource.get(sourceName)),
      };
    });

    const overall = overallRows[0] ?? {};
    const payload: Record<string, unknown> = {
      generated_at: new Date().toISOString(),
      overall: {
        total_establishments: number(overall.total_establishments),
        ingestion_backed: number(overall.ingestion_backed),
        published: number(overall.published),
        curated_published: number(overall.curated_published),
        candidates: number(overall.candidates),
        publication_reviews: number(overall.publication_reviews),
        suppressed: number(overall.suppressed),
        resolver_current: number(overall.resolver_current),
        multi_source: number(overall.multi_source),
        access_evidence_current: number(overall.access_evidence_current),
        evidence_waiting: number(overall.evidence_waiting),
        actionable_match_reviews: number(overall.actionable_match_reviews),
        publication_evaluated_at: overall.publication_evaluated_at,
      },
      sources,
      recent_runs: recentRunRows.map((row) => run(row)),
      publication_breakdown: publicationReasonRows.map((row) => ({
        state: String(row.state),
        reason: String(row.reason),
        count: number(row.count),
      })),
      review_breakdown: Array.from(reviewBreakdown.values()).sort((a, b) => b.count - a.count),
      published_types: typeRows.map((row) => ({
        slug: String(row.slug),
        name: String(row.name),
        count: number(row.count),
      })),
      published_cities: cityRows.map((row) => ({
        city: String(row.city),
        count: number(row.count),
      })),
    };

    if (view === "venues") {
      const [countRows, items] = await Promise.all([
        sql`
          select count(*)::bigint as total
          from public.establishments e
          where (${publicationState === null} or e.publication_state = ${publicationState ?? ""})
            and (${reason === null} or e.publication_reason = ${reason ?? ""})
            and (
              ${q === ""} or e.name ilike ${pattern} or e.address ilike ${pattern}
              or e.city ilike ${pattern}
            )
            and (
              ${source === null} or exists (
                select 1 from ingest.establishment_sources f
                where f.establishment_id = e.id and f.source = ${source ?? ""}
              )
            )
        `,
        sql`
          with provenance as (
            select establishment_id,
                   array_agg(distinct source order by source) as sources,
                   count(distinct source)::int as source_count
            from ingest.establishment_sources
            group by establishment_id
          )
          select e.id::text, e.name, e.address, e.city, e.region, e.status,
                 e.publication_state, e.publication_reason, e.access_mode,
                 e.public_access_verified_at, e.publication_evaluated_at,
                 e.identity_confidence, e.display_name_confidence, e.display_name_source,
                 e.type_confidence, e.field_resolution_version, e.last_verified_at,
                 pt.slug as primary_type_slug, pt.name as primary_type_name,
                 coalesce(p.sources, '{}'::text[]) as sources,
                 coalesce(p.source_count, 0)::int as source_count
          from public.establishments e
          left join provenance p on p.establishment_id = e.id
          left join public.primary_types pt on pt.id = e.primary_type_id
          where (${publicationState === null} or e.publication_state = ${publicationState ?? ""})
            and (${reason === null} or e.publication_reason = ${reason ?? ""})
            and (
              ${q === ""} or e.name ilike ${pattern} or e.address ilike ${pattern}
              or e.city ilike ${pattern}
            )
            and (${source === null} or ${source ?? ""} = any(coalesce(p.sources, '{}'::text[])))
          order by
            case e.publication_state
              when 'review' then 0 when 'candidate' then 1
              when 'suppressed' then 2 else 3
            end,
            e.publication_evaluated_at desc nulls last,
            e.name
          limit ${limit} offset ${offset}
        `,
      ]);
      payload.list = {
        view,
        state: publicationState,
        reason,
        total: number(countRows[0]?.total),
        offset,
        limit,
        items: items.map((row) => ({
          ...row,
          identity_confidence: row.identity_confidence == null ? null : number(row.identity_confidence),
          display_name_confidence: row.display_name_confidence == null
            ? null
            : number(row.display_name_confidence),
          type_confidence: row.type_confidence == null ? null : number(row.type_confidence),
          source_count: number(row.source_count),
          sources: row.sources ?? [],
        })),
      };
    } else if (view === "review_queue") {
      const [countRows, items] = await Promise.all([
        sql`
          select count(*)::bigint as total
          from ingest.establishment_review_queue rq
          join ingest.source_records sr
            on sr.source = rq.source and sr.source_record_id = rq.source_record_id
          where rq.state = 'pending'
            and (${reason === null} or rq.reason = ${reason ?? ""})
            and (${source === null} or rq.source = ${source ?? ""})
            and (
              ${q === ""} or sr.name ilike ${pattern} or sr.address ilike ${pattern}
              or sr.city ilike ${pattern} or rq.reason ilike ${pattern}
            )
        `,
        sql`
          select rq.id::text, rq.source, rq.reason, rq.confidence, rq.created_at,
                 rq.candidate_establishment_id::text, sr.name, sr.address, sr.city, sr.region,
                 sr.primary_type_slug, candidate.name as candidate_name
          from ingest.establishment_review_queue rq
          join ingest.source_records sr
            on sr.source = rq.source and sr.source_record_id = rq.source_record_id
          left join public.establishments candidate on candidate.id = rq.candidate_establishment_id
          where rq.state = 'pending'
            and (${reason === null} or rq.reason = ${reason ?? ""})
            and (${source === null} or rq.source = ${source ?? ""})
            and (
              ${q === ""} or sr.name ilike ${pattern} or sr.address ilike ${pattern}
              or sr.city ilike ${pattern} or rq.reason ilike ${pattern}
            )
          order by
            case when rq.reason = 'needs_type_or_location_corroboration' then 1 else 0 end,
            rq.created_at desc,
            sr.name
          limit ${limit} offset ${offset}
        `,
      ]);
      payload.list = {
        view,
        reason,
        total: number(countRows[0]?.total),
        offset,
        limit,
        items: items.map((row) => ({
          ...row,
          label: SOURCE_LABELS[String(row.source)] ?? row.source,
          confidence: row.confidence == null ? null : number(row.confidence),
        })),
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
