import "jsr:@supabase/functions-js@2.5.0/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.7";

const PROJECT_REF = "lighcnfzajgvfbdoekzt";
const OWNER_USER_ID = "06e91911-fb0d-4ece-bba8-94665e7889f0";
const PAGES_ORIGIN = "https://snehith01001110.github.io";
const POOLER_HOST = "aws-0-us-west-2.pooler.supabase.com";
const REVIEW_VERSION = "manual-review-v1";
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const FIELD_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const REVIEWABLE_FIELDS = new Set([
  "phone_e164",
  "website_url",
  "neighborhood",
  "address",
  "latitude",
  "longitude",
  "operating_status",
  "hours",
  "price_level",
]);

type Sql = ReturnType<typeof postgres>;
type AuthenticatedUser = { id: string; email: string | null };

const corsHeaders = {
  "Access-Control-Allow-Headers":
    "authorization, apikey, content-type, x-client-info",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
  "Content-Type": "application/json; charset=utf-8",
  Expires: "0",
  Pragma: "no-cache",
  Vary: "Authorization, Origin",
};

Deno.serve(async (request: Request) => {
  const headers = requestHeaders(request);
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers });
  }
  if (request.method !== "GET" && request.method !== "POST") {
    return json({ error: "method_not_allowed" }, 405, headers);
  }

  const user = await authenticatedUser(request);
  if (!user) return json({ error: "unauthorized" }, 401, headers);
  if (user.id.toLowerCase() !== OWNER_USER_ID) {
    return json({ error: "forbidden" }, 403, headers);
  }

  const databaseUrl = await scopedDatabaseUrl();
  if (!databaseUrl) {
    return json({ error: "database_unavailable" }, 503, headers);
  }
  const sql = databaseClient(databaseUrl);

  try {
    if (request.method === "GET") {
      return json(await listConflicts(sql, request), 200, headers);
    }
    const body = await reviewBody(request);
    if (!body) return json({ error: "invalid_request" }, 400, headers);
    const result = await reviewConflict(sql, user, body);
    return json(result, 200, headers);
  } catch (error) {
    const mapped = mapError(error);
    if (mapped) return json(mapped.body, mapped.status, headers);
    console.error("paloma-conflict-review", errorCode(error));
    return json({ error: "temporarily_unavailable" }, 503, headers);
  } finally {
    await sql.end({ timeout: 1 });
  }
});

function requestHeaders(request: Request): Headers {
  const headers = new Headers(corsHeaders);
  const origin = request.headers.get("origin");
  if (origin === PAGES_ORIGIN || origin?.startsWith("http://localhost:")) {
    headers.set("Access-Control-Allow-Origin", origin);
  } else {
    headers.set("Access-Control-Allow-Origin", PAGES_ORIGIN);
  }
  return headers;
}

function json(
  body: Record<string, unknown>,
  status: number,
  headers: Headers,
): Response {
  return new Response(JSON.stringify(body), { status, headers });
}

function databaseClient(databaseUrl: string): Sql {
  return postgres(databaseUrl, {
    max: 1,
    prepare: false,
    idle_timeout: 5,
    connect_timeout: 5,
  });
}

