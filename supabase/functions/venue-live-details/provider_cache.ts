import postgres, { type JSONValue } from "npm:postgres@3.4.7";
import {
  isServerCacheEntryFresh,
  providerPolicy,
  serverCacheExpiresAt,
} from "./provider_policy.ts";
import type { ProviderName } from "./provider_policy.ts";

type Sql = ReturnType<typeof postgres>;

const CACHE_TOKEN_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const ENDPOINT_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const REFRESH_LEASE_SECONDS = 20;
const CACHE_WAIT_DELAYS_MS = [100, 200, 400, 800, 1_200] as const;

export type RuntimeProviderLink = {
  id: string;
  establishmentId: string;
  provider: ProviderName;
  providerPlaceId: string;
};

export type CachedProviderResponse = {
  payload: Record<string, unknown>;
  fetchedAt: Date;
  expiresAt: Date;
};

export type ProviderRefreshLease = {
  token: string;
  expiresAt: Date;
};

export type ProviderRequestDescriptor = Readonly<{
  provider: ProviderName;
  endpoint: string;
  apiVersion: string;
  parameters?: Readonly<Record<string, unknown>>;
}>;

export type ProviderPayloadValidation =
  | { ok: true }
  | { ok: false; reason: string };

export type ProviderPayloadResult = {
  payload: Record<string, unknown>;
  fetchedAt: Date;
  expiresAt: Date | null;
  cacheStatus: "hit" | "miss" | "miss_unstored" | "bypass";
};

export interface ProviderCacheStore {
  read(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
  ): Promise<CachedProviderResponse | null>;
  claim(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
  ): Promise<ProviderRefreshLease | null>;
  store(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
    lease: ProviderRefreshLease,
    payload: Record<string, unknown>,
    fetchedAt: Date,
  ): Promise<CachedProviderResponse | null>;
  abandon(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
    lease: ProviderRefreshLease,
  ): Promise<void>;
  evict(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
  ): Promise<void>;
}

export class ProviderRefreshInProgressError extends Error {
  constructor() {
    super("a provider refresh is already in progress");
    this.name = "ProviderRefreshInProgressError";
  }
}

export class ProviderPayloadRejectedError extends Error {
  constructor(readonly reason: string) {
    super(`provider payload rejected: ${reason}`);
    this.name = "ProviderPayloadRejectedError";
  }
}

export class ProviderPayloadTooLargeError extends Error {
  constructor() {
    super("provider payload exceeds the reviewed cache limit");
    this.name = "ProviderPayloadTooLargeError";
  }
}

export class PostgresProviderCacheStore implements ProviderCacheStore {
  constructor(private readonly sql: Sql) {}

  read(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
  ): Promise<CachedProviderResponse | null> {
    return freshProviderResponse(
      this.sql,
      link,
      endpoint,
      requestFingerprint,
    );
  }

  claim(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
  ): Promise<ProviderRefreshLease | null> {
    return claimProviderRefresh(
      this.sql,
      link,
      endpoint,
      requestFingerprint,
    );
  }

  store(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
    lease: ProviderRefreshLease,
    payload: Record<string, unknown>,
    fetchedAt: Date,
  ): Promise<CachedProviderResponse | null> {
    return storeProviderResponse(
      this.sql,
      link,
      endpoint,
      requestFingerprint,
      lease,
      payload,
      fetchedAt,
    );
  }

  abandon(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
    lease: ProviderRefreshLease,
  ): Promise<void> {
    return abandonProviderRefresh(
      this.sql,
      link,
      endpoint,
      requestFingerprint,
      lease,
    );
  }

  evict(
    link: RuntimeProviderLink,
    endpoint: string,
    requestFingerprint: string,
  ): Promise<void> {
    return evictProviderResponse(
      this.sql,
      link,
      endpoint,
      requestFingerprint,
    );
  }
}

