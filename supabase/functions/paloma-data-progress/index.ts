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

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }
  if (req.method !== "GET") {
    return Response.json({ error: "method_not_allowed" }, { status: 405, headers: corsHeaders });
  }

  const databaseUrl = Deno.env.get("SUPABASE_DB_URL");
  if (!databaseUrl) {
    return Response.json({ error: "database_unavailable" }, { status: 500, headers: corsHeaders });
  }

  const sql = postgres(databaseUrl, {
    max: 1,
    idle_timeout: 5,
    connect_timeout: 5,
    prepare: false,
  });

  try {
    const [latestRuns, stagedRows, linkedRows, reviewRows, overallRows, recentRuns] = await Promise.all([
      sql`
        select distinct on (source)
          source, mode, status, fetched_count, created_count, updated_count,
          unchanged_count, review_count, closed_count, started_at, finished_at,
          left(coalesce(error_summary, ''), 280) as error_summary
        from ingest.ingestion_runs
        where source in ('ca_abc', 'datasf', 'overture')
        order by source, started_at desc
      `,
      sql`
        select source, count(*)::bigint as staged_count
        from ingest.source_records
        where source in ('ca_abc', 'datasf', 'overture')
        group by source
      `,
      sql`
        select source, count(distinct establishment_id)::bigint as linked_count
        from ingest.establishment_sources
        where source in ('ca_abc', 'datasf', 'overture')
        group by source
      `,
      sql`
        select source,
          count(*) filter (where state = 'pending')::bigint as pending_review_count,
          count(*)::bigint as total_review_count
        from ingest.establishment_review_queue
        where source in ('ca_abc', 'datasf', 'overture')
        group by source
      `,
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
          (select count(*) from ingest.source_records)::bigint as staged_records,
          (select count(*) from ingest.establishment_review_queue where state = 'pending')::bigint as pending_reviews
        from public.establishments e
        left join provenance p on p.establishment_id = e.id
      `,
      sql`
        select source, mode, status, fetched_count, created_count, updated_count,
          unchanged_count, review_count, closed_count, started_at, finished_at,
          left(coalesce(error_summary, ''), 180) as error_summary
        from ingest.ingestion_runs
        where source in ('ca_abc', 'datasf', 'overture')
        order by started_at desc
        limit 12
      `,
    ]);

    const latestBySource = new Map(latestRuns.map((row) => [String(row.source), row]));
    const stagedBySource = new Map(stagedRows.map((row) => [String(row.source), number(row.staged_count)]));
    const linkedBySource = new Map(linkedRows.map((row) => [String(row.source), number(row.linked_count)]));
    const reviewBySource = new Map(reviewRows.map((row) => [String(row.source), row]));

    const sources = SOURCES.map((source) => {
      const run = latestBySource.get(source);
      const review = reviewBySource.get(source);
      return {
        source,
        label: LABELS[source],
        staged_count: stagedBySource.get(source) ?? 0,
        linked_count: linkedBySource.get(source) ?? 0,
        pending_review_count: number(review?.pending_review_count),
        total_review_count: number(review?.total_review_count),
        latest_run: run ? {
          mode: run.mode,
          status: run.status,
          fetched: number(run.fetched_count),
          created: number(run.created_count),
          updated: number(run.updated_count),
          unchanged: number(run.unchanged_count),
          review: number(run.review_count),
          closed: number(run.closed_count),
          started_at: run.started_at,
          finished_at: run.finished_at,
          error: run.error_summary || null,
        } : null,
      };
    });

    const overall = overallRows[0] ?? {};
    return Response.json({
      generated_at: new Date().toISOString(),
      overall: {
        total_establishments: number(overall.total_establishments),
        ingestion_backed: number(overall.ingestion_backed),
        one_source: number(overall.one_source),
        multi_source: number(overall.multi_source),
        staged_records: number(overall.staged_records),
        pending_reviews: number(overall.pending_reviews),
      },
      sources,
      recent_runs: recentRuns.map((run) => ({
        source: run.source,
        label: LABELS[String(run.source)] ?? run.source,
        mode: run.mode,
        status: run.status,
        fetched: number(run.fetched_count),
        created: number(run.created_count),
        updated: number(run.updated_count),
        unchanged: number(run.unchanged_count),
        review: number(run.review_count),
        closed: number(run.closed_count),
        started_at: run.started_at,
        finished_at: run.finished_at,
        error: run.error_summary || null,
      })),
    }, { headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8" } });
  } catch (error) {
    console.error("paloma-data-progress", error);
    return Response.json({ error: "progress_query_failed" }, { status: 500, headers: corsHeaders });
  } finally {
    await sql.end({ timeout: 1 });
  }
});