async function authenticatedUser(
  request: Request,
): Promise<AuthenticatedUser | null> {
  const authorization = request.headers.get("authorization") ?? "";
  if (!authorization.startsWith("Bearer ")) return null;
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY") ??
    Deno.env.get("SUPABASE_PUBLISHABLE_KEY");
  if (!supabaseUrl || !anonKey) return null;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4_000);
  try {
    const response = await fetch(`${supabaseUrl}/auth/v1/user`, {
      method: "GET",
      headers: { authorization, apikey: anonKey },
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) return null;
    const payload = await response.json() as { id?: unknown; email?: unknown };
    if (typeof payload.id !== "string" || !UUID_PATTERN.test(payload.id)) {
      return null;
    }
    return {
      id: payload.id,
      email: typeof payload.email === "string" ? payload.email : null,
    };
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

async function scopedDatabaseUrl(): Promise<string | null> {
  const configured = Deno.env.get("SUPABASE_DB_URL");
  if (!configured) return null;

  let configuredUrl: URL;
  try {
    configuredUrl = new URL(configured);
  } catch {
    return null;
  }
  const configuredUser = decodeURIComponent(configuredUrl.username);
  if (
    configuredUser === "paloma_ingest" ||
    configuredUser.startsWith("paloma_ingest.")
  ) {
    return configured;
  }

  // Edge functions use the existing server-side connection only to retrieve the
  // vault-managed scoped worker password. The review queries themselves always
  // run through the least-privilege paloma_ingest pooler role.
  const direct = databaseClient(configured);
  try {
    const rows = await direct`
      select decrypted_secret
      from vault.decrypted_secrets
      where name = 'paloma_ingest_db_password'
      limit 1
    `;
    const password = rows[0]?.decrypted_secret;
    if (!password) return null;
    const pooler = new URL(
      `postgresql://placeholder@${POOLER_HOST}:5432/postgres`,
    );
    pooler.username = `paloma_ingest.${PROJECT_REF}`;
    pooler.password = String(password);
    pooler.searchParams.set("sslmode", "require");
    return pooler.toString();
  } finally {
    await direct.end({ timeout: 1 });
  }
}

async function listConflicts(
  sql: Sql,
  request: Request,
): Promise<Record<string, unknown>> {
  const url = new URL(request.url);
  const limit = boundedInt(url.searchParams.get("limit"), 25, 1, 50);
  const offset = boundedInt(url.searchParams.get("offset"), 0, 0, 10_000);
  const field = optionalText(url.searchParams.get("field"), 64);
  const city = optionalText(url.searchParams.get("city"), 100);
  const search = optionalText(url.searchParams.get("q"), 100);
  if (field && !FIELD_PATTERN.test(field)) {
    throw new RequestError("invalid_field", 400);
  }

  const [rows, countRows] = await Promise.all([
    sql`
      with page as (
        select
          conflict.id::text as conflict_id,
          conflict.establishment_id::text as establishment_id,
          conflict.field_name,
          conflict.reason,
          conflict.evidence_ids,
          conflict.priority,
          conflict.created_at,
          establishment.name,
          establishment.address,
          establishment.city,
          establishment.region,
          establishment.phone_e164,
          establishment.website_url,
          establishment.neighborhood,
          establishment.hours,
          establishment.price_level,
          establishment.status,
          extensions.st_y(establishment.location::geometry)::text as latitude,
          extensions.st_x(establishment.location::geometry)::text as longitude
        from review.field_conflicts conflict
        join public.establishments establishment
          on establishment.id = conflict.establishment_id
        where conflict.state = 'pending'
          and (${field}::text is null or conflict.field_name = ${field})
          and (${city}::text is null or lower(establishment.city) = lower(${city}))
          and (
            ${search}::text is null
            or establishment.name ilike '%' || ${search} || '%'
            or establishment.address ilike '%' || ${search} || '%'
            or establishment.city ilike '%' || ${search} || '%'
            or conflict.id::text = ${search}
          )
        order by conflict.priority desc, conflict.created_at, conflict.id
        limit ${limit} offset ${offset}
      )
      select
        page.conflict_id,
        page.establishment_id,
        page.field_name,
        page.reason,
        page.priority,
        page.created_at,
        page.name,
        page.address,
        page.city,
        page.region,
        case page.field_name
          when 'phone_e164' then page.phone_e164
          when 'website_url' then page.website_url
          when 'neighborhood' then page.neighborhood
          when 'operating_status' then page.status
          when 'price_level' then page.price_level::text
          when 'latitude' then page.latitude
          when 'longitude' then page.longitude
          else null
        end as current_value_text,
        case when page.field_name = 'hours' then page.hours else null::jsonb end
          as current_value_json,
        evidence.id::text as evidence_id,
        evidence.value_text as evidence_value_text,
        evidence.value_json as evidence_value_json,
        evidence.source as evidence_source,
        evidence.claim_kind as evidence_claim_kind,
        evidence.evidence_confidence::float8 as evidence_confidence,
        evidence.identity_confidence::float8 as identity_confidence,
        evidence.authority::float8 as authority,
        evidence.upstream_origin_keys,
        evidence.source_items,
        evidence.source_updated_at,
        evidence.expires_at
      from page
      left join catalog.field_observations evidence
        on evidence.id = any(page.evidence_ids)
       and (
         evidence.establishment_id = page.establishment_id::uuid
         or evidence.candidate_id = page.establishment_id::uuid
       )
       and evidence.field_name = page.field_name
       and evidence.observation_status = 'asserted'
       and (evidence.expires_at is null or evidence.expires_at > now())
      order by page.priority desc, page.created_at, page.conflict_id,
               evidence.source, evidence.id
    `,
    sql`
      select count(*)::int as total_count
      from review.field_conflicts conflict
      join public.establishments establishment
        on establishment.id = conflict.establishment_id
      where conflict.state = 'pending'
        and (${field}::text is null or conflict.field_name = ${field})
        and (${city}::text is null or lower(establishment.city) = lower(${city}))
        and (
          ${search}::text is null
          or establishment.name ilike '%' || ${search} || '%'
          or establishment.address ilike '%' || ${search} || '%'
          or establishment.city ilike '%' || ${search} || '%'
          or conflict.id::text = ${search}
        )
    `,
  ]);

  const items = new Map<string, Record<string, unknown>>();
  for (const row of rows) {
    const conflictId = String(row.conflict_id);
    let item = items.get(conflictId);
    if (!item) {
      item = {
        conflict_id: conflictId,
        field_name: row.field_name,
        reason: row.reason,
        priority: Number(row.priority),
        created_at: row.created_at,
        establishment: {
          id: row.establishment_id,
          name: row.name,
          address: row.address,
          city: row.city,
          region: row.region,
        },
        current: {
          value_text: row.current_value_text ?? null,
          value_json: row.current_value_json ?? null,
        },
        evidence: [],
      };
      items.set(conflictId, item);
    }
    if (typeof row.evidence_id === "string") {
      const evidence = item.evidence as Array<Record<string, unknown>>;
      evidence.push({
        id: row.evidence_id,
        value_text: row.evidence_value_text ?? null,
        value_json: row.evidence_value_json ?? null,
        source: row.evidence_source,
        claim_kind: row.evidence_claim_kind,
        evidence_confidence: row.evidence_confidence,
        identity_confidence: row.identity_confidence,
        authority: row.authority,
        origin_count: stringArray(row.upstream_origin_keys).length,
        source_updated_at: row.source_updated_at,
        expires_at: row.expires_at,
        links: safeEvidenceLinks(row.source_items),
      });
    }
  }

  return {
    state: "pending",
    limit,
    offset,
    total_count: Number(countRows[0]?.total_count ?? 0),
    items: [...items.values()],
  };
}

type ReviewBody = {
  conflictId: number;
  expectedCity: string;
  selectedEvidenceId: string | null;
  notes: string;
};

async function reviewBody(request: Request): Promise<ReviewBody | null> {
  const contentLength = Number(request.headers.get("content-length") ?? 0);
  if (Number.isFinite(contentLength) && contentLength > 8_192) return null;
  let payload: Record<string, unknown>;
  try {
    const parsed = await request.json();
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    payload = parsed as Record<string, unknown>;
  } catch {
    return null;
  }

  const conflictId = positiveInt(payload.conflict_id);
  const expectedCity = exactText(payload.city, 100);
  const notes = exactText(payload.notes, 2_000);
  if (conflictId === null || !expectedCity || !notes) return null;
  const rawEvidence = payload.selected_evidence_id;
  let selectedEvidenceId: string | null = null;
  if (rawEvidence !== undefined && rawEvidence !== null && rawEvidence !== "") {
    if (typeof rawEvidence !== "string" || !UUID_PATTERN.test(rawEvidence)) {
      return null;
    }
    selectedEvidenceId = rawEvidence;
  }
  return { conflictId, expectedCity, selectedEvidenceId, notes };
}

async function reviewConflict(
  sql: Sql,
  user: AuthenticatedUser,
  body: ReviewBody,
): Promise<Record<string, unknown>> {
  return await sql.begin(async (tx) => {
    const conflictRows = await tx`
      select conflict.id::text as conflict_id,
             conflict.establishment_id::text as establishment_id,
             conflict.field_name,
             conflict.state,
             conflict.evidence_ids,
             establishment.city
      from review.field_conflicts conflict
      join public.establishments establishment
        on establishment.id = conflict.establishment_id
      where conflict.id = ${body.conflictId}
      for update
    `;
    const conflict = conflictRows[0];
    if (!conflict) {
      throw new RequestError("conflict_not_found", 404);
    }
    if (conflict.state !== "pending") {
      throw new RequestError("conflict_not_pending", 409);
    }

    const establishmentId = String(conflict.establishment_id);
    const fieldName = String(conflict.field_name);
    if (!REVIEWABLE_FIELDS.has(fieldName)) {
      throw new RequestError("unsupported_field", 409);
    }
    if (
      String(conflict.city).trim().toLowerCase() !==
        body.expectedCity.trim().toLowerCase()
    ) {
      throw new RequestError("city_mismatch", 409);
    }

    let evidence: Record<string, unknown> | null = null;
    if (body.selectedEvidenceId) {
      const evidenceRows = await tx`
        select id::text,
               value_text,
               normalized_value,
               value_json,
               evidence_confidence::float8,
               identity_confidence::float8,
               authority::float8,
               upstream_origin_keys
        from catalog.field_observations
        where id = ${body.selectedEvidenceId}::uuid
          and (
            establishment_id = ${establishmentId}::uuid
            or candidate_id = ${establishmentId}::uuid
          )
          and field_name = ${fieldName}
          and observation_status = 'asserted'
          and (expires_at is null or expires_at > now())
      `;
      evidence = evidenceRows[0] ?? null;
      if (!evidence) {
        throw new RequestError("evidence_not_current", 422);
      }
      validateEvidenceValue(fieldName, evidence);
    }

    const currentRows = await tx`
      select id::text
      from catalog.current_field_decisions
      where establishment_id = ${establishmentId}::uuid
        and field_name = ${fieldName}
    `;
    const currentDecisionId = currentRows[0]?.id == null
      ? null
      : String(currentRows[0].id);
    const decisionStatus = evidence ? "selected" : "unknown";
    const confidence = evidence ? reviewConfidence(evidence) : null;
    const evidenceIds = stringArray(conflict.evidence_ids);
    if (
      body.selectedEvidenceId && !evidenceIds.includes(body.selectedEvidenceId)
    ) {
      evidenceIds.push(body.selectedEvidenceId);
    }
    const fingerprint = await reviewFingerprint(
      body.conflictId,
      decisionStatus,
      body.selectedEvidenceId,
      user.id,
      body.notes,
    );
    const valueText = evidenceText(evidence);
    const normalizedValue = evidenceNormalizedValue(evidence);
    const valueJson = evidenceJsonText(evidence);
    const originKeys = evidence
      ? stringArray(evidence.upstream_origin_keys)
      : [];
    const decisionRows = await tx`
      insert into catalog.field_decisions (
        establishment_id, field_name, decision_status, value_text,
        normalized_value, value_json, confidence, resolver_version,
        evidence_ids, independent_origin_keys, reason_codes,
        supersedes_decision_id, decision_fingerprint, metadata
      ) values (
        ${establishmentId}::uuid,
        ${fieldName},
        ${decisionStatus},
        ${valueText},
        ${normalizedValue},
        ${valueJson}::jsonb,
        ${confidence},
        ${REVIEW_VERSION},
        ${tx.array(evidenceIds)}::uuid[],
        ${tx.array(originKeys)}::text[],
        ${
      tx.array(
        evidence ? ["human_evidence_review"] : ["human_review_unverified"],
      )
    }::text[],
        ${currentDecisionId}::bigint,
        ${fingerprint},
        jsonb_build_object(
          'reviewer', ${user.id},
          'reviewer_email', ${user.email},
          'notes', ${body.notes},
          'conflict_id', ${body.conflictId}
        )
      )
      returning id::text
    `;
    const decisionId = String(decisionRows[0]?.id ?? "");
    if (!decisionId) throw new Error("decision_insert_failed");

    await projectReviewedValue(
      tx,
      establishmentId,
      fieldName,
      evidence,
      confidence,
    );
    const updateRows = await tx`
      update review.field_conflicts
      set state = 'resolved',
          resolved_at = now(),
          resolved_by = ${user.id},
          resolution_notes = ${body.notes},
          decision_id = ${decisionId}::bigint
      where id = ${body.conflictId}
        and state = 'pending'
      returning id::text
    `;
    if (!updateRows[0]) throw new RequestError("conflict_not_pending", 409);
    return {
      ok: true,
      conflict_id: String(body.conflictId),
      decision_id: decisionId,
      decision_status: decisionStatus,
    };
  });
}

async function projectReviewedValue(
  tx: Sql,
  establishmentId: string,
  fieldName: string,
  evidence: Record<string, unknown> | null,
  confidence: number | null,
): Promise<void> {
  const value = evidenceText(evidence);
  const normalizedValue = evidenceNormalizedValue(evidence);
  const source = evidence ? "manual" : null;
  if (fieldName === "phone_e164") {
    await tx`
      update public.establishments
      set phone_e164 = ${value}, phone_source = ${source},
          phone_confidence = ${confidence}, updated_at = now()
      where id = ${establishmentId}::uuid
    `;
  } else if (fieldName === "website_url") {
    await tx`
      update public.establishments
      set website_url = ${value}, website_source = ${source},
          website_confidence = ${confidence}, updated_at = now()
      where id = ${establishmentId}::uuid
    `;
  } else if (fieldName === "neighborhood") {
    await tx`
      update public.establishments
      set neighborhood = ${value}, neighborhood_source = ${source},
          neighborhood_confidence = ${confidence}, updated_at = now()
      where id = ${establishmentId}::uuid
    `;
  } else if (fieldName === "address" && evidence) {
    await tx`
      update public.establishments
      set address = ${value},
          normalized_address = ${normalizedValue ?? value},
          updated_at = now()
      where id = ${establishmentId}::uuid
    `;
  } else if (fieldName === "latitude" && evidence) {
    await tx`
      update public.establishments
      set location = extensions.st_setsrid(
            extensions.st_makepoint(
              extensions.st_x(location::geometry),
              ${value}::double precision
            ),
            4326
          )::geography,
          updated_at = now()
      where id = ${establishmentId}::uuid
    `;
  } else if (fieldName === "longitude" && evidence) {
    await tx`
      update public.establishments
      set location = extensions.st_setsrid(
            extensions.st_makepoint(
              ${value}::double precision,
              extensions.st_y(location::geometry)
            ),
            4326
          )::geography,
          updated_at = now()
      where id = ${establishmentId}::uuid
    `;
  } else if (fieldName === "operating_status" && evidence) {
    await tx`
      update public.establishments
      set status = ${value}, updated_at = now()
      where id = ${establishmentId}::uuid
    `;
  } else if (fieldName === "hours") {
    const valueJson = evidenceJsonText(evidence);
    await tx`
      update public.establishments
      set hours = ${valueJson}::jsonb,
          hours_source = ${source},
          hours_confidence = ${confidence},
          updated_at = now()
      where id = ${establishmentId}::uuid
    `;
  } else if (fieldName === "price_level") {
    await tx`
      update public.establishments
      set price_level = ${value}::smallint,
          price_source = ${source},
          price_confidence = ${confidence},
          updated_at = now()
      where id = ${establishmentId}::uuid
    `;
  }
}

function evidenceText(evidence: Record<string, unknown> | null): string | null {
  return evidence && typeof evidence.value_text === "string"
    ? evidence.value_text
    : null;
}

function evidenceNormalizedValue(
  evidence: Record<string, unknown> | null,
): string | null {
  return evidence && typeof evidence.normalized_value === "string"
    ? evidence.normalized_value
    : null;
}

function evidenceJsonText(
  evidence: Record<string, unknown> | null,
): string | null {
  if (!evidence || evidence.value_json == null) return null;
  const serialized = JSON.stringify(evidence.value_json);
  return serialized ?? null;
}

function validateEvidenceValue(
  fieldName: string,
  evidence: Record<string, unknown>,
): void {
  const value = evidence.value_text;
  if (fieldName === "latitude" || fieldName === "longitude") {
    const numberValue = Number(value);
    const maximum = fieldName === "latitude" ? 90 : 180;
    if (!Number.isFinite(numberValue) || Math.abs(numberValue) > maximum) {
      throw new RequestError("invalid_evidence_value", 422);
    }
  }
  if (fieldName === "price_level") {
    const price = Number(value);
    if (!Number.isInteger(price) || price < 1 || price > 4) {
      throw new RequestError("invalid_evidence_value", 422);
    }
  }
}

function reviewConfidence(evidence: Record<string, unknown>): number {
  const evidenceConfidence = boundedNumber(evidence.evidence_confidence);
  const identityConfidence = boundedNumber(evidence.identity_confidence);
  const authority = boundedNumber(evidence.authority);
  return Math.min(
    0.99,
    Math.max(
      0,
      evidenceConfidence * identityConfidence * (0.7 + 0.3 * authority),
    ),
  );
}

async function reviewFingerprint(
  conflictId: number,
  status: string,
  evidenceId: string | null,
  reviewer: string,
  notes: string,
): Promise<string> {
  const payload = JSON.stringify({
    conflict_id: conflictId,
    evidence_id: evidenceId,
    notes,
    resolver_version: REVIEW_VERSION,
    reviewer,
    status,
  });
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(payload),
  );
  return [...new Uint8Array(digest)].map((byte) =>
    byte.toString(16).padStart(2, "0")
  )
    .join("");
}

function safeEvidenceLinks(value: unknown): Array<Record<string, string>> {
  if (!Array.isArray(value)) return [];
  const links: Array<Record<string, string>> = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const rawUrl = (item as { url?: unknown }).url;
    if (typeof rawUrl !== "string" || rawUrl.length > 2_048) continue;
    try {
      const parsed = new URL(rawUrl.trim());
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") continue;
      const kind = (item as { kind?: unknown }).kind;
      links.push({
        url: parsed.toString(),
        kind: typeof kind === "string" ? kind.slice(0, 60) : "reference",
      });
    } catch {
      // Ignore malformed provider metadata rather than returning an unsafe link.
    }
  }
  return links;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item) => typeof item === "string").map(String)
    : [];
}

function optionalText(value: string | null, maximum: number): string | null {
  const text = value?.trim().slice(0, maximum) ?? "";
  return text || null;
}

function exactText(value: unknown, maximum: number): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text && text.length <= maximum ? text : null;
}

function positiveInt(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 && parsed <= 2_147_483_647
    ? parsed
    : null;
}

function boundedInt(
  value: string | null,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const parsed = Number(value ?? fallback);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.trunc(parsed)));
}

function boundedNumber(value: unknown): number {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) ? Math.min(1, Math.max(0, parsed)) : 0;
}

class RequestError extends Error {
  status: number;
  constructor(code: string, status: number) {
    super(code);
    this.name = "RequestError";
    this.status = status;
  }
}

function mapError(error: unknown): {
  status: number;
  body: Record<string, unknown>;
} | null {
  if (!(error instanceof RequestError)) return null;
  return { status: error.status, body: { error: error.message } };
}

function errorCode(error: unknown): string {
  if (!(error instanceof Error)) return "unknown";
  return error.name || "error";
}
