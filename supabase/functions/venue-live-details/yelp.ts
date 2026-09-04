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
const YELP_ERROR_RESPONSE_LIMIT_BYTES = 16 * 1_024;
const YELP_TIMEOUT_MS = 3_500;
const YELP_ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const YELP_REQUEST_FIELDS = [
  "term",
  "name",
  "address1",
  "address2",
  "address3",
  "city",
  "state",
  "country",
  "postal_code",
  "latitude",
  "longitude",
  "phone",
  "yelp_business_id",
  "limit",
  "radius",
  "sort_by",
  "match_threshold",
  "locale",
  "device_platform",
] as const;

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
  phoneE164?: string | null;
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
  constructor(
    readonly code: YelpApiErrorCode,
    readonly providerCode: string | null = null,
  ) {
    super(`Yelp API error: ${code}`);
    this.name = "YelpApiError";
  }
}

export async function fetchYelpBusinessCandidates(
  apiKey: string,
  input: YelpMatchInput,
): Promise<Record<string, unknown>> {
  return await yelpJson(yelpBusinessSearchUrl(input), apiKey);
}

export function yelpBusinessSearchUrl(input: YelpMatchInput): URL {
  const name = input.name.trim();
  if (name.length < 1 || name.length > 300) {
    throw new YelpApiError("invalid_request", "invalid_search_input_name");
  }
  if (
    !Number.isFinite(input.latitude) || Math.abs(input.latitude) > 90 ||
    !Number.isFinite(input.longitude) || Math.abs(input.longitude) > 180
  ) {
    throw new YelpApiError(
      "invalid_request",
      "invalid_search_input_coordinates",
    );
  }

  const url = new URL(`${YELP_API_BASE}/businesses/search`);
  url.searchParams.set("term", name);
  url.searchParams.set("latitude", String(input.latitude));
  url.searchParams.set("longitude", String(input.longitude));
  // Search a wider retrieval window than Paloma's acceptance boundary so
  // provider ranking cannot turn small coordinate drift into a false negative.
  // Candidate acceptance remains fail-closed at 100 metres below.
  url.searchParams.set("radius", "500");
  url.searchParams.set("limit", "5");
  return url;
}

export async function fetchYelpBusinessDetails(
  apiKey: string,
  businessId: string,
): Promise<Record<string, unknown>> {
  return await yelpJson(yelpBusinessDetailsUrl(businessId), apiKey);
}

export function yelpBusinessDetailsUrl(businessId: string): URL {
  const url = new URL(
    `${YELP_API_BASE}/businesses/${encodeURIComponent(businessId)}`,
  );
  url.searchParams.set("locale", "en_US");
  return url;
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
  const exactPhone = Boolean(
    expected.phoneE164 && e164(raw.phone) === expected.phoneE164,
  );
  if (
    !providerName ||
    !strongYelpNameMatch(expected.name, providerName) &&
      !(exactPhone && namesAreCompatible(expected.name, providerName))
  ) {
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
    // Yelp's primary listing photo is display-only, subject to the provider
    // response policy and the same-screen Yelp credit returned below.
    cover_image_url: yelpCoverImageUrl(raw),
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

export function yelpCoverImageUrl(raw: Record<string, unknown>): string | null {
  const supplied = safeHttpUrl(raw.image_url);
  if (!supplied) return null;
  const url = new URL(supplied);
  const host = url.hostname.toLowerCase();
  // Yelp documents CDN-hosted Business Details image URLs. Refusing every
  // other host makes this a Yelp image field, rather than a generic remote URL
  // that could slip into a provider response and be displayed without review.
  if (host !== "yelpcdn.com" && !host.endsWith(".yelpcdn.com")) return null;
  // Yelp documents both HTTP and HTTPS samples. Never hand an HTTP image to
  // the iPhone, where App Transport Security would reject it anyway.
  url.protocol = "https:";
  return url.toString();
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
  if (responseError) {
    throw new YelpApiError(
      responseError,
      await yelpProviderErrorCodeFromResponse(response),
    );
  }

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

async function yelpProviderErrorCodeFromResponse(
  response: Response,
): Promise<string | null> {
  const contentLength = Number(response.headers.get("content-length") ?? 0);
  if (
    Number.isFinite(contentLength) &&
    contentLength > YELP_ERROR_RESPONSE_LIMIT_BYTES
  ) return null;
  try {
    const body = await response.text();
    if (
      new TextEncoder().encode(body).byteLength >
        YELP_ERROR_RESPONSE_LIMIT_BYTES
    ) return null;
    return yelpProviderErrorCode(JSON.parse(body));
  } catch {
    return null;
  }
}

export function yelpProviderErrorCode(value: unknown): string | null {
  const payload = object(value);
  const current = object(payload?.error);
  const legacy = Array.isArray(payload?.errors)
    ? object(payload.errors[0])
    : null;
  const rawCode = text(current?.code) ?? text(legacy?.error_code);
  const description = text(current?.description) ??
    text(legacy?.error_message);
  const normalized = rawCode?.toLowerCase().replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "") ?? "";
  if (!normalized) return null;

  const lowerDescription = description?.toLowerCase() ?? "";
  const field = YELP_REQUEST_FIELDS.find((candidate) =>
    new RegExp(`(^|[^a-z0-9_])${candidate}([^a-z0-9_]|$)`).test(
      lowerDescription,
    )
  );
  const candidate = `yelp_${normalized}${field ? `_${field}` : ""}`
    .slice(0, 64)
    .replace(/_+$/g, "");
  return YELP_ERROR_CODE_PATTERN.test(candidate) ? candidate : null;
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

function strongYelpNameMatch(left: string, right: string): boolean {
  const leftTokens = yelpNameTokens(left);
  const rightTokens = yelpNameTokens(right);
  if (leftTokens.length === 0 || rightTokens.length === 0) return false;
  const leftJoined = leftTokens.join(" ");
  const rightJoined = rightTokens.join(" ");
  if (
    leftJoined === rightJoined || leftJoined.includes(rightJoined) ||
    rightJoined.includes(leftJoined)
  ) return true;
  const rightSet = new Set(rightTokens);
  const overlap = leftTokens.filter((token) => rightSet.has(token)).length;
  return overlap / Math.min(leftTokens.length, rightTokens.length) >= 0.75;
}

function yelpNameTokens(value: string): string[] {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .split(/\s+/)
    .filter((token) => token && !["the", "and", "at", "sf"].includes(token));
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
