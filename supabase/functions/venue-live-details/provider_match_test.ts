import { providerMatchIdentityFingerprint } from "./provider_match.ts";

const identity = {
  name: "Revision Test Bar",
  address: "1 Market St",
  city: "San Francisco",
  region: "CA",
  postalCode: "94102",
  countryCode: "US",
  latitude: 37.777,
  longitude: -122.423,
  phoneE164: null,
};

Deno.test("matcher revisions invalidate stale provider-match cooldowns", async () => {
  const oldFingerprint = await providerMatchIdentityFingerprint(
    "yelp",
    identity,
    "api_business_match_strict_v1",
  );
  const currentFingerprint = await providerMatchIdentityFingerprint(
    "yelp",
    identity,
    "api_business_search_verified_v1",
  );

  assert(oldFingerprint !== currentFingerprint);
});

Deno.test("provider matcher revisions are bounded tokens", async () => {
  await assertRejects(() =>
    providerMatchIdentityFingerprint(
      "yelp",
      identity,
      "Business Search v1",
    )
  );
});

function assert(value: unknown): asserts value {
  if (!value) throw new Error(`assertion failed: ${String(value)}`);
}

async function assertRejects(callback: () => Promise<unknown>): Promise<void> {
  try {
    await callback();
  } catch (error) {
    if (error instanceof TypeError) return;
    throw error;
  }
  throw new Error("expected promise to reject");
}