export async function loadProviderPayload(
  store: ProviderCacheStore,
  link: RuntimeProviderLink,
  request: ProviderRequestDescriptor,
  load: () => Promise<Record<string, unknown>>,
  validate: (
    payload: Record<string, unknown>,
  ) => ProviderPayloadValidation,
  options: {
    now?: () => Date;
    sleep?: (milliseconds: number) => Promise<void>;
  } = {},
): Promise<ProviderPayloadResult> {
  if (request.provider !== link.provider) {
    throw new TypeError("provider request and runtime link do not match");
  }
  validateEndpoint(request.endpoint);

  const policy = providerPolicy(link.provider);
  const now = options.now ?? (() => new Date());
  const sleep = options.sleep ?? delay;

  // A null TTL is an enforceable no-store path, not a zero-second cache. No
  // cache method is called, so a future adapter cannot accidentally persist a
  // forbidden response just by sharing this orchestration helper.
  if (policy.serverCacheTtlSeconds === null) {
    const fetchedAt = now();
    const payload = objectPayload(await load());
    if (!payload) throw new ProviderPayloadRejectedError("invalid_payload");
    assertValidPayload(payload, validate);
    return { payload, fetchedAt, expiresAt: null, cacheStatus: "bypass" };
  }

  const requestFingerprint = await providerRequestFingerprint(request);
  const cached = await store.read(
    link,
    request.endpoint,
    requestFingerprint,
  );
  if (cached) {
    const validation = validate(cached.payload);
    if (validation.ok) {
      return { ...cached, cacheStatus: "hit" };
    }
    await store.evict(link, request.endpoint, requestFingerprint);
    throw new ProviderPayloadRejectedError(validation.reason);
  }

  const lease = await store.claim(
    link,
    request.endpoint,
    requestFingerprint,
  );
  if (!lease) {
    for (const milliseconds of CACHE_WAIT_DELAYS_MS) {
      await sleep(milliseconds);
      const refreshed = await store.read(
        link,
        request.endpoint,
        requestFingerprint,
      );
      if (!refreshed) continue;
      const validation = validate(refreshed.payload);
      if (validation.ok) {
        return { ...refreshed, cacheStatus: "hit" };
      }
      await store.evict(link, request.endpoint, requestFingerprint);
      throw new ProviderPayloadRejectedError(validation.reason);
    }
    throw new ProviderRefreshInProgressError();
  }

  try {
    // Start the legal retention clock before the external call rather than
    // after it, so network latency can only shorten the stored lifetime.
    const fetchedAt = now();
    const payload = objectPayload(await load());
    if (!payload) throw new ProviderPayloadRejectedError("invalid_payload");
    assertPayloadSize(payload, policy.maxServerCachePayloadBytes);
    assertValidPayload(payload, validate);

    const stored = await store.store(
      link,
      request.endpoint,
      requestFingerprint,
      lease,
      payload,
      fetchedAt,
    );
    if (!stored) {
      return {
        payload,
        fetchedAt,
        expiresAt: null,
        cacheStatus: "miss_unstored",
      };
    }
    return { ...stored, cacheStatus: "miss" };
  } catch (error) {
    try {
      await store.abandon(
        link,
        request.endpoint,
        requestFingerprint,
        lease,
      );
    } catch {
      // The lease expires independently; never mask the provider error.
    }
    throw error;
  }
}

export async function providerRequestFingerprint(
  request: ProviderRequestDescriptor,
): Promise<string> {
  validateEndpoint(request.endpoint);
  const canonical = stableJson({
    provider: request.provider,
    endpoint: request.endpoint,
    api_version: request.apiVersion,
    parameters: request.parameters ?? {},
  });
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(canonical),
  );
  const hex = [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return `v1:${hex}`;
}

export async function runtimeProviderLink(
  sql: Sql,
  establishmentId: string,
  provider: ProviderName,
): Promise<RuntimeProviderLink | null> {
  const rows = await sql`
    select
      runtime_link.id::text,
      runtime_link.establishment_id::text,
      runtime_link.provider,
      runtime_link.provider_place_id
    from ingest.runtime_provider_links runtime_link
    join public.establishments establishment
      on establishment.id = runtime_link.establishment_id
    where runtime_link.establishment_id = ${establishmentId}::uuid
      and runtime_link.provider = ${provider}
      and runtime_link.retired_at is null
      and runtime_link.match_confidence >= 0.96
      and establishment.publication_state = 'published'
      and establishment.status = 'open'
      and establishment.access_mode = 'walk_in'
      and establishment.verification_tier in ('open_evidence', 'provider', 'manual')
      and establishment.verification_expires_at > now()
    limit 1
  `;
  const row = rows[0];
  if (!row) return null;
  return {
    id: String(row.id),
    establishmentId: String(row.establishment_id),
    provider: row.provider as ProviderName,
    providerPlaceId: String(row.provider_place_id),
  };
}

