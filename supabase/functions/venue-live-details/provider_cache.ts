import postgres from "npm:postgres@3.4.7";
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

export async function runtimeProviderLink(
  sql: Sql,
  establishmentId: string,
  provider: ProviderName,
): Promise<RuntimeProviderLink | null> {
  const rows = await sql`
    select id::text, establishment_id::text, provider, provider_place_id
    from ingest.runtime_provider_links
    where establishment_id = ${establishmentId}::uuid
      and provider = ${provider}
      and retired_at is null
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

export async function freshProviderResponse(
  sql: Sql,
  link: RuntimeProviderLink,
  endpoint: string,
  requestFingerprint: string,
  now = new Date(),
): Promise<CachedProviderResponse | null> {
  validateCacheKey(endpoint, requestFingerprint);
  if (providerPolicy(link.provider).serverCacheTtlSeconds === null) return null;

  const rows = await sql`
    select payload, fetched_at, expires_at
    from ingest.provider_response_cache
    where provider_link_id = ${link.id}::bigint
      and provider = ${link.provider}
      and endpoint = ${endpoint}
      and request_fingerprint = ${requestFingerprint}
      and expires_at > now()
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

export async function claimProviderRefresh(
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

export async function storeProviderResponse(
  sql: Sql,
  link: RuntimeProviderLink,
  endpoint: string,
  requestFingerprint: string,
  lease: ProviderRefreshLease,
  payload: Record<string, unknown>,
  fetchedAt = new Date(),
): Promise<CachedProviderResponse | null> {
  validateCacheKey(endpoint, requestFingerprint);
  const expiresAt = serverCacheExpiresAt(link.provider, fetchedAt);
  const serializedPayload = JSON.stringify(payload);

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
      ${serializedPayload}::jsonb, ${fetchedAt.toISOString()}::timestamptz,
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

export async function abandonProviderRefresh(
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

function validateCacheKey(endpoint: string, requestFingerprint: string): void {
  if (!ENDPOINT_PATTERN.test(endpoint)) {
    throw new TypeError("invalid provider cache endpoint");
  }
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
