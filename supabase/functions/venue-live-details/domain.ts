export const HARD_NEGATIVE_FLAGS = new Set([
  "closed",
  "delete",
  "doesnt_exist",
  "does_not_exist",
  "duplicate",
  "inappropriate",
  "privatevenue",
  "private_venue",
]);

const ALCOHOL_CATEGORY_TERMS = [
  "bar",
  "beer",
  "brewery",
  "brewpub",
  "cocktail",
  "distillery",
  "lounge",
  "nightclub",
  "pub",
  "speakeasy",
  "taproom",
  "tasting room",
  "vineyard",
  "wine",
  "winery",
];

const DAY_NAMES: Record<number, string> = {
  1: "Monday",
  2: "Tuesday",
  3: "Wednesday",
  4: "Thursday",
  5: "Friday",
  6: "Saturday",
  7: "Sunday",
};

export type ExpectedPlace = {
  fsqPlaceId: string;
  name: string;
  latitude: number;
  longitude: number;
};

export type LiveFieldRequest = {
  phone: boolean;
  website: boolean;
  hours: boolean;
  price: boolean;
  settings: boolean;
};

export type LiveDetails = {
  // This is licensed provider content for the current detail view. It must
  // never be copied into the durable establishment record.
  cover_image_url: string | null;
  phone_e164: string | null;
  website_url: string | null;
  hours: { weekday_text: string[] } | null;
  open_now: boolean | null;
  price_level: number | null;
  setting_slugs: string[];
};

export type PlaceValidation =
  | { ok: true; distanceMeters: number }
  | { ok: false; reason: string };

export function validateProviderPlace(
  raw: Record<string, unknown>,
  expected: ExpectedPlace,
): PlaceValidation {
  const returnedId = text(raw.fsq_place_id ?? raw.fsq_id);
  if (returnedId !== expected.fsqPlaceId) {
    return { ok: false, reason: "identity_mismatch" };
  }

  const veracity = finiteNumber(raw.veracity_rating);
  if (veracity === null || veracity < 4) {
    return { ok: false, reason: "low_veracity" };
  }
  if (
    raw.date_closed !== null && raw.date_closed !== undefined &&
    raw.date_closed !== ""
  ) {
    return { ok: false, reason: "closed" };
  }
  const flags = qualityFlags(raw.unresolved_flags);
  if (flags.some((flag) => HARD_NEGATIVE_FLAGS.has(flag))) {
    return { ok: false, reason: "provider_warning" };
  }

  const coordinates = providerCoordinates(raw);
  if (!coordinates) return { ok: false, reason: "missing_coordinates" };
  const distanceMeters = haversineMeters(
    expected.latitude,
    expected.longitude,
    coordinates.latitude,
    coordinates.longitude,
  );
  if (distanceMeters > 100) return { ok: false, reason: "location_mismatch" };

  const providerName = text(raw.name);
  if (!providerName || !namesAreCompatible(expected.name, providerName)) {
    return { ok: false, reason: "name_mismatch" };
  }
  if (!hasSupportedCategory(raw.categories)) {
    return { ok: false, reason: "type_mismatch" };
  }

  return { ok: true, distanceMeters };
}

export function projectLiveDetails(
  raw: Record<string, unknown>,
  requested: LiveFieldRequest,
): LiveDetails {
  const hours = requested.hours ? normalizedHours(raw.hours) : null;
  const price = requested.price ? finiteNumber(raw.price) : null;
  return {
    // Foursquare's no-store detail path does not request or display imagery.
    cover_image_url: null,
    phone_e164: requested.phone ? e164(raw.tel) : null,
    website_url: requested.website ? safeHttpUrl(raw.website) : null,
    hours,
    open_now: requested.hours ? openNow(raw.hours) : null,
    price_level:
      price !== null && Number.isInteger(price) && price >= 1 && price <= 4
        ? price
        : null,
    setting_slugs: requested.settings ? objectiveSettings(raw.attributes) : [],
  };
}

export function hasLiveDetails(details: LiveDetails): boolean {
  return details.cover_image_url !== null ||
    details.phone_e164 !== null ||
    details.website_url !== null ||
    details.hours !== null ||
    details.price_level !== null ||
    details.setting_slugs.length > 0;
}

export function attributionUrl(
  raw: Record<string, unknown>,
  fsqPlaceId: string,
): string {
  const supplied = safeHttpUrl(raw.link);
  return supplied ??
    `https://foursquare.com/v/${encodeURIComponent(fsqPlaceId)}`;
}

export function providerFields(requested: LiveFieldRequest): string[] {
  const fields = new Set([
    "fsq_place_id",
    "name",
    "latitude",
    "longitude",
    "categories",
    "date_closed",
    "unresolved_flags",
    "veracity_rating",
    "link",
  ]);
  if (requested.phone) fields.add("tel");
  if (requested.website) fields.add("website");
  if (requested.hours) fields.add("hours");
  if (requested.price) fields.add("price");
  if (requested.settings) fields.add("attributes");
  return [...fields];
}

function providerCoordinates(
  raw: Record<string, unknown>,
): { latitude: number; longitude: number } | null {
  let latitude = finiteNumber(raw.latitude);
  let longitude = finiteNumber(raw.longitude);
  const geocodes = object(raw.geocodes);
  const main = object(geocodes?.main);
  latitude ??= finiteNumber(main?.latitude);
  longitude ??= finiteNumber(main?.longitude);
  if (latitude === null || longitude === null) return null;
  if (Math.abs(latitude) > 90 || Math.abs(longitude) > 180) return null;
  return { latitude, longitude };
}

