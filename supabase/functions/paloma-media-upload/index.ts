import "jsr:@supabase/functions-js@2.5.0/edge-runtime.d.ts";
import { createRemoteJWKSet, jwtVerify } from "npm:jose@6.1.0";
import {
  encodedObjectPath,
  isJpeg,
  MAX_UPLOAD_BYTES,
  MEDIA_BUCKET,
  sha256Hex,
  uploadMetadata,
} from "./domain.ts";

const OWNER_USER_ID = "06e91911-fb0d-4ece-bba8-94665e7889f0";
const GITHUB_ISSUER = "https://token.actions.githubusercontent.com";
const GITHUB_AUDIENCE = "paloma-data-supabase";
const EXPECTED_REPOSITORY = "snehith01001110/paloma-data";
const EXPECTED_REPOSITORY_ID = "1337595710";
const EXPECTED_OWNER_ID = "92058509";
const EXPECTED_REF = "refs/heads/main";
const ALLOWED_WORKFLOWS = new Set([
  "snehith01001110/paloma-data/.github/workflows/sync.yml@refs/heads/main",
  "snehith01001110/paloma-data/.github/workflows/expansion.yml@refs/heads/main",
]);
const GITHUB_JWKS = createRemoteJWKSet(
  new URL(`${GITHUB_ISSUER}/.well-known/jwks`),
);

// A deployment-only bootstrap can replace this null with a one-use secret
// hash. The checked-in and final deployed function never accepts that path.
const BOOTSTRAP_TOKEN_SHA256: string | null = null;

const baseHeaders = {
  "Access-Control-Allow-Headers":
    "authorization, apikey, content-type, x-client-info, x-paloma-content-sha256, x-paloma-object-path",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
  "Content-Type": "application/json; charset=utf-8",
  "Pragma": "no-cache",
};

Deno.serve(async (request: Request) => {
  const headers = responseHeaders(request);
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers });
  }
  if (request.method !== "POST") {
    return json({ error: "method_not_allowed" }, 405, headers);
  }
  if (!(await isAuthorized(request))) {
    return json({ error: "unauthorized" }, 401, headers);
  }

  const metadata = uploadMetadata(
    request.headers.get("x-paloma-object-path"),
    request.headers.get("x-paloma-content-sha256"),
  );
  if (!metadata || request.headers.get("content-type") !== "image/jpeg") {
    return json({ error: "invalid_metadata" }, 400, headers);
  }
  const contentLength = Number(request.headers.get("content-length") ?? 0);
  if (
    !Number.isFinite(contentLength) || contentLength <= 0 ||
    contentLength > MAX_UPLOAD_BYTES
  ) {
    return json({ error: "invalid_size" }, 413, headers);
  }

  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.length !== contentLength || !isJpeg(bytes)) {
    return json({ error: "invalid_image" }, 400, headers);
  }
  if (await sha256Hex(bytes) !== metadata.sha256) {
    return json({ error: "hash_mismatch" }, 400, headers);
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    return json({ error: "storage_unavailable" }, 503, headers);
  }

  const encodedPath = encodedObjectPath(metadata.objectPath);
  const storageURL =
    `${supabaseUrl}/storage/v1/object/${MEDIA_BUCKET}/${encodedPath}`;
  const storageResponse = await fetch(storageURL, {
    method: "POST",
    headers: {
      "apikey": serviceRoleKey,
      "authorization": `Bearer ${serviceRoleKey}`,
      "cache-control": "max-age=31536000, immutable",
      "content-type": "image/jpeg",
    },
    body: bytes,
  });

  if (!storageResponse.ok) {
    const existingMatches = await existingObjectMatches(
      supabaseUrl,
      metadata.objectPath,
      metadata.sha256,
    );
    if (!existingMatches) {
      console.error("paloma-media-upload", storageResponse.status);
      return json({ error: "upload_failed" }, 502, headers);
    }
  }

  return json(
    {
      bucket: MEDIA_BUCKET,
      object_path: metadata.objectPath,
      public_url:
        `${supabaseUrl}/storage/v1/object/public/${MEDIA_BUCKET}/${encodedPath}`,
      sha256: metadata.sha256,
    },
    200,
    headers,
  );
});

function responseHeaders(request: Request): Headers {
  const headers = new Headers(baseHeaders);
  const origin = request.headers.get("origin");
  if (
    origin === "https://snehith01001110.github.io" ||
    origin?.startsWith("http://localhost:")
  ) {
    headers.set("Access-Control-Allow-Origin", origin);
  }
  headers.set("Vary", "Authorization, Origin");
  return headers;
}

function json(
  body: Record<string, unknown>,
  status: number,
  headers: Headers,
): Response {
  return new Response(JSON.stringify(body), { status, headers });
}

async function isAuthorized(request: Request): Promise<boolean> {
  if (await validBootstrapToken(request)) return true;
  const authorization = request.headers.get("authorization") ?? "";
  if (!authorization.startsWith("Bearer ")) return false;
  const token = authorization.slice(7);
  return (await isOwnerToken(token)) || (await isGithubToken(token));
}

async function validBootstrapToken(request: Request): Promise<boolean> {
  if (!BOOTSTRAP_TOKEN_SHA256) return false;
  const token = request.headers.get("x-paloma-bootstrap-token") ?? "";
  if (token.length < 32 || token.length > 256) return false;
  const actual = await sha256Hex(new TextEncoder().encode(token));
  return timingSafeEqual(actual, BOOTSTRAP_TOKEN_SHA256);
}

async function isOwnerToken(token: string): Promise<boolean> {
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY") ??
    Deno.env.get("SUPABASE_PUBLISHABLE_KEY");
  if (!supabaseUrl || !anonKey) return false;
  try {
    const response = await fetch(`${supabaseUrl}/auth/v1/user`, {
      headers: { authorization: `Bearer ${token}`, apikey: anonKey },
      cache: "no-store",
      signal: AbortSignal.timeout(4_000),
    });
    if (!response.ok) return false;
    const payload = await response.json() as { id?: unknown };
    return payload.id === OWNER_USER_ID;
  } catch {
    return false;
  }
}

async function isGithubToken(token: string): Promise<boolean> {
  try {
    const { payload } = await jwtVerify(token, GITHUB_JWKS, {
      issuer: GITHUB_ISSUER,
      audience: GITHUB_AUDIENCE,
      algorithms: ["RS256"],
    });
    return payload.repository === EXPECTED_REPOSITORY &&
      String(payload.repository_id) === EXPECTED_REPOSITORY_ID &&
      String(payload.repository_owner_id) === EXPECTED_OWNER_ID &&
      payload.ref === EXPECTED_REF &&
      payload.runner_environment === "github-hosted" &&
      ALLOWED_WORKFLOWS.has(String(payload.workflow_ref));
  } catch {
    return false;
  }
}

async function existingObjectMatches(
  supabaseUrl: string,
  objectPath: string,
  expectedSha256: string,
): Promise<boolean> {
  try {
    const response = await fetch(
      `${supabaseUrl}/storage/v1/object/public/${MEDIA_BUCKET}/${
        encodedObjectPath(objectPath)
      }`,
      { cache: "no-store", signal: AbortSignal.timeout(8_000) },
    );
    if (!response.ok) return false;
    const bytes = new Uint8Array(await response.arrayBuffer());
    return bytes.length <= MAX_UPLOAD_BYTES &&
      await sha256Hex(bytes) === expectedSha256;
  } catch {
    return false;
  }
}

function timingSafeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let mismatch = 0;
  for (let index = 0; index < left.length; index += 1) {
    mismatch |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return mismatch === 0;
}
