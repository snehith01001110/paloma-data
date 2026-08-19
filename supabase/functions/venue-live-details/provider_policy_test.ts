import {
  assertNoServerResponseCache,
  isServerCacheEntryFresh,
  providerPolicy,
  ProviderResponseCacheForbiddenError,
  serverCacheExpiresAt,
  YELP_SERVER_CACHE_TTL_SECONDS,
} from "./provider_policy.ts";

Deno.test("Foursquare IDs are durable but its PAYG response cache is disabled", () => {
  const policy = providerPolicy("foursquare");
  assertEquals(policy.serverCacheTtlSeconds, null);
  assert(policy.durableIdentifierKinds.includes("fsq_place_id"));
  assertNoServerResponseCache("foursquare");
  assertThrows(
    () => serverCacheExpiresAt("foursquare", new Date()),
    ProviderResponseCacheForbiddenError,
  );
});

Deno.test("Yelp cache entries expire after exactly the 23-hour safety window", () => {
  const fetchedAt = new Date("2026-08-18T12:00:00.000Z");
  const expiresAt = serverCacheExpiresAt("yelp", fetchedAt);
  assertEquals(
    expiresAt.toISOString(),
    "2026-08-19T11:00:00.000Z",
  );
  assertEquals(
    providerPolicy("yelp").serverCacheTtlSeconds,
    YELP_SERVER_CACHE_TTL_SECONDS,
  );
});

Deno.test("cache freshness fails closed for overlong, expired, and FSQ rows", () => {
  const fetchedAt = new Date("2026-08-18T12:00:00.000Z");
  const allowedExpiry = new Date("2026-08-19T11:00:00.000Z");
  assert(
    isServerCacheEntryFresh(
      "yelp",
      fetchedAt,
      allowedExpiry,
      new Date("2026-08-19T10:59:59.000Z"),
    ),
  );
  assert(
    !isServerCacheEntryFresh(
      "yelp",
      fetchedAt,
      new Date("2026-08-19T12:00:00.000Z"),
      new Date("2026-08-18T13:00:00.000Z"),
    ),
  );
  assert(
    !isServerCacheEntryFresh(
      "yelp",
      fetchedAt,
      allowedExpiry,
      allowedExpiry,
    ),
  );
  assert(
    !isServerCacheEntryFresh(
      "foursquare",
      fetchedAt,
      allowedExpiry,
      new Date("2026-08-18T13:00:00.000Z"),
    ),
  );
});

function assert(value: unknown): asserts value {
  if (!value) throw new Error(`assertion failed: ${String(value)}`);
}

function assertEquals(actual: unknown, expected: unknown): void {
  if (actual !== expected) {
    throw new Error(`expected ${String(expected)}, received ${String(actual)}`);
  }
}

function assertThrows(
  callback: () => unknown,
  expectedError: new (...args: never[]) => Error,
): void {
  try {
    callback();
  } catch (error) {
    if (error instanceof expectedError) return;
    throw error;
  }
  throw new Error("expected callback to throw");
}
