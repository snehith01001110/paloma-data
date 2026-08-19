export type ProviderName = "foursquare" | "yelp";

export type ProviderPolicy = Readonly<{
  provider: ProviderName;
  serverCacheTtlSeconds: number | null;
  durableIdentifierKinds: readonly string[];
  policyReviewedOn: string;
}>;

export const YELP_SERVER_CACHE_TTL_SECONDS = 23 * 60 * 60;

const PROVIDER_POLICIES: Readonly<Record<ProviderName, ProviderPolicy>> = {
  foursquare: Object.freeze({
    provider: "foursquare",
    serverCacheTtlSeconds: null,
    durableIdentifierKinds: Object.freeze([
      "fsq_place_id",
      "fsq_photo_id",
      "fsq_addr_id",
    ]),
    policyReviewedOn: "2026-08-18",
  }),
  yelp: Object.freeze({
    provider: "yelp",
    // Yelp permits at most 24 hours. Paloma uses 23 hours so clock and purge
    // delays cannot accidentally extend a response beyond the contractual cap.
    serverCacheTtlSeconds: YELP_SERVER_CACHE_TTL_SECONDS,
    durableIdentifierKinds: Object.freeze(["business_id"]),
    policyReviewedOn: "2026-08-18",
  }),
};

export class ProviderResponseCacheForbiddenError extends Error {
  constructor(provider: ProviderName) {
    super(`${provider} server response caching is not permitted`);
    this.name = "ProviderResponseCacheForbiddenError";
  }
}

export function providerPolicy(provider: ProviderName): ProviderPolicy {
  return PROVIDER_POLICIES[provider];
}

export function assertNoServerResponseCache(provider: ProviderName): void {
  if (providerPolicy(provider).serverCacheTtlSeconds !== null) {
    throw new Error(`${provider} was expected to use a no-store path`);
  }
}

export function serverCacheExpiresAt(
  provider: ProviderName,
  fetchedAt: Date,
): Date {
  const ttlSeconds = providerPolicy(provider).serverCacheTtlSeconds;
  if (ttlSeconds === null) {
    throw new ProviderResponseCacheForbiddenError(provider);
  }
  if (!Number.isFinite(fetchedAt.getTime())) {
    throw new TypeError("fetchedAt must be a valid date");
  }
  return new Date(fetchedAt.getTime() + ttlSeconds * 1_000);
}

export function isServerCacheEntryFresh(
  provider: ProviderName,
  fetchedAt: Date,
  expiresAt: Date,
  now = new Date(),
): boolean {
  const ttlSeconds = providerPolicy(provider).serverCacheTtlSeconds;
  if (ttlSeconds === null) return false;
  const timestamps = [fetchedAt, expiresAt, now].map((value) =>
    value.getTime()
  );
  if (timestamps.some((value) => !Number.isFinite(value))) return false;

  const [fetchedAtMs, expiresAtMs, nowMs] = timestamps;
  const policyExpiryMs = fetchedAtMs + ttlSeconds * 1_000;
  return fetchedAtMs <= nowMs &&
    expiresAtMs > nowMs &&
    expiresAtMs <= policyExpiryMs;
}
