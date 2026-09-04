import { assertEquals } from "jsr:@std/assert@1.0.14";
import type { LiveDetails } from "./domain.ts";
import {
  liveDetailsResponse,
  missingLiveFields,
  type ProviderLiveDetails,
} from "./live_details_merge.ts";

const details = (overrides: Partial<LiveDetails>): LiveDetails => ({
  cover_image_url: null,
  phone_e164: null,
  website_url: null,
  hours: null,
  open_now: null,
  price_level: null,
  setting_slugs: [],
  ...overrides,
});

const result = (
  provider: "yelp" | "foursquare",
  value: LiveDetails,
): ProviderLiveDetails => ({
  provider,
  cacheStatus: provider === "yelp" ? "hit" : "bypass",
  fetchedAt: provider === "yelp"
    ? "2026-08-19T10:00:00.000Z"
    : "2026-08-19T10:01:00.000Z",
  expiresAt: provider === "yelp" ? "2026-08-20T08:00:00.000Z" : null,
  attribution: {
    name: provider === "yelp" ? "Yelp" : "Foursquare",
    url: `https://${provider}.example/place`,
  },
  details: value,
});

Deno.test("requests only fields Yelp did not supply", () => {
  const yelp = result(
    "yelp",
    details({
      phone_e164: "+14155550100",
      hours: { weekday_text: ["Monday: 4:00 PM–12:00 AM"] },
      open_now: true,
      price_level: 2,
    }),
  );

  assertEquals(
    missingLiveFields(
      { phone: true, website: true, hours: true, price: true, settings: true },
      [yelp],
    ),
    { phone: false, website: true, hours: false, price: false, settings: true },
  );
});

Deno.test("merges complementary providers with field-level provenance", () => {
  const yelp = result(
    "yelp",
    details({
      phone_e164: "+14155550100",
      hours: { weekday_text: ["Monday: 4:00 PM–12:00 AM"] },
      open_now: false,
      price_level: 2,
    }),
  );
  const foursquare = result(
    "foursquare",
    details({
      website_url: "https://nightowl.example/",
      setting_slugs: ["outdoor_patio"],
    }),
  );

  assertEquals(liveDetailsResponse([yelp, foursquare]), {
    available: true,
    provider: "multiple",
    cache_status: "mixed",
    fetched_at: "2026-08-19T10:01:00.000Z",
    expires_at: null,
    attribution: null,
    attributions: [
      { provider: "yelp", name: "Yelp", url: "https://yelp.example/place" },
      {
        provider: "foursquare",
        name: "Foursquare",
        url: "https://foursquare.example/place",
      },
    ],
    field_sources: {
      phone_e164: "yelp",
      website_url: "foursquare",
      hours: "yelp",
      open_now: "yelp",
      price_level: "yelp",
      setting_slugs: "foursquare",
    },
    cover_image_url: null,
    phone_e164: "+14155550100",
    website_url: "https://nightowl.example/",
    hours: { weekday_text: ["Monday: 4:00 PM–12:00 AM"] },
    open_now: false,
    price_level: 2,
    setting_slugs: ["outdoor_patio"],
  });
});

Deno.test("keeps a Yelp cover image paired with its Yelp attribution", () => {
  const yelp = result(
    "yelp",
    details({
      cover_image_url:
        "https://s3-media1.fl.yelpcdn.com/bphoto/night-owl/o.jpg",
    }),
  );
  const response = liveDetailsResponse([yelp]);

  assertEquals(response?.provider, "yelp");
  assertEquals(response?.cover_image_url, yelp.details.cover_image_url);
  assertEquals(response?.field_sources, { cover_image_url: "yelp" });
  assertEquals(response?.attributions, [
    { provider: "yelp", name: "Yelp", url: "https://yelp.example/place" },
  ]);
});

Deno.test("single-provider responses retain their reusable cache metadata", () => {
  const yelp = result("yelp", details({ price_level: 3 }));
  const response = liveDetailsResponse([yelp]);

  assertEquals(response?.provider, "yelp");
  assertEquals(response?.expires_at, "2026-08-20T08:00:00.000Z");
  assertEquals(response?.attributions, [
    { provider: "yelp", name: "Yelp", url: "https://yelp.example/place" },
  ]);
  assertEquals(response?.field_sources, { price_level: "yelp" });
});
