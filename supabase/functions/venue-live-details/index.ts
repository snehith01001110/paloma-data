import "jsr:@supabase/functions-js@2.5.0/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.7";
import {
  attributionUrl,
  hasLiveDetails,
  type LiveFieldRequest,
  projectLiveDetails,
  providerFields,
  validateProviderPlace,
} from "./domain.ts";
import {
  hasRequestedLiveFields,
  liveDetailsResponse,
  missingLiveFields,
  type ProviderLiveDetails,
} from "./live_details_merge.ts";
import {
  loadProviderPayload,
  PostgresProviderCacheStore,
  ProviderPayloadRejectedError,
  ProviderRefreshInProgressError,
  retireRuntimeProviderLink,
  type RuntimeProviderLink,
  runtimeProviderLink,
  touchRuntimeProviderLink,
} from "./provider_cache.ts";
import {
  claimProviderMatch,
  completeProviderMatch,
  deferProviderRematch,
  providerMatchIdentityFingerprint,
  type ProviderMatchLease,
  storeMatchedProviderLink,
} from "./provider_match.ts";
import { assertNoServerResponseCache } from "./provider_policy.ts";
import {
  fetchYelpBusinessCandidates,
  fetchYelpBusinessDetails,
  projectYelpLiveDetails,
  selectYelpBusinessMatch,
  validateYelpPlace,
  YelpApiError,
  yelpAttributionUrl,
  yelpBusinessId,
} from "./yelp.ts";

declare const EdgeRuntime: {
  waitUntil(promise: Promise<unknown>): void;
};

const PLACES_API_VERSION = "2025-06-17";
const MAX_BODY_BYTES = 1_024;
const YELP_MATCH_METHOD = "api_business_search_verified_v2";
const MAX_PROVIDER_RESPONSE_BYTES = 512 * 1_024;
const USER_REQUESTS_PER_MINUTE = 20;
const USER_REQUESTS_PER_DAY = 100;
const GLOBAL_REQUESTS_PER_SECOND = 15;
const FSQ_TIMEOUT_MS = 5_500;
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
  // The shared server cache is private and policy-bounded. Licensed responses
  // are still never cached by browsers, CDNs, or the default URLSession cache.
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
  address: string;
  city: string;
  region: string | null;
  postalCode: string | null;
  countryCode: string;
  latitude: number;
  longitude: number;
  phoneE164: string | null;
  fsqPlaceId: string;
  needsPhone: boolean;
  needsWebsite: boolean;
  needsHours: boolean;
  needsPrice: boolean;
  needsSettings: boolean;
};

