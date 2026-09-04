import {
  attributionUrl,
  hasLiveDetails,
  projectLiveDetails,
  providerFields,
  validateProviderPlace,
} from "./domain.ts";

const expected = {
  fsqPlaceId: "4abc123",
  name: "The Night Owl",
  latitude: 37.79,
  longitude: -122.39,
};

Deno.test("validates a high-veracity matching place and projects transient fields", () => {
  const raw = {
    fsq_place_id: "4abc123",
    name: "Night Owl",
    latitude: 37.7901,
    longitude: -122.3901,
    categories: [{ name: "Cocktail Bar" }],
    veracity_rating: 5,
    tel: "+14155551212",
    website: "https://nightowl.example",
    price: 3,
    attributes: { outdoor_seating: true },
    hours: {
      open_now: true,
      regular: [
        { day: 5, open: "1600", close: "+0200" },
        { day: 6, open: "1600", close: "0200" },
      ],
    },
    link: "https://foursquare.com/v/night-owl/4abc123",
  };

  assert(validateProviderPlace(raw, expected).ok);
  const details = projectLiveDetails(raw, {
    phone: true,
    website: true,
    hours: true,
    price: true,
    settings: true,
  });
  assertEquals(details, {
    cover_image_url: null,
    phone_e164: "+14155551212",
    website_url: "https://nightowl.example/",
    hours: { weekday_text: ["Friday: 4 PM–2 AM", "Saturday: 4 PM–2 AM"] },
    open_now: true,
    price_level: 3,
    setting_slugs: ["outdoor_patio"],
  });
  assert(hasLiveDetails(details));
  assertEquals(attributionUrl(raw, expected.fsqPlaceId), raw.link);
});

Deno.test("fails closed on identity, veracity, closure, distance, name, and type conflicts", () => {
  const base = {
    fsq_place_id: expected.fsqPlaceId,
    name: expected.name,
    latitude: expected.latitude,
    longitude: expected.longitude,
    categories: [{ name: "Wine Bar" }],
    veracity_rating: 5,
  };
  assertEquals(
    validateProviderPlace({ ...base, fsq_place_id: "other" }, expected),
    {
      ok: false,
      reason: "identity_mismatch",
    },
  );
  assertEquals(
    validateProviderPlace({ ...base, veracity_rating: 3 }, expected),
    {
      ok: false,
      reason: "low_veracity",
    },
  );
  assertEquals(
    validateProviderPlace({ ...base, date_closed: "2025-01-01" }, expected),
    {
      ok: false,
      reason: "closed",
    },
  );
  assertEquals(
    validateProviderPlace(
      { ...base, unresolved_flags: { duplicate: true } },
      expected,
    ),
    {
      ok: false,
      reason: "provider_warning",
    },
  );
  assertEquals(validateProviderPlace({ ...base, latitude: 37.8 }, expected), {
    ok: false,
    reason: "location_mismatch",
  });
  assertEquals(
    validateProviderPlace({ ...base, name: "Completely Different" }, expected),
    {
      ok: false,
      reason: "name_mismatch",
    },
  );
  assertEquals(
    validateProviderPlace({
      ...base,
      categories: [{ name: "Thai Restaurant" }, { name: "Cocktail Bar" }],
    }, expected),
    { ok: false, reason: "type_mismatch" },
  );
});

Deno.test("drops malformed optional fields rather than returning questionable values", () => {
  const details = projectLiveDetails({
    tel: "555-1212",
    website: "javascript:alert(1)",
    price: 9,
    hours: { regular: [{ day: 9, open: "2500", close: "0200" }] },
    attributes: { outdoor_seating: "yes" },
  }, {
    phone: true,
    website: true,
    hours: true,
    price: true,
    settings: true,
  });
  assertEquals(details, {
    cover_image_url: null,
    phone_e164: null,
    website_url: null,
    hours: null,
    open_now: null,
    price_level: null,
    setting_slugs: [],
  });
  assert(!hasLiveDetails(details));
});

Deno.test("requests only missing rich fields plus the verification envelope", () => {
  const fields = providerFields({
    phone: false,
    website: true,
    hours: false,
    price: true,
    settings: false,
  });
  assert(fields.includes("fsq_place_id"));
  assert(fields.includes("veracity_rating"));
  assert(fields.includes("website"));
  assert(fields.includes("price"));
  assert(!fields.includes("tel"));
  assert(!fields.includes("hours"));
  assert(!fields.includes("attributes"));
});

Deno.test("uses a safe Foursquare venue URL when the response omits link", () => {
  assertEquals(
    attributionUrl({}, "id with spaces"),
    "https://foursquare.com/v/id%20with%20spaces",
  );
});

function assert(value: unknown): asserts value {
  if (!value) throw new Error(`assertion failed: ${String(value)}`);
}

function assertEquals(actual: unknown, expectedValue: unknown): void {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expectedValue);
  if (actualJson !== expectedJson) {
    throw new Error(`expected ${expectedJson}, received ${actualJson}`);
  }
}
