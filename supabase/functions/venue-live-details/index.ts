import "jsr:@supabase/functions-js@2.5.0/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.7";
import {
  attributionUrl,
  hasLiveDetails,
  projectLiveDetails,
  providerFields,
  validateProviderPlace,
} from "./domain.ts";
import type { LiveFieldRequest } from "./domain.ts";
import { assertNoServerResponseCache } from "./provider_policy.ts";

const PLACES_API_VERSION = "2025-06-17";
const MAX_BODY_BYTES = 1_024;
const USER_REQUESTS_PER_MINUTE = 20;
const USER_REQUESTS_PER_DAY = 100;
const GLOBAL_REQUESTS_PER_SECOND = 15;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

// The self-service Foursquare policy currently prohibits response caching. Keep
// this endpoint fail-closed if the shared policy is ever edited unintentionally.
assertNoServerResponseCache("foursquare");

const responseHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, apikey, content-type, x-client-info",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Cache-Control": "private, no-store, no-cache, must-revalidate, max-age=0",
  "Pragma": "no-cache",
  "Expires": "0",
  "Vary": "Authorization",
  "Content-Type": "application/json; charset=utf-8",
};

type Sql = ReturnType<typeof postgres>;

type EligiblePlace = {
  id: string;
  name: string;
  latitude: number;
  longitude: number;
  fsq_place_id: string;
  needs_phone: boolean;
  needs_website: boolean;
  needs_hours: boolean;
  needs_price: boolean;
  needs_settings: boolean;
};

Deno.serve(async (request: Request) => {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: responseHeaders });
  }
  if (request.method !== "POST") {
    return json({ error: "method_not_allowed" }, 405);
  }

  const userId = await authenticatedUserId(request);
  if (!userId) return json({ error: "unauthorized" }, 401);

  const contentLength = Number(request.headers.get("content-length") ?? 0);
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES) {
    return json({ error: "invalid_request" }, 400);
  }
  const establishmentId = await requestEstablishmentId(request);
  if (!establishmentId) return json({ error: "invalid_request" }, 400);

  const databaseUrl = Deno.env.get("SUPABASE_DB_URL");
  const serviceKey = Deno.env.get("FSQ_PLACES_API_KEY");
  if (!databaseUrl || !serviceKey) {
    return json({ error: "temporarily_unavailable" }, 503);
  }

  const sql = postgres(databaseUrl, {
    max: 1,
    prepare: false,
    idle_timeout: 2,
    connect_timeout: 5,
  });

  let eligible: EligiblePlace | null = null;
  let requested: LiveFieldRequest | null = null;
  try {
    eligible = await eligiblePlace(sql, establishmentId);
    if (!eligible) {
      return json({ available: false, reason: "not_available" }, 404);
    }

    requested = requestedFields(eligible);
    if (!Object.values(requested).some(Boolean)) {
      return json({ available: false, reason: "durable_details_complete" });
    }
    if (!await consumeQuota(sql, userId)) {
      return json({ error: "rate_limited" }, 429, { "Retry-After": "60" });
    }
  } catch (error) {
    console.error("venue-live-details", safeErrorCode(error));
    return json({ error: "temporarily_unavailable" }, 503);
  } finally {
    await sql.end({ timeout: 1 });
  }

  return await fetchLiveDetails(eligible, requested, serviceKey);
});

