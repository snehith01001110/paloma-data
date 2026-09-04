import {
  projectYelpLiveDetails,
  selectYelpBusinessMatch,
  validateYelpPlace,
  yelpApiErrorCodeForStatus,
  yelpAttributionUrl,
  yelpBusinessDetailsUrl,
  yelpBusinessSearchUrl,
  yelpCoverImageUrl,
  yelpProviderErrorCode,
} from "./yelp.ts";

const expected = {
  yelpBusinessId: "night-owl-yelp-id",
  name: "The Night Owl",
  latitude: 37.79,
  longitude: -122.39,
};

const business = {
  id: "night-owl-yelp-id",
  name: "Night Owl",
  is_closed: false,
  url: "https://www.yelp.com/biz/night-owl-san-francisco",
  coordinates: { latitude: 37.7901, longitude: -122.3901 },
  categories: [{ alias: "cocktailbars", title: "Cocktail Bars" }],
  phone: "+14155551212",
  image_url: "https://s3-media1.fl.yelpcdn.com/bphoto/night-owl/o.jpg",
  price: "$$$",
  hours: [{
    hours_type: "REGULAR",
    is_open_now: true,
    open: [
      { day: 4, start: "1600", end: "0200", is_overnight: true },
      { day: 5, start: "1600", end: "0200", is_overnight: true },
    ],
  }],
};

const matchInput = {
  name: "Dogpatch Saloon",
  address: "2496 3rd St",
  city: "San Francisco",
  region: "ca",
  postalCode: "94107",
  countryCode: "us",
  latitude: 37.757963,
  longitude: -122.388534,
  phoneE164: null,
};

Deno.test("validates and projects Yelp detail fields without relabeling its URL", () => {
  assert(validateYelpPlace(business, expected).ok);
  assertEquals(
    yelpAttributionUrl(business),
    "https://www.yelp.com/biz/night-owl-san-francisco",
  );
  assertEquals(
    projectYelpLiveDetails(business, {
      phone: true,
      website: true,
      hours: true,
      price: true,
      settings: true,
    }),
    {
      cover_image_url:
        "https://s3-media1.fl.yelpcdn.com/bphoto/night-owl/o.jpg",
      phone_e164: "+14155551212",
      website_url: null,
      hours: {
        weekday_text: ["Friday: 4 PM–2 AM", "Saturday: 4 PM–2 AM"],
      },
      open_now: true,
      price_level: 3,
      setting_slugs: [],
    },
  );
});

Deno.test("fails closed on Yelp identity, closure, location, name, type, and attribution", () => {
  assertEquals(
    validateYelpPlace({ ...business, id: "other" }, expected),
    { ok: false, reason: "identity_mismatch" },
  );
  assertEquals(
    validateYelpPlace({ ...business, is_closed: true }, expected),
    { ok: false, reason: "closed" },
  );
  assertEquals(
    validateYelpPlace({
      ...business,
      coordinates: { latitude: 37.8, longitude: -122.39 },
    }, expected),
    { ok: false, reason: "location_mismatch" },
  );
  assertEquals(
    validateYelpPlace({ ...business, name: "Different Place" }, expected),
    { ok: false, reason: "name_mismatch" },
  );
  assertEquals(
    validateYelpPlace({
      ...business,
      categories: [{ alias: "thai", title: "Thai Restaurants" }],
    }, expected),
    { ok: false, reason: "type_mismatch" },
  );
  assertEquals(
    validateYelpPlace(
      { ...business, url: "https://example.com/place" },
      expected,
    ),
    { ok: false, reason: "missing_attribution" },
  );
});

Deno.test("candidate selection rejects provider ambiguity", () => {
  const matchExpected = {
    name: expected.name,
    latitude: expected.latitude,
    longitude: expected.longitude,
  };
  const single = selectYelpBusinessMatch(
    { businesses: [business] },
    matchExpected,
  );
  assert(single.ok);
  if (single.ok) assertEquals(single.confidence, 0.99);

  assertEquals(
    selectYelpBusinessMatch(
      { businesses: [business, { ...business, id: "second-id" }] },
      matchExpected,
    ),
    { ok: false, reason: "ambiguous" },
  );
  assertEquals(
    selectYelpBusinessMatch({ businesses: [] }, matchExpected),
    { ok: false, reason: "not_found" },
  );
});

Deno.test("drops malformed Yelp optional fields", () => {
  assertEquals(
    projectYelpLiveDetails({
      phone: "415-555-1212",
      price: "$$$$$",
      hours: [{
        hours_type: "REGULAR",
        open: [{ day: 9, start: "2500", end: "0200" }],
      }],
    }, {
      phone: true,
      website: true,
      hours: true,
      price: true,
      settings: true,
    }),
    {
      cover_image_url: null,
      phone_e164: null,
      website_url: null,
      hours: null,
      open_now: null,
      price_level: null,
      setting_slugs: [],
    },
  );
});

Deno.test("accepts only Yelp CDN cover-image URLs", () => {
  assertEquals(
    yelpCoverImageUrl({
      image_url: "http://s3-media2.fl.yelpcdn.com/bphoto/night-owl/o.jpg",
    }),
    "https://s3-media2.fl.yelpcdn.com/bphoto/night-owl/o.jpg",
  );
  assertEquals(
    yelpCoverImageUrl({
      image_url: "https://images.example.com/night-owl.jpg",
    }),
    null,
  );
});

Deno.test("classifies Yelp HTTP failures without retaining response bodies", () => {
  assertEquals(yelpApiErrorCodeForStatus(200), null);
  assertEquals(yelpApiErrorCodeForStatus(400), "invalid_request");
  assertEquals(yelpApiErrorCodeForStatus(401), "unauthorized");
  assertEquals(yelpApiErrorCodeForStatus(403), "forbidden");
  assertEquals(yelpApiErrorCodeForStatus(404), "not_found");
  assertEquals(yelpApiErrorCodeForStatus(429), "rate_limited");
  assertEquals(yelpApiErrorCodeForStatus(503), "unavailable");
  assertEquals(
    yelpProviderErrorCode({
      error: {
        code: "VALIDATION_ERROR",
        description: "match_threshold is invalid",
      },
    }),
    "yelp_validation_error_match_threshold",
  );
  assertEquals(
    yelpProviderErrorCode({
      errors: [{
        error_code: "FIELD_REQUIRED",
        error_message: "state is required",
      }],
    }),
    "yelp_field_required_state",
  );
  assertEquals(
    yelpProviderErrorCode({ error: { description: "secret" } }),
    null,
  );
});

Deno.test("searches by exact consumer name near the verified coordinates", () => {
  const url = yelpBusinessSearchUrl(matchInput);
  assertEquals(url.pathname, "/v3/businesses/search");
  assertEquals(Object.fromEntries(url.searchParams), {
    term: "Dogpatch Saloon",
    latitude: "37.757963",
    longitude: "-122.388534",
    radius: "500",
    limit: "5",
  });
});

Deno.test("uses only supported Business Details query parameters", () => {
  const url = yelpBusinessDetailsUrl("dogpatch-saloon-san-francisco");
  assertEquals(
    url.pathname,
    "/v3/businesses/dogpatch-saloon-san-francisco",
  );
  assertEquals(Object.fromEntries(url.searchParams), { locale: "en_US" });
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
