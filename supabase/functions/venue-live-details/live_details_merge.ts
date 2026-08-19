import {
  hasLiveDetails,
  type LiveDetails,
  type LiveFieldRequest,
} from "./domain.ts";

export type LiveDetailsProvider = "yelp" | "foursquare";

export type ProviderLiveDetails = {
  provider: LiveDetailsProvider;
  cacheStatus: string;
  fetchedAt: string;
  expiresAt: string | null;
  attribution: { name: string; url: string };
  details: LiveDetails;
};

type FieldSources = {
  phone_e164: LiveDetailsProvider | null;
  website_url: LiveDetailsProvider | null;
  hours: LiveDetailsProvider | null;
  open_now: LiveDetailsProvider | null;
  price_level: LiveDetailsProvider | null;
  setting_slugs: LiveDetailsProvider | null;
};

const emptyDetails = (): LiveDetails => ({
  phone_e164: null,
  website_url: null,
  hours: null,
  open_now: null,
  price_level: null,
  setting_slugs: [],
});

const emptySources = (): FieldSources => ({
  phone_e164: null,
  website_url: null,
  hours: null,
  open_now: null,
  price_level: null,
  setting_slugs: null,
});

export function missingLiveFields(
  requested: LiveFieldRequest,
  providerResults: ProviderLiveDetails[],
): LiveFieldRequest {
  const { details } = mergeProviderValues(providerResults);
  return {
    phone: requested.phone && details.phone_e164 === null,
    website: requested.website && details.website_url === null,
    hours: requested.hours && details.hours === null,
    price: requested.price && details.price_level === null,
    settings: requested.settings && details.setting_slugs.length === 0,
  };
}

export function hasRequestedLiveFields(requested: LiveFieldRequest): boolean {
  return Object.values(requested).some(Boolean);
}

export function liveDetailsResponse(
  providerResults: ProviderLiveDetails[],
): Record<string, unknown> | null {
  const validResults = providerResults.filter((result) =>
    hasLiveDetails(result.details)
  );
  const { details, sources } = mergeProviderValues(validResults);
  if (!hasLiveDetails(details)) return null;

  const usedProviders = new Set(
    Object.values(sources).filter(
      (provider): provider is LiveDetailsProvider => provider !== null,
    ),
  );
  const usedResults = validResults.filter((result) =>
    usedProviders.has(result.provider)
  );
  const single = usedResults.length === 1 ? usedResults[0] : null;

  return {
    available: true,
    provider: single?.provider ?? "multiple",
    cache_status: single?.cacheStatus ?? "mixed",
    fetched_at: latestFetchedAt(usedResults),
    // A mixed response includes an uncached Foursquare observation, so it must
    // never inherit Yelp's reusable expiry.
    expires_at: single?.expiresAt ?? null,
    attribution: single?.attribution ?? null,
    attributions: usedResults.map((result) => ({
      provider: result.provider,
      ...result.attribution,
    })),
    field_sources: sources,
    ...details,
  };
}

function mergeProviderValues(providerResults: ProviderLiveDetails[]): {
  details: LiveDetails;
  sources: FieldSources;
} {
  const details = emptyDetails();
  const sources = emptySources();

  // Caller order is the precedence order. Providers fill only unresolved
  // fields, preventing a lower-priority source from replacing a valid value.
  for (const result of providerResults) {
    const value = result.details;
    if (details.phone_e164 === null && value.phone_e164 !== null) {
      details.phone_e164 = value.phone_e164;
      sources.phone_e164 = result.provider;
    }
    if (details.website_url === null && value.website_url !== null) {
      details.website_url = value.website_url;
      sources.website_url = result.provider;
    }
    if (details.hours === null && value.hours !== null) {
      details.hours = value.hours;
      details.open_now = value.open_now;
      sources.hours = result.provider;
      if (value.open_now !== null) sources.open_now = result.provider;
    }
    if (details.price_level === null && value.price_level !== null) {
      details.price_level = value.price_level;
      sources.price_level = result.provider;
    }
    if (
      details.setting_slugs.length === 0 && value.setting_slugs.length > 0
    ) {
      details.setting_slugs = [...new Set(value.setting_slugs)];
      sources.setting_slugs = result.provider;
    }
  }
  return { details, sources };
}

function latestFetchedAt(results: ProviderLiveDetails[]): string | null {
  let latest: string | null = null;
  let latestTime = Number.NEGATIVE_INFINITY;
  for (const result of results) {
    const timestamp = Date.parse(result.fetchedAt);
    if (Number.isFinite(timestamp) && timestamp > latestTime) {
      latest = result.fetchedAt;
      latestTime = timestamp;
    }
  }
  return latest;
}
