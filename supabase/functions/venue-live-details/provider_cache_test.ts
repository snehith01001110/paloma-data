import {
  type CachedProviderResponse,
  loadProviderPayload,
  providerCacheJsonParameter,
  type ProviderCacheStore,
  ProviderPayloadRejectedError,
  ProviderPayloadTooLargeError,
  type ProviderRefreshLease,
  providerRequestFingerprint,
  type RuntimeProviderLink,
} from "./provider_cache.ts";
import { serverCacheExpiresAt } from "./provider_policy.ts";

const yelpLink: RuntimeProviderLink = {
  id: "1",
  establishmentId: "00000000-0000-4000-8000-000000000001",
  provider: "yelp",
  providerPlaceId: "yelp-business",
};

const foursquareLink: RuntimeProviderLink = {
  ...yelpLink,
  provider: "foursquare",
  providerPlaceId: "fsq-place",
};

const request = {
  provider: "yelp" as const,
  endpoint: "business_details",
  apiVersion: "v3",
  parameters: { locale: "en_US" },
};

Deno.test("cache writes preserve payloads as JSONB objects", () => {
  const payload = { id: "yelp-business", price: "$$" };
  let received: unknown;
  const parameter = providerCacheJsonParameter({
    json(value) {
      received = value;
      return { value, type: 3802 };
    },
  }, payload);

  assert(received === payload);
  assert(parameter.value === payload);
  assertEquals(parameter.type, 3802);
});

Deno.test("canonical provider fingerprints ignore object key order", async () => {
  const left = await providerRequestFingerprint({
    ...request,
    parameters: { locale: "en_US", fields: ["hours", "phone"] },
  });
  const right = await providerRequestFingerprint({
    ...request,
    parameters: { fields: ["hours", "phone"], locale: "en_US" },
  });
  const changed = await providerRequestFingerprint({
    ...request,
    parameters: { fields: ["phone", "hours"], locale: "en_US" },
  });
  assertEquals(left, right);
  assert(left !== changed);
  assert(/^v1:[0-9a-f]{64}$/.test(left));
});

Deno.test("a valid cache hit avoids both a lease and an external call", async () => {
  const fetchedAt = new Date("2026-08-18T12:00:00.000Z");
  const store = new FakeStore({
    payload: { id: "yelp-business" },
    fetchedAt,
    expiresAt: serverCacheExpiresAt("yelp", fetchedAt),
  });
  let loads = 0;
  const result = await loadProviderPayload(
    store,
    yelpLink,
    request,
    () => {
      loads += 1;
      return Promise.resolve({ id: "unexpected" });
    },
    valid,
  );
  assertEquals(result.cacheStatus, "hit");
  assertEquals(loads, 0);
  assertEquals(store.claims, 0);
});

Deno.test("a cold Yelp request stores one validated response", async () => {
  const now = new Date("2026-08-18T12:00:00.000Z");
  const store = new FakeStore(null);
  const result = await loadProviderPayload(
    store,
    yelpLink,
    request,
    () => Promise.resolve({ id: "yelp-business", price: "$$" }),
    valid,
    { now: () => now },
  );
  assertEquals(result.cacheStatus, "miss");
  assertEquals(store.claims, 1);
  assertEquals(store.stores, 1);
  assertEquals(result.expiresAt?.toISOString(), "2026-08-19T10:00:00.000Z");
});

Deno.test("Foursquare bypasses every cache operation", async () => {
  const store = new FakeStore(null);
  const result = await loadProviderPayload(
    store,
    foursquareLink,
    { ...request, provider: "foursquare" },
    () => Promise.resolve({ fsq_place_id: "fsq-place" }),
    valid,
  );
  assertEquals(result.cacheStatus, "bypass");
  assertEquals(store.reads, 0);
  assertEquals(store.claims, 0);
  assertEquals(store.stores, 0);
});

