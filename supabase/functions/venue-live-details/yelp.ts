import {
  e164,
  formattedTime,
  haversineMeters,
  type LiveDetails,
  type LiveFieldRequest,
  namesAreCompatible,
  type PlaceValidation,
  safeHttpUrl,
} from "./domain.ts";

const YELP_API_BASE = "https://api.yelp.com/v3";
const YELP_RESPONSE_LIMIT_BYTES = 512 * 1_024;
const YELP_TIMEOUT_MS = 3_500;

const YELP_ALCOHOL_CATEGORY_ALIASES = new Set([
  "bars",
  "beerbar",
  "beergardens",
  "breweries",
  "brewpubs",
  "cocktailbars",
  "distilleries",
  "divebars",
  "gaybars",
  "hookah_bars",
  "lounges",
  "pubs",
  "speakeasies",
  "sportsbars",
  "tastingrooms",
  "whiskeybars",
  "wine_bars",
  "wineries",
]);

const YELP_ALCOHOL_CATEGORY_TERMS = [
  "bar",
  "beer",
  "brewery",
  "brewpub",
  "cocktail",
  "distillery",
  "lounge",
  "pub",
  "speakeasy",
  "tasting room",
  "whiskey",
  "wine",
  "winery",
];

const YELP_DAY_NAMES: Record<number, string> = {
  0: "Monday",
  1: "Tuesday",
  2: "Wednesday",
  3: "Thursday",
  4: "Friday",
  5: "Saturday",
  6: "Sunday",
};

export type YelpMatchInput = Readonly<{
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

export type ExpectedYelpPlace = Readonly<{
  yelpBusinessId?: string;
  name: string;
  latitude: number;
  longitude: number;
}>;

export type YelpMatchSelection =
  | {
    ok: true;
    business: Record<string, unknown>;
    confidence: number;
  }
  | { ok: false; reason: "not_found" | "ambiguous" | "rejected" };

export type YelpApiErrorCode =
  | "invalid_request"
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "rate_limited"
  | "timeout"
  | "unavailable"
  | "invalid_payload";

export class YelpApiError extends Error {
  constructor(readonly code: YelpApiErrorCode) {
    super(`Yelp API error: ${code}`);
    this.name = "YelpApiError";
  }
}

export async function fetchYelpBusinessMatch(
  apiKey: string,
  input: YelpMatchInput,
): Promise<Record<string, unknown>> {
  const url = new URL(`${YELP_API_BASE}/businesses/matches`);
  url.searchParams.set("name", input.name);
  url.searchParams.set("address1", input.address);
  url.searchParams.set("city", input.city);
  url.searchParams.set("country", input.countryCode.toUpperCase());
  url.searchParams.set("match_threshold", "strict");
  // More than one result lets Paloma reject ambiguity instead of accepting the
  // first candidate merely because the provider ranked it first.
  url.searchParams.set("limit", "3");
  if (input.region) url.searchParams.set("state", input.region);
  if (input.postalCode) url.searchParams.set("postal_code", input.postalCode);
  if (Number.isFinite(input.latitude) && Number.isFinite(input.longitude)) {
    url.searchParams.set("latitude", String(input.latitude));
    url.searchParams.set("longitude", String(input.longitude));
  }
  if (input.phoneE164) url.searchParams.set("phone", input.phoneE164);
  return await yelpJson(url, apiKey);
}

export async function fetchYelpBusinessDetails(
  apiKey: string,
  businessId: string,
): Promise<Record<string, unknown>> {
  const url = new URL(
    `${YELP_API_BASE}/businesses/${encodeURIComponent(businessId)}`,
  );
  url.searchParams.set("locale", "en_US");
  url.searchParams.set("device_platform", "ios");
  return await yelpJson(url, apiKey);
}

export function selectYelpBusinessMatch(
  payload: Record<string, unknown>,
  expected: ExpectedYelpPlace,
): YelpMatchSelection {
  const businesses = Array.isArray(payload.businesses)
    ? payload.businesses
    : [];
  const valid: Array<{
    business: Record<string, unknown>;
    distanceMeters: number;
  }> = [];
  for (const value of businesses) {
    const business = object(value);
    if (!business) continue;
    const validation = validateYelpPlace(business, expected, false);
    if (validation.ok) {
      valid.push({
        business,
        distanceMeters: validation.distanceMeters,
      });
    }
  }
  if (valid.length === 0) {
    return {
      ok: false,
      reason: businesses.length === 0 ? "not_found" : "rejected",
    };
  }
  if (valid.length !== 1) return { ok: false, reason: "ambiguous" };
  return {
    ok: true,
    business: valid[0].business,
    confidence: valid[0].distanceMeters <= 50 ? 0.99 : 0.97,
  };
}

export function validateYelpPlace(
  raw: Record<string, unknown>,
  expected: ExpectedYelpPlace,
  requireAttribution = true,
): PlaceValidation {
  const returnedId = text(raw.id);
  if (!returnedId || returnedId.length > 255) {
    return { ok: false, reason: "missing_identity" };
  }
  if (expected.yelpBusinessId && returnedId !== expected.yelpBusinessId) {
    return { ok: false, reason: "identity_mismatch" };
  }
  if (raw.is_closed === true) return { ok: false, reason: "closed" };

  const coordinates = object(raw.coordinates);
  const latitude = finiteNumber(coordinates?.latitude);
  const longitude = finiteNumber(coordinates?.longitude);
  if (
    latitude === null || longitude === null || Math.abs(latitude) > 90 ||
    Math.abs(longitude) > 180
  ) {
    return { ok: false, reason: "missing_coordinates" };
  }
  const distanceMeters = haversineMeters(
    expected.latitude,
    expected.longitude,
    latitude,
    longitude,
  );
  if (distanceMeters > 100) {
    return { ok: false, reason: "location_mismatch" };
  }

  const providerName = text(raw.name);
  if (!providerName || !namesAreCompatible(expected.name, providerName)) {
    return { ok: false, reason: "name_mismatch" };
  }
  if (!hasSupportedYelpCategory(raw.categories)) {
    return { ok: false, reason: "type_mismatch" };
  }
  if (requireAttribution && !yelpAttributionUrl(raw)) {
    return { ok: false, reason: "missing_attribution" };
  }
  return { ok: true, distanceMeters };
}

export function projectYelpLiveDetails(
  raw: Record<string, unknown>,
  requested: LiveFieldRequest,
): LiveDetails {
  const price = requested.price ? text(raw.price) : null;
  return {
    phone_e164: requested.phone ? e164(raw.phone) : null,
    // Yelp's `url` is the required Yelp attribution link, not the venue's own
    // website. Never relabel it as consumer contact data.
    website_url: null,
    hours: requested.hours ? normalizedYelpHours(raw.hours) : null,
    open_now: requested.hours ? yelpOpenNow(raw.hours) : null,
    price_level: price && /^\${1,4}$/.test(price) ? price.length : null,
    // Base Yelp details do not provide an objective setting taxonomy that maps
    // safely to Paloma's durable setting slugs.
    setting_slugs: [],
  };
}

export function yelpAttributionUrl(
  raw: Record<string, unknown>,
): string | null {
  const supplied = safeHttpUrl(raw.url);
  if (!supplied) return null;
  const host = new URL(supplied).hostname.toLowerCase();
  return host === "yelp.com" || host.endsWith(".yelp.com") ? supplied : null;
}

export function yelpBusinessId(
  raw: Record<string, unknown>,
): string | null {
  const value = text(raw.id);
  return value && value.length <= 255 ? value : null;
}

async function yelpJson(
  url: URL,
  apiKey: string,
): Promise<Record<string, unknown>> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), YELP_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: {
        "Authorization": `Bearer ${apiKey}`,
        "Accept": "application/json",
      },
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (error) {
    throw new YelpApiError(
      error instanceof Error && error.name === "AbortError"
        ? "timeout"
        : "unavailable",
    );
  } finally {
    clearTimeout(timeout);
  }

  const responseError = yelpApiErrorCodeForStatus(response.status);
  if (responseError) throw new YelpApiError(responseError);

  const contentLength = Number(response.headers.get("content-length") ?? 0);
  if (
    Number.isFinite(contentLength) && contentLength > YELP_RESPONSE_LIMIT_BYTES
  ) {
    throw new YelpApiError("invalid_payload");
  }
  let body: string;
  try {
    body = await response.text();
  } catch {
    throw new YelpApiError("invalid_payload");
  }
  if (new TextEncoder().encode(body).byteLength > YELP_RESPONSE_LIMIT_BYTES) {
    throw new YelpApiError("invalid_payload");
  }
  try {
    const payload = object(JSON.parse(body));
    if (!payload) throw new YelpApiError("invalid_payload");
    return payload;
  } catch (error) {
    if (error instanceof YelpApiError) throw error;
    throw new YelpApiError("invalid_payload");
  }
}