export async function touchRuntimeProviderLink(
  sql: Sql,
  link: RuntimeProviderLink,
  validatedAt: Date,
): Promise<void> {
  await sql`
    update ingest.runtime_provider_links
    set last_validated_at = greatest(
          coalesce(last_validated_at, ${validatedAt.toISOString()}::timestamptz),
          ${validatedAt.toISOString()}::timestamptz
        ),
        updated_at = now()
    where id = ${link.id}::bigint
      and provider = ${link.provider}
      and retired_at is null
  `;
}

export async function retireRuntimeProviderLink(
  sql: Sql,
  link: RuntimeProviderLink,
): Promise<void> {
  await sql`
    update ingest.runtime_provider_links
    set retired_at = now(), updated_at = now()
    where id = ${link.id}::bigint
      and provider = ${link.provider}
      and retired_at is null
  `;
}

async function freshProviderResponse(
  sql: Sql,
  link: RuntimeProviderLink,
  endpoint: string,
  requestFingerprint: string,
  now = new Date(),
): Promise<CachedProviderResponse | null> {
  validateCacheKey(endpoint, requestFingerprint);
  if (providerPolicy(link.provider).serverCacheTtlSeconds === null) return null;

  const rows = await sql`
    select cache.payload, cache.fetched_at, cache.expires_at
    from ingest.provider_response_cache cache
    join ingest.runtime_provider_links runtime_link
      on runtime_link.id = cache.provider_link_id
     and runtime_link.provider = cache.provider
    join public.establishments establishment
      on establishment.id = runtime_link.establishment_id
    where cache.provider_link_id = ${link.id}::bigint
      and cache.provider = ${link.provider}
      and cache.endpoint = ${endpoint}
      and cache.request_fingerprint = ${requestFingerprint}
      and cache.expires_at > now()
      and runtime_link.retired_at is null
      and establishment.publication_state = 'published'
      and establishment.status = 'open'
      and establishment.access_mode = 'walk_in'
      and establishment.verification_expires_at > now()
    limit 1
  `;
  const row = rows[0];
  if (!row) return null;

  const payload = objectPayload(row.payload);
  const fetchedAt = new Date(String(row.fetched_at));
  const expiresAt = new Date(String(row.expires_at));
  if (
    !payload ||
    !isServerCacheEntryFresh(link.provider, fetchedAt, expiresAt, now)
  ) {
    return null;
  }
  return { payload, fetchedAt, expiresAt };
}

async function claimProviderRefresh(
  sql: Sql,
  link: RuntimeProviderLink,
  endpoint: string,
  requestFingerprint: string,
): Promise<ProviderRefreshLease | null> {
  validateCacheKey(endpoint, requestFingerprint);
  if (providerPolicy(link.provider).serverCacheTtlSeconds === null) return null;

  const token = crypto.randomUUID();
  const rows = await sql`
    insert into ingest.provider_refresh_leases as leases (
      provider_link_id, provider, endpoint, request_fingerprint,
      lease_token, lease_expires_at, updated_at
    ) values (
      ${link.id}::bigint, ${link.provider}, ${endpoint}, ${requestFingerprint},
      ${token}::uuid, now() + make_interval(secs => ${REFRESH_LEASE_SECONDS}), now()
    )
    on conflict (provider_link_id, endpoint, request_fingerprint) do update set
      provider = excluded.provider,
      lease_token = excluded.lease_token,
      lease_expires_at = excluded.lease_expires_at,
      updated_at = now()
    where leases.lease_expires_at <= now()
    returning lease_token::text, lease_expires_at
  `;
  const row = rows[0];
  if (!row || String(row.lease_token) !== token) return null;
  return { token, expiresAt: new Date(String(row.lease_expires_at)) };
}