class ProviderQuotaExceededError extends Error {
  constructor() {
    super("provider quota exceeded");
    this.name = "ProviderQuotaExceededError";
  }
}

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
  const fsqApiKey = Deno.env.get("FSQ_PLACES_API_KEY");
  const yelpApiKey = Deno.env.get("YELP_API_KEY");
  if (!databaseUrl || (!fsqApiKey && !yelpApiKey)) {
    return json({ error: "temporarily_unavailable" }, 503);
  }

  const sql = databaseClient(databaseUrl);
  let place: EligiblePlace | null = null;
  let requested: LiveFieldRequest | null = null;
  let foursquareRequested: LiveFieldRequest | null = null;
  const providerResults: ProviderLiveDetails[] = [];
  let shouldDiscoverYelp = false;

  try {
    await assumeRuntimeRole(sql);
    place = await eligiblePlace(sql, establishmentId);
    if (!place) {
      return json({ available: false, reason: "not_available" }, 404);
    }

    requested = requestedFields(place);
    if (!hasRequestedLiveFields(requested)) {
      return json({ available: false, reason: "durable_details_complete" });
    }

    const yelpLink = yelpApiKey
      ? await runtimeProviderLink(sql, establishmentId, "yelp")
      : null;
    if (yelpApiKey && yelpLink) {
      try {
        const yelpResult = await yelpLiveDetails(
          sql,
          userId,
          place,
          requested,
          yelpApiKey,
          yelpLink,
        );
        if (yelpResult) providerResults.push(yelpResult);
      } catch (error) {
        if (error instanceof ProviderQuotaExceededError) {
          return json({ error: "rate_limited" }, 429, {
            "Retry-After": "60",
          });
        }
        await handleYelpLinkFailure(sql, yelpLink, error);
        logProviderEvent("yelp", providerFailureCode(error));
      }
    } else if (yelpApiKey) {
      shouldDiscoverYelp = true;
    }

    foursquareRequested = missingLiveFields(requested, providerResults);
    if (!hasRequestedLiveFields(foursquareRequested)) {
      return liveDetailsJson(liveDetailsResponse(providerResults)!);
    }
    if (!fsqApiKey) {
      const partial = liveDetailsResponse(providerResults);
      return partial
        ? liveDetailsJson(partial)
        : json({ error: "provider_unavailable" }, 503);
    }
    if (!await consumeQuota(sql, userId)) {
      const partial = liveDetailsResponse(providerResults);
      return partial
        ? liveDetailsJson(partial)
        : json({ error: "rate_limited" }, 429, { "Retry-After": "60" });
    }
  } catch (error) {
    console.error("venue-live-details", safeErrorCode(error));
    return json({ error: "temporarily_unavailable" }, 503);
  } finally {
    await sql.end({ timeout: 1 });
  }

  const foursquareOutcome = await fetchFoursquareLiveDetails(
    place,
    foursquareRequested,
    fsqApiKey,
  );
  if (shouldDiscoverYelp && yelpApiKey) {
    // Yelp discovery is user-triggered but does not delay the first detail
    // screen. It stores only the durable Yelp business ID; rich details are
    // fetched on a later user request and then cached within policy.
    EdgeRuntime.waitUntil(
      discoverYelpLink(databaseUrl, userId, place, yelpApiKey),
    );
  }
  if (foursquareOutcome.result) {
    providerResults.push(foursquareOutcome.result);
  }
  const merged = liveDetailsResponse(providerResults);
  return merged ? liveDetailsJson(merged) : foursquareOutcome.response;
});

function databaseClient(databaseUrl: string): Sql {
  return postgres(databaseUrl, {
    max: 1,
    prepare: false,
    idle_timeout: 2,
    connect_timeout: 5,
  });
}

async function assumeRuntimeRole(sql: Sql): Promise<void> {
  await sql`set role paloma_runtime`;
}

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
      e.address,
      e.city,
      e.region,
      e.postal_code,
      btrim(e.country_code::text) as country_code,
      e.phone_e164,
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
      left join ingest.source_records source_record
        on source_record.source = link.source
       and source_record.source_record_id = link.source_record_id
      where link.candidate_id = candidate.id
        and link.source = 'fsq'
        and link.identity_confidence >= 0.96
        and (
          (
            source_record.source_record_id is not null
            and source_record.retired_at is null
            and source_record.source_status = 'open'
            and source_record.consumer_facing
            and source_record.public_access = 'walk_in'
            and not (source_record.quality_flags && array[
              'closed', 'delete', 'doesnt_exist', 'does_not_exist',
              'duplicate', 'inappropriate', 'privatevenue', 'private_venue'
            ]::text[])
          )
          or (
            -- A reviewed consumer-identity exception may use the exact FSQ anchor
            -- created by the manual-verification gate. The source row is intentionally
            -- hidden from this role because its coarse taxonomy is the conflict being
            -- overridden; the worker-controlled link method is the narrow allow-list.
            link.match_method = 'reviewed_identity_exception:anchor_source_id'
            and candidate.anchor_source = 'fsq'
            and candidate.anchor_source_record_id = link.source_record_id
            and e.verification_tier = 'manual'
          )
        )
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
      and e.access_mode = 'walk_in'
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
    address: String(row.address),
    city: String(row.city),
    region: nullableText(row.region),
    postalCode: nullableText(row.postal_code),
    countryCode: String(row.country_code),
    latitude: Number(row.latitude),
    longitude: Number(row.longitude),
    phoneE164: nullableText(row.phone_e164),
    fsqPlaceId: String(row.fsq_place_id),
    needsPhone: Boolean(row.needs_phone),
    needsWebsite: Boolean(row.needs_website),
    needsHours: Boolean(row.needs_hours),
    needsPrice: Boolean(row.needs_price),
    needsSettings: Boolean(row.needs_settings),
  };
}