export function yelpApiErrorCodeForStatus(
  status: number,
): YelpApiErrorCode | null {
  if (status >= 200 && status < 300) return null;
  if (status === 400) return "invalid_request";
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 429) return "rate_limited";
  return "unavailable";
}

function normalizedYelpHours(
  value: unknown,
): { weekday_text: string[] } | null {
  if (!Array.isArray(value)) return null;
  const group =
    value.map(object).find((row) => row?.hours_type === "REGULAR") ??
      value.map(object).find(Boolean);
  const periods = Array.isArray(group?.open) ? group.open : [];
  const byDay = new Map<number, string[]>();
  for (const value of periods) {
    const period = object(value);
    const day = finiteNumber(period?.day);
    const open = formattedTime(period?.start);
    const close = formattedTime(period?.end);
    if (
      day === null || !Number.isInteger(day) || !YELP_DAY_NAMES[day] ||
      !open || !close
    ) {
      continue;
    }
    const values = byDay.get(day) ?? [];
    values.push(`${open}–${close}`);
    byDay.set(day, values);
  }
  const weekdayText = [...byDay.entries()]
    .sort(([left], [right]) => left - right)
    .map(([day, periods]) => `${YELP_DAY_NAMES[day]}: ${periods.join(", ")}`);
  return weekdayText.length > 0 ? { weekday_text: weekdayText } : null;
}

function yelpOpenNow(value: unknown): boolean | null {
  if (!Array.isArray(value)) return null;
  const group =
    value.map(object).find((row) => row?.hours_type === "REGULAR") ??
      value.map(object).find(Boolean);
  return typeof group?.is_open_now === "boolean" ? group.is_open_now : null;
}

function hasSupportedYelpCategory(value: unknown): boolean {
  if (!Array.isArray(value) || value.length === 0) return false;
  return value.some((item) => {
    const category = object(item);
    const alias = text(category?.alias)?.toLowerCase() ?? "";
    const title = text(category?.title)?.toLowerCase() ?? "";
    return YELP_ALCOHOL_CATEGORY_ALIASES.has(alias) ||
      YELP_ALCOHOL_CATEGORY_TERMS.some((term) => title.includes(term));
  });
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed || null;
}

function finiteNumber(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