async function authenticatedUserId(request: Request): Promise<string | null> {
  const authorization = request.headers.get("authorization") ?? "";
  if (!authorization.startsWith("Bearer ")) return null;
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY");
  if (!supabaseUrl || !anonKey) return null;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4_000);
  try {
    const response = await fetch(`${supabaseUrl}/auth/v1/user`, {
      method: "GET",
      headers: { authorization, apikey: anonKey },
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) return null;
    const payload = await response.json() as { id?: unknown };
    return typeof payload.id === "string" && UUID_PATTERN.test(payload.id)
      ? payload.id
      : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

async function requestEstablishmentId(
  request: Request,
): Promise<string | null> {
  try {
    const payload = await request.json() as { establishment_id?: unknown };
    const value = payload.establishment_id;
    return typeof value === "string" && UUID_PATTERN.test(value) ? value : null;
  } catch {
    return null;
  }
}

async function eligiblePlace(
  sql: Sql,
  establishmentId: string,
): Promise<EligiblePlace | null> {
  const rows = await sql`
    select
      e.id::text,
      e.name,
      extensions.st_y(e.location::geometry)::float8 as latitude,
      extensions.st_x(e.location::geometry)::float8 as longitude,
      fsq.source_record_id as fsq_place_id,
      e.phone_e164 is null as needs_phone,
      e.website_url is null as needs_website,
      e.hours is null as needs_hours,
      e.price_level is null as needs_price,
      not exists (
        select 1 from public.establishment_settings es
        where es.establishment_id = e.id
      ) as needs_settings
    from public.establishments e
    join ingest.catalog_candidates candidate on candidate.id = e.catalog_candidate_id
    join lateral (
      select link.source_record_id, link.identity_confidence
      from ingest.candidate_source_links link
      join ingest.source_records source_record
        on source_record.source = link.source
       and source_record.source_record_id = link.source_record_id
      where link.candidate_id = candidate.id
        and link.source = 'fsq'
        and link.identity_confidence >= 0.96
        and source_record.retired_at is null
        and source_record.source_status = 'open'
        and source_record.consumer_facing
        and source_record.public_access = 'walk_in'
        and not (source_record.quality_flags && array[
          'closed', 'delete', 'doesnt_exist', 'does_not_exist',
          'duplicate', 'inappropriate', 'privatevenue', 'private_venue'
        ]::text[])
      order by
        (candidate.anchor_source = 'fsq'
          and candidate.anchor_source_record_id = link.source_record_id) desc,
        link.identity_confidence desc,
        link.last_checked_at desc
      limit 1
    ) fsq on true
    where e.id = ${establishmentId}::uuid
      and e.publication_state = 'published'
      and e.status = 'open'
      and e.catalog_candidate_id is not null
      and e.verification_tier in ('open_evidence', 'provider', 'manual')
      and e.verification_expires_at > now()
      and candidate.candidate_state in ('verified', 'published')
      and candidate.identity_confidence >= 0.96
    limit 1
  `;
  const row = rows[0];
  if (!row) return null;
  return {
    id: String(row.id),
    name: String(row.name),
    latitude: Number(row.latitude),
    longitude: Number(row.longitude),
    fsq_place_id: String(row.fsq_place_id),
    needs_phone: Boolean(row.needs_phone),
    needs_website: Boolean(row.needs_website),
    needs_hours: Boolean(row.needs_hours),
    needs_price: Boolean(row.needs_price),
    needs_settings: Boolean(row.needs_settings),
  };
}

function requestedFields(place: EligiblePlace): LiveFieldRequest {
  return {
    phone: place.needs_phone,
    website: place.needs_website,
    hours: place.needs_hours,
    price: place.needs_price,
    settings: place.needs_settings,
  };
}

async function consumeQuota(sql: Sql, userId: string): Promise<boolean> {
  const userRows = await sql`
    insert into ingest.live_detail_user_limits as limits (
      user_id, minute_started_at, minute_count, day_started_at, day_count, updated_at
    ) values (
      ${userId}::uuid, date_trunc('minute', now()), 1,
      (now() at time zone 'utc')::date, 1, now()
    )
    on conflict (user_id) do update set
      minute_started_at = case
        when limits.minute_started_at <= now() - interval '1 minute'
          then date_trunc('minute', now())
        else limits.minute_started_at
      end,
      minute_count = case
        when limits.minute_started_at <= now() - interval '1 minute' then 1
        else limits.minute_count + 1
      end,
      day_started_at = case
        when limits.day_started_at < (now() at time zone 'utc')::date
          then (now() at time zone 'utc')::date
        else limits.day_started_at
      end,
      day_count = case
        when limits.day_started_at < (now() at time zone 'utc')::date then 1
        else limits.day_count + 1
      end,
      updated_at = now()
    where
      (case
        when limits.minute_started_at <= now() - interval '1 minute' then 1
        else limits.minute_count + 1
      end) <= ${USER_REQUESTS_PER_MINUTE}
      and (case
        when limits.day_started_at < (now() at time zone 'utc')::date then 1
        else limits.day_count + 1
      end) <= ${USER_REQUESTS_PER_DAY}
    returning 1
  `;
  if (userRows.length === 0) return false;

  const globalRows = await sql`
    insert into ingest.live_detail_global_limit as limits (
      singleton, second_started_at, request_count, updated_at
    ) values (true, date_trunc('second', now()), 1, now())
    on conflict (singleton) do update set
      second_started_at = case
        when limits.second_started_at < date_trunc('second', now())
          then date_trunc('second', now())
        else limits.second_started_at
      end,
      request_count = case
        when limits.second_started_at < date_trunc('second', now()) then 1
        else limits.request_count + 1
      end,
      updated_at = now()
    where (case
      when limits.second_started_at < date_trunc('second', now()) then 1
      else limits.request_count + 1
    end) <= ${GLOBAL_REQUESTS_PER_SECOND}
    returning 1
  `;
  return globalRows.length > 0;
}

async function fetchLiveDetails(
  place: EligiblePlace,
  requested: LiveFieldRequest,
  serviceKey: string,
): Promise<Response> {
  const url = new URL(
    `https://places-api.foursquare.com/places/${
      encodeURIComponent(place.fsq_place_id)
    }`,
  );
  url.searchParams.set("fields", providerFields(requested).join(","));
  if (requested.phone) url.searchParams.set("tel_format", "E164");

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 7_000);
  let response: Response;
  try {
    response = await fetch(url, {
      headers: {
        "Authorization": `Bearer ${serviceKey}`,
        "X-Places-Api-Version": PLACES_API_VERSION,
        "Accept": "application/json",
      },
      cache: "no-store",
      signal: controller.signal,
    });
  } catch {
    return json({ error: "provider_unavailable" }, 503);
  } finally {
    clearTimeout(timeout);
  }

  if (response.status === 404) {
    return json({ available: false, reason: "not_available" });
  }
  if (response.status === 429) {
    return json({ error: "provider_busy" }, 503, { "Retry-After": "60" });
  }
  if (!response.ok) return json({ error: "provider_unavailable" }, 503);

  let raw: Record<string, unknown>;
  try {
    raw = await response.json() as Record<string, unknown>;
  } catch {
    return json({ error: "provider_unavailable" }, 503);
  }
  const validation = validateProviderPlace(raw, {
    fsqPlaceId: place.fsq_place_id,
    name: place.name,
    latitude: place.latitude,
    longitude: place.longitude,
  });
  if (!validation.ok) {
    return json({ available: false, reason: "verification_failed" });
  }

  const details = projectLiveDetails(raw, requested);
  if (!hasLiveDetails(details)) {
    return json({ available: false, reason: "not_available" });
  }
  return json({
    available: true,
    provider: "foursquare",
    fetched_at: new Date().toISOString(),
    attribution: {
      name: "Foursquare",
      url: attributionUrl(raw, place.fsq_place_id),
    },
    ...details,
  });
}

function json(
  payload: Record<string, unknown>,
  status = 200,
  extraHeaders: Record<string, string> = {},
): Response {
  return Response.json(payload, {
    status,
    headers: { ...responseHeaders, ...extraHeaders },
  });
}

function safeErrorCode(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === "PostgresError") return "database_error";
    if (error.name === "AbortError") return "timeout";
  }
  return "unexpected_error";
}
