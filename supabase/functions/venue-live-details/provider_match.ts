import postgres from "npm:postgres@3.4.7";
import {
  providerRequestFingerprint,
  type RuntimeProviderLink,
} from "./provider_cache.ts";
import type { ProviderName } from "./provider_policy.ts";

type Sql = ReturnType<typeof postgres>;

const MATCH_LEASE_SECONDS = 15;
const MATCHED_RECHECK_SECONDS = 90 * 24 * 60 * 60;
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const DECISION_REASON_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const MATCHER_REVISION_PATTERN = /^[a-z][a-z0-9_]{0,99}$/;

export type ProviderMatchIdentity = Readonly<{
  name: string;
  address: string;
  city: string;
  region: string | null;
  postalCode: string | null;
  countryCode: string;
  latitude: number;
  longitude: number;
  phoneE164: string | null;
}>;

export type ProviderMatchLease = {
  token: string;
  identityFingerprint: string;
};

export type ProviderMatchOutcome = "not_found" | "rejected" | "error";

export async function providerMatchIdentityFingerprint(
  provider: ProviderName,
  identity: ProviderMatchIdentity,
  matcherRevision: string,
): Promise<string> {
  if (!MATCHER_REVISION_PATTERN.test(matcherRevision)) {
    throw new TypeError("invalid provider matcher revision");
  }
  return await providerRequestFingerprint({
    provider,
    endpoint: "business_match_identity",
    apiVersion: "v1",
    parameters: {
      matcher_revision: matcherRevision,
      name: identity.name.trim(),
      address: identity.address.trim(),
      city: identity.city.trim(),
      region: identity.region?.trim() ?? null,
      postal_code: identity.postalCode?.trim() ?? null,
      country_code: identity.countryCode.trim().toUpperCase(),
      latitude: roundedCoordinate(identity.latitude),
      longitude: roundedCoordinate(identity.longitude),
      phone_e164: identity.phoneE164,
    },
  });
}

export async function claimProviderMatch(
  sql: Sql,
  establishmentId: string,
  provider: ProviderName,
  identityFingerprint: string,
): Promise<ProviderMatchLease | null> {
  const token = crypto.randomUUID();
  const rows = await sql`
    insert into runtime.provider_match_state as match_state (
      establishment_id, provider, identity_fingerprint, outcome,
      attempted_at, retry_after, lease_token, lease_expires_at,
      last_error_code, decision_reason, updated_at
    ) values (
      ${establishmentId}::uuid, ${provider}, ${identityFingerprint}, 'pending',
      now(), now(), ${token}::uuid,
      now() + make_interval(secs => ${MATCH_LEASE_SECONDS}), null, null, now()
    )
    on conflict (establishment_id, provider) do update set
      identity_fingerprint = excluded.identity_fingerprint,
      outcome = 'pending',
      attempted_at = now(),
      retry_after = now(),
      lease_token = excluded.lease_token,
      lease_expires_at = excluded.lease_expires_at,
      last_error_code = null,
      decision_reason = null,
      updated_at = now()
    where match_state.identity_fingerprint <> excluded.identity_fingerprint
       or (
         match_state.retry_after <= now()
         and (
           match_state.lease_expires_at is null
           or match_state.lease_expires_at <= now()
         )
       )
    returning lease_token::text, identity_fingerprint
  `;
  const row = rows[0];
  if (!row || String(row.lease_token) !== token) return null;
  return {
    token,
    identityFingerprint: String(row.identity_fingerprint),
  };
}