function requestedFields(place: EligiblePlace): LiveFieldRequest {
  return {
    phone: place.needsPhone,
    website: place.needsWebsite,
    hours: place.needsHours,
    price: place.needsPrice,
    settings: place.needsSettings,
  };
}

async function yelpLiveDetails(
  sql: Sql,
  userId: string,
  place: EligiblePlace,
  requested: LiveFieldRequest,
  apiKey: string,
  link: RuntimeProviderLink,
): Promise<ProviderLiveDetails | null> {
  const expected = {
    yelpBusinessId: link.providerPlaceId,
    name: place.name,
    latitude: place.latitude,
    longitude: place.longitude,
    phoneE164: place.phoneE164,
  };
  const loaded = await loadProviderPayload(
    new PostgresProviderCacheStore(sql),
    link,
    {
      provider: "yelp",
      endpoint: "business_details",
      apiVersion: "v3",
      parameters: { locale: "en_US" },
    },
    async () => {
      if (!await consumeQuota(sql, userId)) {
        throw new ProviderQuotaExceededError();
      }
      return await fetchYelpBusinessDetails(apiKey, link.providerPlaceId);
    },
    (payload) => {
      const validation = validateYelpPlace(payload, expected);
      return validation.ok
        ? { ok: true }
        : { ok: false, reason: validation.reason };
    },
  );

  await touchRuntimeProviderLink(sql, link, loaded.fetchedAt);
  const details = projectYelpLiveDetails(loaded.payload, requested);
  const attribution = yelpAttributionUrl(loaded.payload);
  if (!attribution) return null;

  logProviderEvent("yelp", loaded.cacheStatus);
  return {
    provider: "yelp",
    cacheStatus: loaded.cacheStatus,
    fetchedAt: loaded.fetchedAt.toISOString(),
    expiresAt: loaded.expiresAt?.toISOString() ?? null,
    attribution: { name: "Yelp", url: attribution },
    details,
  };
}

async function handleYelpLinkFailure(
  sql: Sql,
  link: RuntimeProviderLink,
  error: unknown,
): Promise<void> {
  const invalidPayload = error instanceof ProviderPayloadRejectedError;
  const missingBusiness = error instanceof YelpApiError &&
    error.code === "not_found";
  if (!invalidPayload && !missingBusiness) return;

  await retireRuntimeProviderLink(sql, link);
  await deferProviderRematch(
    sql,
    link.establishmentId,
    "yelp",
    "rejected",
    missingBusiness ? 7 * 24 * 60 * 60 : 30 * 24 * 60 * 60,
    null,
    missingBusiness ? "linked_business_not_found" : "linked_payload_rejected",
  );
}

