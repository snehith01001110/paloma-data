import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.7";

const SOURCES = ["ca_abc", "datasf", "overture"] as const;
const LABELS: Record<string, string> = {
  ca_abc: "California ABC",
  datasf: "DataSF",
  overture: "Overture Maps",
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
  const reason = (url.searchParams.get("reason") ?? "").trim().slice(0, 80) || null;
  const limit = boundedInt(url.searchParams.get("limit"), 24, 1, 50);
  const offset = boundedInt(url.searchParams.get("offset"), 0, 0, 10000);
  const pattern = `%${q}%`;

  const sql = postgres(databaseUrl, { max: 1, idle_timeout: 5, connect_timeout: 5, prepare: false });

  try {
    const [latestRuns, stagedRows, linkedRows, reviewRows, overallRows, reviewBreakdownRows] = await Promise.all([
      sql`select distinct on (source) source, mode, status, fetched_count, created_count, updated_count, unchanged_count, review_count, closed_count, started_at, finished_at, left(coalesce(error_summary, ''), 280) as error_summary from ingest.ingestion_runs where source in ('ca_abc', 'datasf', 'overture') order by source, started_at desc`,
      sql`select source, count(*)::bigint as staged_count from ingest.source_records where source in ('ca_abc', 'datasf', 'overture') group by source`,
      sql`select source, count(*)::bigint as linked_record_count, count(distinct establishment_id)::bigint as linked_establishment_count from ingest.establishment_sources where source in ('ca_abc', 'datasf', 'overture') group by source`,
      sql`select source, count(*) filter (where state = 'pending')::bigint as pending_review_count, count(distinct source_record_id) filter (where state = 'pending')::bigint as pending_review_record_count from ingest.establishment_review_queue where source in ('ca_abc', 'datasf', 'overture') group by source`,
      sql`with provenance as (select establishment_id, count(distinct source) as source_count from ingest.establishment_sources group by establishment_id) select count(*)::bigint as total_establishments, count(*) filter (where p.establishment_id is not null)::bigint as ingestion_backed, count(*) filter (where p.source_count = 1)::bigint as one_source, count(*) filter (where p.source_count >= 2)::bigint as multi_source, (select count(*) from ingest.establishment_review_queue where state = 'pending')::bigint as pending_reviews from public.establishments e left join provenance p on p.establishment_id = e.id`,
      sql`select reason, source, count(*)::bigint as pending_count from ingest.establishment_review_queue where state = 'pending' and source in ('ca_abc', 'datasf', 'overture') group by reason, source order by pending_count desc`,
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
      return {
        source: sourceName,
        label: LABELS[sourceName],
        staged_count: staged,
        linked_record_count: linkedRecords,
        linked_establishment_count: number(linked?.linked_establishment_count),
        pending_review_count: number(review?.pending_review_count),
        pending_review_record_count: reviewRecords,
        waiting_count: Math.max(0, staged - linkedRecords - reviewRecords),
        latest_run: run ? { mode: run.mode, status: run.status, fetched: number(run.fetched_count), started_at: run.started_at, finished_at: run.finished_at, error: run.error_summary || null } : null,
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
      },
      sources,
      review_breakdown: Array.from(breakdown.values()).sort((a, b) => b.count - a.count),
    };

    if (view === "accepted") {
      const [countRows, items] = await Promise.all([
        sql`with provenance as (select establishment_id from ingest.establishment_sources group by establishment_id) select count(*)::bigint as total from public.establishments e join provenance p on p.establishment_id = e.id where (${q === ""} or e.name ilike ${pattern} or e.address ilike ${pattern} or e.city ilike ${pattern}) and (${source === null} or exists (select 1 from ingest.establishment_sources f where f.establishment_id = e.id and f.source = ${source ?? ""}))`,
        sql`with provenance as (select establishment_id, array_agg(distinct source order by source) as sources, count(distinct source)::int as source_count from ingest.establishment_sources group by establishment_id) select e.id::text, e.name, e.address, e.city, e.region, e.status, e.data_quality_score, e.last_verified_at, pt.slug as primary_type_slug, p.sources, p.source_count from public.establishments e join provenance p on p.establishment_id = e.id left join public.primary_types pt on pt.id = e.primary_type_id where (${q === ""} or e.name ilike ${pattern} or e.address ilike ${pattern} or e.city ilike ${pattern}) and (${source === null} or exists (select 1 from ingest.establishment_sources f where f.establishment_id = e.id and f.source = ${source ?? ""})) order by e.last_verified_at desc nulls last, e.name limit ${limit} offset ${offset}`,
      ]);
      payload.list = { view, total: number(countRows[0]?.total), offset, limit, items: items.map((row) => ({ id: row.id, name: row.name, address: row.address, city: row.city, region: row.region, status: row.status, primary_type_slug: row.primary_type_slug, data_quality_score: row.data_quality_score == null ? null : number(row.data_quality_score), last_verified_at: row.last_verified_at, sources: row.sources ?? [], source_count: number(row.source_count) })) };
    } else if (view === "review") {
      const [countRows, items] = await Promise.all([
        sql`select count(*)::bigint as total from ingest.establishment_review_queue rq join ingest.source_records sr on sr.source = rq.source and sr.source_record_id = rq.source_record_id where rq.state = 'pending' and (${q === ""} or sr.name ilike ${pattern} or sr.address ilike ${pattern} or sr.city ilike ${pattern} or rq.reason ilike ${pattern}) and (${source === null} or rq.source = ${source ?? ""}) and (${reason === null} or rq.reason = ${reason ?? ""})`,
        sql`select rq.id::text, rq.source, rq.reason, rq.confidence, rq.created_at, rq.candidate_establishment_id::text, sr.name, sr.address, sr.city, sr.region, sr.primary_type_slug, candidate.name as candidate_name from ingest.establishment_review_queue rq join ingest.source_records sr on sr.source = rq.source and sr.source_record_id = rq.source_record_id left join public.establishments candidate on candidate.id = rq.candidate_establishment_id where rq.state = 'pending' and (${q === ""} or sr.name ilike ${pattern} or sr.address ilike ${pattern} or sr.city ilike ${pattern} or rq.reason ilike ${pattern}) and (${source === null} or rq.source = ${source ?? ""}) and (${reason === null} or rq.reason = ${reason ?? ""}) order by rq.created_at desc, sr.name limit ${limit} offset ${offset}`,
      ]);
      payload.list = { view, total: number(countRows[0]?.total), offset, limit, reason, items: items.map((row) => ({ id: row.id, source: row.source, label: LABELS[String(row.source)] ?? row.source, name: row.name, address: row.address, city: row.city, region: row.region, primary_type_slug: row.primary_type_slug, reason: row.reason, confidence: row.confidence == null ? null : number(row.confidence), created_at: row.created_at, candidate_establishment_id: row.candidate_establishment_id, candidate_name: row.candidate_name })) };
    }

    return Response.json(payload, { headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8" } });
  } catch (error) {
    console.error("paloma-data-progress", error);
    return Response.json({ error: "progress_query_failed" }, { status: 500, headers: corsHeaders });
  } finally {
    await sql.end({ timeout: 1 });
  }
});