Deno.test("a concurrent miss waits for the winner instead of calling twice", async () => {
  const fetchedAt = new Date("2026-08-18T12:00:00.000Z");
  const winner = {
    payload: { id: "yelp-business" },
    fetchedAt,
    expiresAt: serverCacheExpiresAt("yelp", fetchedAt),
  };
  const store = new FakeStore(null);
  store.claimLease = false;
  store.responseAfterRead = { count: 3, response: winner };
  let loads = 0;
  const result = await loadProviderPayload(
    store,
    yelpLink,
    request,
    () => {
      loads += 1;
      return Promise.resolve({ id: "unexpected" });
    },
    valid,
    { sleep: () => Promise.resolve() },
  );
  assertEquals(result.cacheStatus, "hit");
  assertEquals(loads, 0);
  assertEquals(store.claims, 1);
});

Deno.test("invalid cached identity is evicted and never returned", async () => {
  const fetchedAt = new Date("2026-08-18T12:00:00.000Z");
  const store = new FakeStore({
    payload: { id: "wrong-business" },
    fetchedAt,
    expiresAt: serverCacheExpiresAt("yelp", fetchedAt),
  });
  await assertRejects(
    () =>
      loadProviderPayload(
        store,
        yelpLink,
        request,
        () => Promise.resolve({ id: "unexpected" }),
        (payload) =>
          payload.id === yelpLink.providerPlaceId
            ? { ok: true }
            : { ok: false, reason: "identity_mismatch" },
      ),
    ProviderPayloadRejectedError,
  );
  assertEquals(store.evictions, 1);
});

Deno.test("oversized responses are abandoned before storage", async () => {
  const store = new FakeStore(null);
  await assertRejects(
    () =>
      loadProviderPayload(
        store,
        yelpLink,
        request,
        () => Promise.resolve({ value: "x".repeat(300 * 1_024) }),
        valid,
      ),
    ProviderPayloadTooLargeError,
  );
  assertEquals(store.stores, 0);
  assertEquals(store.abandons, 1);
});

class FakeStore implements ProviderCacheStore {
  reads = 0;
  claims = 0;
  stores = 0;
  abandons = 0;
  evictions = 0;
  claimLease = true;
  responseAfterRead: {
    count: number;
    response: CachedProviderResponse;
  } | null = null;

  constructor(private response: CachedProviderResponse | null) {}

  read(): Promise<CachedProviderResponse | null> {
    this.reads += 1;
    if (this.responseAfterRead && this.reads >= this.responseAfterRead.count) {
      return Promise.resolve(this.responseAfterRead.response);
    }
    return Promise.resolve(this.response);
  }

  claim(): Promise<ProviderRefreshLease | null> {
    this.claims += 1;
    return Promise.resolve(
      this.claimLease
        ? {
          token: "00000000-0000-4000-8000-000000000002",
          expiresAt: new Date(Date.now() + 20_000),
        }
        : null,
    );
  }

  store(
    link: RuntimeProviderLink,
    _endpoint: string,
    _fingerprint: string,
    _lease: ProviderRefreshLease,
    payload: Record<string, unknown>,
    fetchedAt: Date,
  ): Promise<CachedProviderResponse | null> {
    this.stores += 1;
    this.response = {
      payload,
      fetchedAt,
      expiresAt: serverCacheExpiresAt(link.provider, fetchedAt),
    };
    return Promise.resolve(this.response);
  }

  abandon(): Promise<void> {
    this.abandons += 1;
    return Promise.resolve();
  }

  evict(): Promise<void> {
    this.evictions += 1;
    this.response = null;
    return Promise.resolve();
  }
}

function valid(): { ok: true } {
  return { ok: true };
}

function assert(value: unknown): asserts value {
  if (!value) throw new Error(`assertion failed: ${String(value)}`);
}

function assertEquals(actual: unknown, expected: unknown): void {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  if (actualJson !== expectedJson) {
    throw new Error(`expected ${expectedJson}, received ${actualJson}`);
  }
}

async function assertRejects(
  callback: () => Promise<unknown>,
  expectedError: new (...args: never[]) => Error,
): Promise<void> {
  try {
    await callback();
  } catch (error) {
    if (error instanceof expectedError) return;
    throw error;
  }
  throw new Error("expected promise to reject");
}