async function discoverYelpLink(
  databaseUrl: string,
  userId: string,
  place: EligiblePlace,
  apiKey: string,
): Promise<void> {
  const sql = databaseClient(databaseUrl);
  let lease: ProviderMatchLease | null = null;
  try {
    await assumeRuntimeRole(sql);
    if (await runtimeProviderLink(sql, place.id, "yelp")) return;

    const matchInput = {
      name: place.name,
      address: place.address,
      city: place.city,
      region: place.region,
      postalCode: place.postalCode,
      countryCode: place.countryCode,
      latitude: place.latitude,
      longitude: place.longitude,
      phoneE164: place.phoneE164,
    };
    const identityFingerprint = await providerMatchIdentityFingerprint(
      "yelp",
      matchInput,
      YELP_MATCH_METHOD,
    );
    lease = await claimProviderMatch(
      sql,
      place.id,
      "yelp",
      identityFingerprint,
    );
    if (!lease) return;

    if (!await consumeQuota(sql, userId)) {
      await completeProviderMatch(
        sql,
        place.id,
        "yelp",
        lease,
        "error",
        60,
        "quota_exceeded",
        "provider_error",
      );
      return;
    }
    const payload = await fetchYelpBusinessCandidates(apiKey, matchInput);
    const selection = selectYelpBusinessMatch(payload, {
      name: place.name,
      latitude: place.latitude,
      longitude: place.longitude,
      phoneE164: place.phoneE164,
    });
    if (!selection.ok) {
      const retrySeconds = selection.reason === "not_found"
        ? 7 * 24 * 60 * 60
        : 30 * 24 * 60 * 60;
      await completeProviderMatch(
        sql,
        place.id,
        "yelp",
        lease,
        selection.reason === "not_found" ? "not_found" : "rejected",
        retrySeconds,
        null,
        selection.reason === "ambiguous"
          ? "ambiguous_multiple_candidates"
          : selection.reason,
      );
      logProviderEvent("yelp_match", selection.reason);
      return;
    }

    const businessId = yelpBusinessId(selection.business);
    if (!businessId) {
      await completeProviderMatch(
        sql,
        place.id,
        "yelp",
        lease,
        "rejected",
        30 * 24 * 60 * 60,
        null,
        "invalid_provider_identity",
      );
      return;
    }
    const link = await storeMatchedProviderLink(
      sql,
      place.id,
      "yelp",
      businessId,
      YELP_MATCH_METHOD,
      selection.confidence,
      lease,
    );
    if (!link) {
      await completeProviderMatch(
        sql,
        place.id,
        "yelp",
        lease,
        "error",
        300,
        "link_store_failed",
        "provider_error",
      );
      return;
    }
    logProviderEvent("yelp_match", "matched");
  } catch (error) {
    if (lease) {
      try {
        await completeProviderMatch(
          sql,
          place.id,
          "yelp",
          lease,
          "error",
          providerMatchRetrySeconds(error),
          providerFailureCode(error),
          "provider_error",
        );
      } catch {
        // The match lease expires on its own; preserve the original error code.
      }
    }
    logProviderEvent("yelp_match", providerFailureCode(error));
  } finally {
    await sql.end({ timeout: 1 });
  }
}

function providerMatchRetrySeconds(error: unknown): number {
  if (!(error instanceof YelpApiError)) return 5 * 60;
  switch (error.code) {
    case "unauthorized":
    case "forbidden":
      return 60 * 60;
    case "invalid_request":
      return 24 * 60 * 60;
    case "rate_limited":
      return 15 * 60;
    case "invalid_payload":
      return 30 * 60;
    default:
      return 5 * 60;
  }
}