export async function storeMatchedProviderLink(
  sql: Sql,
  establishmentId: string,
  provider: ProviderName,
  providerPlaceId: string,
  matchMethod: string,
  matchConfidence: number,
  lease: ProviderMatchLease,
  matchedAt = new Date(),
): Promise<RuntimeProviderLink | null> {
  const rows = await sql`
    with owned_lease as (
      select establishment_id, provider
      from runtime.provider_match_state
      where establishment_id = ${establishmentId}::uuid
        and provider = ${provider}
        and identity_fingerprint = ${lease.identityFingerprint}
        and lease_token = ${lease.token}::uuid
        and lease_expires_at > now()
    ), upserted as (
      insert into runtime.runtime_provider_links as runtime_link (
        establishment_id, provider, provider_place_id, match_method,
        match_confidence, matched_at, last_validated_at, retired_at, updated_at
      )
      select
        owned_lease.establishment_id, owned_lease.provider, ${providerPlaceId},
        ${matchMethod}, ${matchConfidence},
        ${matchedAt.toISOString()}::timestamptz,
        ${matchedAt.toISOString()}::timestamptz, null, now()
      from owned_lease
      on conflict (establishment_id, provider) do update set
        provider_place_id = excluded.provider_place_id,
        match_method = excluded.match_method,
        match_confidence = excluded.match_confidence,
        matched_at = excluded.matched_at,
        last_validated_at = excluded.last_validated_at,
        retired_at = null,
        updated_at = now()
      returning id, establishment_id, provider, provider_place_id
    ), finished as (
      update runtime.provider_match_state
      set outcome = 'matched',
          retry_after = now() + make_interval(secs => ${MATCHED_RECHECK_SECONDS}),
          lease_token = null,
          lease_expires_at = null,
          last_error_code = null,
          decision_reason = 'matched',
          updated_at = now()
      where establishment_id = ${establishmentId}::uuid
        and provider = ${provider}
        and identity_fingerprint = ${lease.identityFingerprint}
        and lease_token = ${lease.token}::uuid
        and exists (select 1 from upserted)
      returning establishment_id
    )
    select
      upserted.id::text,
      upserted.establishment_id::text,
      upserted.provider,
      upserted.provider_place_id
    from upserted
    join finished using (establishment_id)
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

export async function completeProviderMatch(
  sql: Sql,
  establishmentId: string,
  provider: ProviderName,
  lease: ProviderMatchLease,
  outcome: ProviderMatchOutcome,
  retryAfterSeconds: number,
  errorCode: string | null = null,
  decisionReason: string | null = null,
): Promise<void> {
  const boundedRetrySeconds = Math.max(
    60,
    Math.min(Math.floor(retryAfterSeconds), 90 * 24 * 60 * 60),
  );
  const safeErrorCode = validatedErrorCode(outcome, errorCode);
  const safeDecisionReason = validatedDecisionReason(
    decisionReason ?? outcome,
  );
  await sql`
    update runtime.provider_match_state
    set outcome = ${outcome},
        retry_after = now() + make_interval(secs => ${boundedRetrySeconds}),
        lease_token = null,
        lease_expires_at = null,
        last_error_code = ${safeErrorCode},
        decision_reason = ${safeDecisionReason},
        updated_at = now()
    where establishment_id = ${establishmentId}::uuid
      and provider = ${provider}
      and identity_fingerprint = ${lease.identityFingerprint}
      and lease_token = ${lease.token}::uuid
  `;
}

export async function deferProviderRematch(
  sql: Sql,
  establishmentId: string,
  provider: ProviderName,
  outcome: ProviderMatchOutcome,
  retryAfterSeconds: number,
  errorCode: string | null = null,
  decisionReason: string | null = null,
): Promise<void> {
  const boundedRetrySeconds = Math.max(
    60,
    Math.min(Math.floor(retryAfterSeconds), 90 * 24 * 60 * 60),
  );
  const safeErrorCode = validatedErrorCode(outcome, errorCode);
  const safeDecisionReason = validatedDecisionReason(
    decisionReason ?? outcome,
  );
  await sql`
    update runtime.provider_match_state
    set outcome = ${outcome},
        retry_after = now() + make_interval(secs => ${boundedRetrySeconds}),
        lease_token = null,
        lease_expires_at = null,
        last_error_code = ${safeErrorCode},
        decision_reason = ${safeDecisionReason},
        updated_at = now()
    where establishment_id = ${establishmentId}::uuid
      and provider = ${provider}
  `;
}

function validatedDecisionReason(value: string): string {
  return DECISION_REASON_PATTERN.test(value) ? value : "unclassified";
}

function validatedErrorCode(
  outcome: ProviderMatchOutcome,
  errorCode: string | null,
): string | null {
  if (outcome !== "error") return null;
  if (!errorCode || !ERROR_CODE_PATTERN.test(errorCode)) {
    return "unclassified";
  }
  return errorCode;
}

function roundedCoordinate(value: number): number {
  if (!Number.isFinite(value)) {
    throw new TypeError("provider match coordinates must be finite");
  }
  return Math.round(value * 100_000) / 100_000;
}