export function namesAreCompatible(left: string, right: string): boolean {
  const leftTokens = nameTokens(left);
  const rightTokens = nameTokens(right);
  if (leftTokens.length === 0 || rightTokens.length === 0) return false;
  const leftJoined = leftTokens.join(" ");
  const rightJoined = rightTokens.join(" ");
  if (
    leftJoined === rightJoined || leftJoined.includes(rightJoined) ||
    rightJoined.includes(leftJoined)
  ) {
    return true;
  }
  const rightSet = new Set(rightTokens);
  const overlap = leftTokens.filter((token) => rightSet.has(token)).length;
  return overlap / Math.min(leftTokens.length, rightTokens.length) >= 0.5;
}

function nameTokens(value: string): string[] {
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

function hasSupportedCategory(value: unknown): boolean {
  if (!Array.isArray(value) || value.length === 0) return false;
  const labels = value.map((item) => {
    const row = object(item);
    return text(row?.label ?? row?.name)?.toLowerCase() ?? "";
  });
  const primary = labels[0] ?? "";
  if (primary.includes("restaurant") && !primary.includes("brewpub")) {
    return false;
  }
  return labels.some((label) =>
    ALCOHOL_CATEGORY_TERMS.some((term) => label.includes(term))
  );
}

function qualityFlags(value: unknown): string[] {
  const values: string[] = [];
  if (Array.isArray(value)) {
    for (const item of value) if (text(item)) values.push(text(item)!);
  } else {
    const row = object(value);
    if (row) {
      for (const [key, enabled] of Object.entries(row)) {
        if (enabled) values.push(key);
      }
    } else if (text(value)) {
      values.push(text(value)!);
    }
  }
  return values.map((flag) => {
    const token = flag.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(
      /^_+|_+$/g,
      "",
    );
    const compact = token.replace(/_/g, "");
    if (compact === "doesntexist") return "doesnt_exist";
    if (compact === "doesnotexist") return "does_not_exist";
    if (compact === "privatevenue") return "privatevenue";
    return token;
  });
}

function normalizedHours(value: unknown): { weekday_text: string[] } | null {
  const row = object(value);
  if (!row) return null;
  const regular = Array.isArray(row.regular) ? row.regular : [];
  const byDay = new Map<number, string[]>();
  for (const item of regular) {
    const period = object(item);
    const day = finiteNumber(period?.day);
    const open = formattedTime(period?.open);
    const close = formattedTime(period?.close);
    if (
      day === null || !Number.isInteger(day) || !DAY_NAMES[day] || !open ||
      !close
    ) continue;
    const values = byDay.get(day) ?? [];
    values.push(`${open}–${close}`);
    byDay.set(day, values);
  }
  const weekdayText = [...byDay.entries()]
    .sort(([left], [right]) => left - right)
    .map(([day, periods]) => `${DAY_NAMES[day]}: ${periods.join(", ")}`);
  if (weekdayText.length > 0) return { weekday_text: weekdayText };

  const display = text(row.display);
  return display ? { weekday_text: [`Hours: ${display}`] } : null;
}

export function formattedTime(value: unknown): string | null {
  const raw = text(value);
  if (!raw) return null;
  const match = raw.match(/^\+?(\d{2}):?(\d{2})$/);
  if (!match) return raw;
  const hour24 = Number(match[1]) % 24;
  const minute = Number(match[2]);
  if (minute > 59) return null;
  const suffix = hour24 >= 12 ? "PM" : "AM";
  const hour12 = hour24 % 12 || 12;
  return minute === 0
    ? `${hour12} ${suffix}`
    : `${hour12}:${String(minute).padStart(2, "0")} ${suffix}`;
}

function openNow(value: unknown): boolean | null {
  const row = object(value);
  return typeof row?.open_now === "boolean" ? row.open_now : null;
}

export function e164(value: unknown): string | null {
  const raw = text(value)?.replace(/[\s().-]/g, "") ?? "";
  return /^\+[1-9]\d{7,14}$/.test(raw) ? raw : null;
}

export function safeHttpUrl(value: unknown): string | null {
  const raw = text(value);
  if (!raw || raw.length > 2048) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:"
      ? url.toString()
      : null;
  } catch {
    return null;
  }
}

function objectiveSettings(value: unknown): string[] {
  const row = object(value);
  if (!row) return [];
  const outdoor = row.outdoor_seating ?? row.outdoorseating;
  return outdoor === true ? ["outdoor_patio"] : [];
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

export function haversineMeters(
  latitude1: number,
  longitude1: number,
  latitude2: number,
  longitude2: number,
): number {
  const radians = (degrees: number) => degrees * Math.PI / 180;
  const earthRadiusMeters = 6_371_000;
  const latitudeDelta = radians(latitude2 - latitude1);
  const longitudeDelta = radians(longitude2 - longitude1);
  const a = Math.sin(latitudeDelta / 2) ** 2 +
    Math.cos(radians(latitude1)) * Math.cos(radians(latitude2)) *
      Math.sin(longitudeDelta / 2) ** 2;
  return earthRadiusMeters * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}
