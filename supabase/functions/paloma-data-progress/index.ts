import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.7";

const SOURCES = ["ca_abc", "datasf", "overture"] as const;
const LABELS: Record<string, string> = {
  ca_abc: "California ABC",
  datasf: "DataSF",
  overture: "Overture Maps",
  official_web: "Official web",
  canonical_seed: "Previous canonical",
};
const QUALITY_FILTERS = ["needs_verification", "identity", "name", "type", "unresolved"] as const;

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

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders });
  if (req.method !== "GET") return Response.json({ error: "method_not_allowed" }, { status: 405, headers: corsHeaders });

  const databaseUrl = Deno.env.get("SUPABASE_DB_URL");
  if (!databaseUrl) return Response.json({ error: "database_unavailable" }, { status: 500, headers: corsHeaders });

  const url = new URL(req.url);
  const view = url.searchParams.get("view");
  const q = (url.searchParams.get("q") ?? "").trim().slice(0, 100);
  const requestedSource = url.searchParams.get("source");
  const source = SOURCES.includes(requestedSource as (typeof SOURCES)[number]) ? requestedSource : null;
  const requestedQuality = url.searchParams.get("quality");
  const quality = QUALITY_FILTERS.includes(requestedQuality as (typeof QUALITY_FILTERS)[number]) ? requestedQuality : null;
  const reason = (url.searchParams.get("reason") ?? "").trim().slice(0, 80) || null;
  const limit = boundedInt(url.searchParams.get("limit"), 24, 1, 50);
  const offset = boundedInt(url.searchParams.get("offset"), 0, 0, 10000);
  const pattern = `%${q}%`;

  const sql = postgres(databaseUrl, { max: 1, idle_timeout: 5, connect_timeout: 5, prepare: false });

  try {
    const [latestRuns, stagedRows, linkedRows, reviewRows, overallRows, reviewBreakdownRows, nameSourceRows, nameConflictRows] = await Promise.all([
      sql`select distinct on (source) source, mode, status, fetched_count, created_count, updated_count, unchanged_count, review_count, closed_count, started_at, finished_at, left(coalesce(error_summary, ''), 280) as error_summary from ingest.ingestion_runs where source in ('ca_abc', 'datasf', 'overture') order by source, started_at desc`,
      sql`select source, count(*)::bigint as staged_count from ingest.source_records where source in ('ca_abc', 'datasf', 'overture') group by source`,
      sql`select source, count(*)::bigint as linked_record_count, count(distinct establishment_id)::bigint as linked_establishment_count from ingest.establishment_sources where source in ('ca_abc', 'datasf', 'overture') group by source`,
      sql`select source, count(*) filter (where state = 'pending')::bigint as pending_review_count, count(distinct source_record_id) filter (where state = 'pending')::bigint as pending_review_record_count from ingest.establishment_review_queue where source in ('ca_abc', 'datasf', 'overture') group by source`,
      sql`
        with provenance as (
          select establishment_id, count(distinct source) as source_count
          from ingest.establishment_sources
          group by establishment_id
        )
        select
          count(*)::bigint as total_establishments,
          count(*) filter (where p.establishment_id is not null)::bigint as ingestion_backed,
          count(*) filter (where p.source_count = 1)::bigint as one_source,
          count(*) filter (where p.source_count >= 2)::bigint as multi_source,
          count(*) filter (where p.establishment_id is not null and e.field_resolution_version = 'v2')::bigint as field_resolved,
          count(*) filter (where p.establishment_id is not null and coalesce(e.identity_confidence, 0) >= 0.90)::bigint as strong_identity,
          count(*) filter (where p.establishment_id is not null and coalesce(e.display_name_confidence, 0) >= 0.85)::bigint as strong_display_name,
          count(*) filter (where p.establishment_id is not null and coalesce(e.type_confidence, 0) >= 0.85)::bigint as strong_type,
          count(*) filter (
            where p.establishment_id is not null
              and e.field_resolution_version = 'v2'
              and coalesce(e.identity_confidence, 0) >= 0.90
              and coalesce(e.display_name_confidence, 0) >= 0.85
              and coalesce(e.type_confidence, 0) >= 0.85
          )::bigint as field_healthy,
          count(*) filter (where p.establishment_id is not null and (e.field_resolution_version is distinct from 'v2'))::bigint as unresolved_fields,
          count(*) filter (where p.establishment_id is not null and coalesce(e.identity_confidence, 0) < 0.90)::bigint as weak_identity,
          count(*) filter (where p.establishment_id is not null and coalesce(e.display_name_confidence, 0) < 0.85)::bigint as weak_display_name,
          count(*) filter (where p.establishment_id is not null and coalesce(e.type_confidence, 0) < 0.85)::bigint as weak_type,
          count(*) filter (where p.establishment_id is not null and e.display_name_source = 'official_web')::bigint as official_web_names,
          (select count(*) from ingest.establishment_review_queue where state = 'pending')::bigint as pending_reviews
        from public.establishments e
        left join provenance p on p.establishment_id = e.id
      `,
      sql`select reason, source, count(*)::bigint as pending_count from ingest.establishment_review_queue where state = 'pending' and source in ('ca_abc', 'datasf', 'overture') group by reason, source order by pending_count desc`,
      sql`
        select coalesce(display_name_source, 'unresolved') as source, count(*)::bigint as count
        from public.establishments e
        where exists (select 1 from ingest.establishment_sources es where es.establishment_id = e.id)
        group by coalesce(display_name_source, 'unresolved')
        order by count desc
      `,
      sql`
        with variants as (
          select establishment_id,
                 count(distinct normalized_value) filter (
                   where source <> 'canonical_seed' and normalized_value is not null and normalized_value <> ''
                 ) as variant_count
          from ingest.establishment_field_evidence
          where field_name = 'display_name'
          group by establishment_id
        )
        select count(*) filter (where variant_count > 1)::bigint as establishments_with_name_conflicts
        from variants
      `,
    ]);

    const latestBySource = new Map(latestRuns.map((row) => [String(row.source), row]));
    const stagedBySource = new Map(stagedRows.map((row) => [String(row.source), number(row.staged_count)]));
    const linkedBySource = new Map(linkedRows.map((row) => [String(row.source), row]));
    const reviewBySource = new Map(reviewRows.map((row) => [String(row.source), row]));

    const sources = SOURCES.map((sourceName) => {
      const run = latestBySource.get(sourceName);
      const linked = linkedBySource.get(sourceName);
      const review = reviewBySource.get(sourceName);
      const staged = stagedBySource.get(sourceName) ?? 0;
      const linkedRecords = number(linked?.linked_record_count);
      const reviewRecords = number(review?.pending_review_record_count);
      const residualRecords = Math.max(0, staged - linkedRecords - reviewRecords);
      const isRunning = run?.status === "running";
      return {
        source: sourceName,
        label: LABELS[sourceName],
        staged_count: staged,
        linked_record_count: linkedRecords,
        linked_establishment_count: number(linked?.linked_establishment_count),
        pending_review_count: number(review?.pending_review_count),
        pending_review_record_count: reviewRecords,
        waiting_count: isRunning ? residualRecords : 0,
        ignored_count: isRunning ? 0 : residualRecords,
        latest_run: run ? {
          mode: run.mode,
          status: run.status,
          fetched: number(run.fetched_count),
          started_at: run.started_at,
          finished_at: run.finished_at,
          error: run.error_summary || null,
        } : null,
      };
    });

    const breakdown = new Map<string, { reason: string; count: number; sources: Array<{ source: string; label: string; count: number }> }>();
    for (const row of reviewBreakdownRows) {
      const reasonName = String(row.reason);
      const sourceName = String(row.source);
      const count = number(row.pending_count);
      const existing = breakdown.get(reasonName) ?? { reason: reasonName, count: 0, sources: [] };
      existing.count += count;
      existing.sources.push({ source: sourceName, label: LABELS[sourceName] ?? sourceName, count });
      breakdown.set(reasonName, existing);
    }

    const overall = overallRows[0] ?? {};
    const payload: Record<string, unknown> = {
      generated_at: new Date().toISOString(),
      overall: {
        total_establishments: number(overall.total_establishments),
        ingestion_backed: number(overall.ingestion_backed),
        one_source: number(overall.one_source),
        multi_source: number(overall.multi_source),
        pending_reviews: number(overall.pending_reviews),
        field_resolved: number(overall.field_resolved),
        field_healthy: number(overall.field_healthy),
        strong_identity: number(overall.strong_identity),
        strong_display_name: number(overall.strong_display_name),
        strong_type: number(overall.strong_type),
        unresolved_fields: number(overall.unresolved_fields),
        weak_identity: number(overall.weak_identity),
        weak_display_name: number(overall.weak_display_name),
        weak_type: number(overall.weak_type),
        official_web_names: number(overall.official_web_names),
        display_name_conflicts: number(nameConflictRows[0]?.establishments_with_name_conflicts),
      },
      display_name_sources: nameSourceRows.map((row) => ({
        source: String(row.source),
        label: LABELS[String(row.source)] ?? String(row.source),
        count: number(row.count),
      })),
      sources,
      review_breakdown: Array.from(breakdown.values()).sort((a, b) => b.count - a.count),
    };

    if (view === "accepted") {
      const [countRows, items] = await Promise.all([
        sql`
          with provenance as (select establishment_id from ingest.establishment_sources group by establishment_id)
          select count(*)::bigint as total
          from public.establishments e
          join provenance p on p.establishment_id = e.id
          where (${q === ""} or e.name ilike ${pattern} or e.address ilike ${pattern} or e.city ilike ${pattern})
            and (${source === null} or exists (select 1 from ingest.establishment_sources f where f.establishment_id = e.id and f.source = ${source ?? ""}))
            and (
              ${quality === null}
              or (${quality === "needs_verification"} and (
                e.field_resolution_version is distinct from 'v2'
                or coalesce(e.identity_confidence, 0) < 0.90
                or coalesce(e.display_name_confidence, 0) < 0.85
                or coalesce(e.type_confidence, 0) < 0.85
              ))
              or (${quality === "identity"} and coalesce(e.identity_confidence, 0) < 0.90)
              or (${quality === "name"} and coalesce(e.display_name_confidence, 0) < 0.85)
              or (${quality === "type"} and coalesce(e.type_confidence, 0) < 0.85)
              or (${quality === "unresolved"} and e.field_resolution_version is distinct from 'v2')
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
          select
            e.id::text, e.name, e.address, e.city, e.region, e.status,
            e.data_quality_score, e.identity_confidence, e.display_name_confidence,
            e.display_name_source, e.type_confidence, e.field_resolution_version,
            e.last_verified_at, pt.slug as primary_type_slug, p.sources, p.source_count,
            coalesce((
              select jsonb_agg(to_jsonb(ne) order by ne.selected desc, ne.resolution_score desc nulls last, ne.evidence_confidence desc)
              from (
                select fe.source, fe.value_text, fe.claim_kind, fe.evidence_confidence,
                       fe.identity_confidence, fe.selected, fe.resolution_score
                from ingest.establishment_field_evidence fe
                where fe.establishment_id = e.id and fe.field_name in ('display_name', 'legal_name')
                order by fe.selected desc, fe.resolution_score desc nulls last, fe.authority desc
                limit 10
              ) ne
            ), '[]'::jsonb) as name_evidence,
            coalesce((
              select count(distinct fe.normalized_value)
              from ingest.establishment_field_evidence fe
              where fe.establishment_id = e.id
                and fe.field_name = 'display_name'
                and fe.source <> 'canonical_seed'
                and fe.normalized_value is not null and fe.normalized_value <> ''
            ), 0)::int as display_name_variant_count
          from public.establishments e
          join provenance p on p.establishment_id = e.id
          left join public.primary_types pt on pt.id = e.primary_type_id
          where (${q === ""} or e.name ilike ${pattern} or e.address ilike ${pattern} or e.city ilike ${pattern})
            and (${source === null} or exists (select 1 from ingest.establishment_sources f where f.establishment_id = e.id and f.source = ${source ?? ""}))
            and (
              ${quality === null}
              or (${quality === "needs_verification"} and (
                e.field_resolution_version is distinct from 'v2'
                or coalesce(e.identity_confidence, 0) < 0.90
                or coalesce(e.display_name_confidence, 0) < 0.85
                or coalesce(e.type_confidence, 0) < 0.85
              ))
              or (${quality === "identity"} and coalesce(e.identity_confidence, 0) < 0.90)
              or (${quality === "name"} and coalesce(e.display_name_confidence, 0) < 0.85)
              or (${quality === "type"} and coalesce(e.type_confidence, 0) < 0.85)
              or (${quality === "unresolved"} and e.field_resolution_version is distinct from 'v2')
            )
          order by
            case when e.field_resolution_version is distinct from 'v2' then 0 else 1 end,
            least(coalesce(e.identity_confidence, 0), coalesce(e.display_name_confidence, 0), coalesce(e.type_confidence, 0)) asc,
            e.last_verified_at desc nulls last,
            e.name
          limit ${limit} offset ${offset}
        `,
      ]);
      payload.list = {
        view,
        quality,
        total: number(countRows[0]?.total),
        offset,
        limit,
        items: items.map((row) => ({
          id: row.id,
          name: row.name,
          address: row.address,
          city: row.city,
          region: row.region,
          status: row.status,
          primary_type_slug: row.primary_type_slug,
          data_quality_score: row.data_quality_score == null ? null : number(row.data_quality_score),
          identity_confidence: row.identity_confidence == null ? null : number(row.identity_confidence),
          display_name_confidence: row.display_name_confidence == null ? null : number(row.display_name_confidence),
          display_name_source: row.display_name_source,
          type_confidence: row.type_confidence == null ? null : number(row.type_confidence),
          field_resolution_version: row.field_resolution_version,
          last_verified_at: row.last_verified_at,
          sources: row.sources ?? [],
          source_count: number(row.source_count),
          name_evidence: row.name_evidence ?? [],
          display_name_variant_count: number(row.display_name_variant_count),
        })),
      };
    } else if (view === "review") {
      const [countRows, items] = await Promise.all([
        sql`select count(*)::bigint as total from ingest.establishment_review_queue rq join ingest.source_records sr on sr.source = rq.source and sr.source_record_id = rq.source_record_id where rq.state = 'pending' and (${q === ""} or sr.name ilike ${pattern} or sr.address ilike ${pattern} or sr.city ilike ${pattern} or rq.reason ilike ${pattern}) and (${source === null} or rq.source = ${source ?? ""}) and (${reason === null} or rq.reason = ${reason ?? ""})`,
        sql`
          select rq.id::text, rq.source, rq.reason, rq.confidence, rq.created_at,
                 rq.candidate_establishment_id::text, sr.name, sr.address, sr.city, sr.region,
                 sr.primary_type_slug, candidate.name as candidate_name,
                 candidate.identity_confidence as candidate_identity_confidence,
                 candidate.display_name_confidence as candidate_display_name_confidence,
                 candidate.display_name_source as candidate_display_name_source,
                 candidate.type_confidence as candidate_type_confidence
          from ingest.establishment_review_queue rq
          join ingest.source_records sr on sr.source = rq.source and sr.source_record_id = rq.source_record_id
          left join public.establishments candidate on candidate.id = rq.candidate_establishment_id
          where rq.state = 'pending'
            and (${q === ""} or sr.name ilike ${pattern} or sr.address ilike ${pattern} or sr.city ilike ${pattern} or rq.reason ilike ${pattern})
            and (${source === null} or rq.source = ${source ?? ""})
            and (${reason === null} or rq.reason = ${reason ?? ""})
          order by rq.created_at desc, sr.name
          limit ${limit} offset ${offset}
        `,
      ]);
      payload.list = {
        view,
        total: number(countRows[0]?.total),
        offset,
        limit,
        reason,
        items: items.map((row) => ({
          id: row.id,
          source: row.source,
          label: LABELS[String(row.source)] ?? row.source,
          name: row.name,
          address: row.address,
          city: row.city,
          region: row.region,
          primary_type_slug: row.primary_type_slug,
          reason: row.reason,
          confidence: row.confidence == null ? null : number(row.confidence),
          created_at: row.created_at,
          candidate_establishment_id: row.candidate_establishment_id,
          candidate_name: row.candidate_name,
          candidate_identity_confidence: row.candidate_identity_confidence == null ? null : number(row.candidate_identity_confidence),
          candidate_display_name_confidence: row.candidate_display_name_confidence == null ? null : number(row.candidate_display_name_confidence),
          candidate_display_name_source: row.candidate_display_name_source,
          candidate_type_confidence: row.candidate_type_confidence == null ? null : number(row.candidate_type_confidence),
        })),
      };
    }

    return Response.json(payload, { headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8" } });
  } catch (error) {
    console.error("paloma-data-progress", error);
    return Response.json({ error: "progress_query_failed" }, { status: 500, headers: corsHeaders });
  } finally {
    await sql.end({ timeout: 1 });
  }
});