async function consumeQuota(sql: Sql, userId: string): Promise<boolean> {
  const userRows = await sql`
    insert into runtime.live_detail_user_limits as limits (
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
    insert into runtime.live_detail_global_limit as limits (
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

async function fetchFoursquareLiveDetails(
  place: EligiblePlace,
  requested: LiveFieldRequest,
  serviceKey: string,
): Promise<{ result: ProviderLiveDetails | null; response: Response }> {
  const url = new URL(
    `https://places-api.foursquare.com/places/${
      encodeURIComponent(place.fsqPlaceId)
    }`,
  );
  url.searchParams.set("fields", providerFields(requested).join(","));
  if (requested.phone) url.searchParams.set("tel_format", "E164");

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), FSQ_TIMEOUT_MS);
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
    return {
      result: null,
      response: json({ error: "provider_unavailable" }, 503),
    };
  } finally {
    clearTimeout(timeout);
  }

  if (response.status === 404) {
    return {
      result: null,
      response: json({ available: false, reason: "not_available" }),
    };
  }
  if (response.status === 429) {
    return {
      result: null,
      response: json({ error: "provider_busy" }, 503, { "Retry-After": "60" }),
    };
  }
  if (!response.ok) {
    return {
      result: null,
      response: json({ error: "provider_unavailable" }, 503),
    };
  }

  const raw = await boundedJsonObject(response);
  if (!raw) {
    return {
      result: null,
      response: json({ error: "provider_unavailable" }, 503),
    };
  }
  const validation = validateProviderPlace(raw, {
    fsqPlaceId: place.fsqPlaceId,
    name: place.name,
    latitude: place.latitude,
    longitude: place.longitude,
  });
  if (!validation.ok) {
    return {
      result: null,
      response: json({ available: false, reason: "verification_failed" }),
    };
  }

  const details = projectLiveDetails(raw, requested);
  if (!hasLiveDetails(details)) {
    return {
      result: null,
      response: json({ available: false, reason: "not_available" }),
    };
  }
  const fetchedAt = new Date().toISOString();
  logProviderEvent("foursquare", "bypass");
  return {
    result: {
      provider: "foursquare",
      cacheStatus: "bypass",
      fetchedAt,
      expiresAt: null,
      attribution: {
        name: "Foursquare",
        url: attributionUrl(raw, place.fsqPlaceId),
      },
      details,
    },
    // Used only when no provider supplied a displayable field.
    response: json({ available: false, reason: "not_available" }),
  };
}

async function boundedJsonObject(
  response: Response,
): Promise<Record<string, unknown> | null> {
  const contentLength = Number(response.headers.get("content-length") ?? 0);
  if (
    Number.isFinite(contentLength) &&
    contentLength > MAX_PROVIDER_RESPONSE_BYTES
  ) {
    return null;
  }
  try {
    const body = await response.text();
    if (
      new TextEncoder().encode(body).byteLength > MAX_PROVIDER_RESPONSE_BYTES
    ) {
      return null;
    }
    const value = JSON.parse(body);
    return value !== null && typeof value === "object" && !Array.isArray(value)
      ? value as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
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

function liveDetailsJson(payload: Record<string, unknown>): Response {
  const rawSources = payload.field_sources;
  const sources = rawSources !== null && typeof rawSources === "object" &&
      !Array.isArray(rawSources)
    ? rawSources as Record<string, unknown>
    : {};
  const fields = Object.entries(sources)
    .filter(([, provider]) => typeof provider === "string")
    .map(([field]) => field)
    .sort();
  const attributions = Array.isArray(payload.attributions)
    ? payload.attributions.length
    : 0;
  // Aggregate-safe diagnostics only: no establishment/provider IDs, URLs,
  // names, payload values, or user identifiers.
  console.info("venue-live-details-result", {
    provider_mode: payload.provider,
    field_count: fields.length,
    fields,
    attribution_count: attributions,
  });
  return json(payload);
}

function providerFailureCode(error: unknown): string {
  if (error instanceof ProviderRefreshInProgressError) return "refresh_busy";
  if (error instanceof ProviderPayloadRejectedError) {
    return `rejected_${error.reason}`;
  }
  if (error instanceof YelpApiError) {
    return error.providerCode ?? error.code;
  }
  if (error instanceof ProviderQuotaExceededError) return "quota_exceeded";
  return safeErrorCode(error);
}

function logProviderEvent(provider: string, outcome: string): void {
  // Deliberately exclude establishment IDs, provider IDs, request URLs, and
  // payloads. Aggregate provider/cache outcomes are sufficient for operations.
  console.info("venue-live-details-provider", { provider, outcome });
}

function nullableText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed || null;
}

function safeErrorCode(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === "PostgresError") return "database_error";
    if (error.name === "AbortError") return "timeout";
  }
  return "unexpected_error";
}