async function storeProviderResponse(
  sql: Sql,
  link: RuntimeProviderLink,
  endpoint: string,
  requestFingerprint: string,
  lease: ProviderRefreshLease,
  payload: Record<string, unknown>,
  fetchedAt = new Date(),
): Promise<CachedProviderResponse | null> {
  validateCacheKey(endpoint, requestFingerprint);
  const policy = providerPolicy(link.provider);
  assertPayloadSize(payload, policy.maxServerCachePayloadBytes);
  const expiresAt = serverCacheExpiresAt(link.provider, fetchedAt);
  // Postgres.js serializes parameters typed as json/jsonb. Passing an already
  // stringified value here would encode it a second time and turn the JSON
  // object into a JSON string, which the database correctly rejects.
  const payloadParameter = providerCacheJsonParameter(sql, payload);

  const rows = await sql`
    with owned_lease as (
      delete from ingest.provider_refresh_leases
      where provider_link_id = ${link.id}::bigint
        and provider = ${link.provider}
        and endpoint = ${endpoint}
        and request_fingerprint = ${requestFingerprint}
        and lease_token = ${lease.token}::uuid
        and lease_expires_at > now()
      returning provider_link_id
    )
    insert into ingest.provider_response_cache as cache (
      provider_link_id, provider, endpoint, request_fingerprint,
      payload, fetched_at, expires_at, created_at, updated_at
    )
    select
      ${link.id}::bigint, ${link.provider}, ${endpoint}, ${requestFingerprint},
      ${payloadParameter}, ${fetchedAt.toISOString()}::timestamptz,
      ${expiresAt.toISOString()}::timestamptz, now(), now()
    from owned_lease
    on conflict (provider_link_id, endpoint, request_fingerprint) do update set
      provider = excluded.provider,
      payload = excluded.payload,
      fetched_at = excluded.fetched_at,
      expires_at = excluded.expires_at,
      updated_at = now()
    returning payload, fetched_at, expires_at
  `;
  const row = rows[0];
  if (!row) return null;
  const storedPayload = objectPayload(row.payload);
  if (!storedPayload) return null;
  return {
    payload: storedPayload,
    fetchedAt: new Date(String(row.fetched_at)),
    expiresAt: new Date(String(row.expires_at)),
  };
}

export function providerCacheJsonParameter<T>(
  sql: { json(value: JSONValue): T },
  payload: Record<string, unknown>,
): T {
  // Provider payloads enter through response.json()/JSON.parse and cached rows
  // re-enter through Postgres JSONB, so objectPayload has already narrowed a
  // JSON object at both boundaries.
  return sql.json(payload as unknown as JSONValue);
}

async function abandonProviderRefresh(
  sql: Sql,
  link: RuntimeProviderLink,
  endpoint: string,
  requestFingerprint: string,
  lease: ProviderRefreshLease,
): Promise<void> {
  validateCacheKey(endpoint, requestFingerprint);
  await sql`
    delete from ingest.provider_refresh_leases
    where provider_link_id = ${link.id}::bigint
      and provider = ${link.provider}
      and endpoint = ${endpoint}
      and request_fingerprint = ${requestFingerprint}
      and lease_token = ${lease.token}::uuid
  `;
}

async function evictProviderResponse(
  sql: Sql,
  link: RuntimeProviderLink,
  endpoint: string,
  requestFingerprint: string,
): Promise<void> {
  validateCacheKey(endpoint, requestFingerprint);
  await sql`
    delete from ingest.provider_response_cache
    where provider_link_id = ${link.id}::bigint
      and provider = ${link.provider}
      and endpoint = ${endpoint}
      and request_fingerprint = ${requestFingerprint}
  `;
}

function assertValidPayload(
  payload: Record<string, unknown>,
  validate: (
    payload: Record<string, unknown>,
  ) => ProviderPayloadValidation,
): void {
  const validation = validate(payload);
  if (!validation.ok) {
    throw new ProviderPayloadRejectedError(validation.reason);
  }
}

function assertPayloadSize(
  payload: Record<string, unknown>,
  maximumBytes: number | null,
): void {
  if (maximumBytes === null) return;
  const bytes = new TextEncoder().encode(JSON.stringify(payload)).byteLength;
  if (bytes > maximumBytes) throw new ProviderPayloadTooLargeError();
}

function validateEndpoint(endpoint: string): void {
  if (!ENDPOINT_PATTERN.test(endpoint)) {
    throw new TypeError("invalid provider cache endpoint");
  }
}

function validateCacheKey(endpoint: string, requestFingerprint: string): void {
  validateEndpoint(endpoint);
  if (!CACHE_TOKEN_PATTERN.test(requestFingerprint)) {
    throw new TypeError("invalid provider request fingerprint");
  }
}

function objectPayload(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function stableJson(value: unknown): string {
  return JSON.stringify(canonicalValue(value));
}

function canonicalValue(value: unknown): unknown {
  if (
    value === null || typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (typeof value === "object") {
    const row = value as Record<string, unknown>;
    return Object.fromEntries(
      Object.keys(row).sort().filter((key) => row[key] !== undefined).map((
        key,
      ) => [key, canonicalValue(row[key])]),
    );
  }
  throw new TypeError("provider request contains a non-canonical value");
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
