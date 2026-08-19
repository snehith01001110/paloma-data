import "jsr:@supabase/functions-js@2.5.0/edge-runtime.d.ts";
import { createRemoteJWKSet, jwtVerify } from "npm:jose@6.1.0";

const ISSUER = "https://token.actions.githubusercontent.com";
const AUDIENCE = "paloma-data-supabase";
const EXPECTED_REPOSITORY = "snehith01001110/paloma-data";
const EXPECTED_REPOSITORY_ID = "1337595710";
const EXPECTED_OWNER_ID = "92058509";
const EXPECTED_REF = "refs/heads/main";
const EXPECTED_WORKFLOW_REF =
  "snehith01001110/paloma-data/.github/workflows/sync.yml@refs/heads/main";
const ALLOWED_EVENTS = new Set(["push", "schedule", "workflow_dispatch"]);
const JWKS = createRemoteJWKSet(
  new URL("https://token.actions.githubusercontent.com/.well-known/jwks"),
);

const ABC_EXPORTS = [
  "https://www.abc.ca.gov/wp-content/uploads/WeeklyExport_CSV.zip",
  "https://www.abc.ca.gov/wp-content/uploads/m_tape460.zip",
];

function unauthorized(): Response {
  return Response.json(
    { error: "unauthorized" },
    { status: 401, headers: { "Cache-Control": "no-store" } },
  );
}

async function verifyGithub(req: Request): Promise<boolean> {
  const auth = req.headers.get("authorization") ?? "";
  if (!auth.startsWith("Bearer ")) return false;
  try {
    const { payload } = await jwtVerify(auth.slice(7), JWKS, {
      issuer: ISSUER,
      audience: AUDIENCE,
      algorithms: ["RS256"],
    });
    return (
      payload.repository === EXPECTED_REPOSITORY &&
      String(payload.repository_id) === EXPECTED_REPOSITORY_ID &&
      String(payload.repository_owner_id) === EXPECTED_OWNER_ID &&
      payload.ref === EXPECTED_REF &&
      payload.workflow_ref === EXPECTED_WORKFLOW_REF &&
      payload.runner_environment === "github-hosted" &&
      ALLOWED_EVENTS.has(String(payload.event_name))
    );
  } catch {
    return false;
  }
}

Deno.serve(async (req: Request) => {
  if (req.method !== "GET") return new Response(null, { status: 405 });
  if (!(await verifyGithub(req))) return unauthorized();

  const failures: string[] = [];
  for (const url of ABC_EXPORTS) {
    try {
      const upstream = await fetch(url, {
        headers: {
          "User-Agent": "Paloma/1.0 establishment-ingestion",
          "Accept": "application/zip,application/octet-stream,*/*;q=0.8",
        },
        redirect: "follow",
      });
      if (!upstream.ok || !upstream.body) {
        failures.push(`${url}:${upstream.status}`);
        continue;
      }

      const headers = new Headers();
      headers.set(
        "Content-Type",
        upstream.headers.get("content-type") ?? "application/zip",
      );
      headers.set("Cache-Control", "no-store");
      headers.set("X-Paloma-Source", "California-ABC");
      headers.set(
        "X-Paloma-Upstream",
        url.endsWith("WeeklyExport_CSV.zip") ? "csv" : "fixed-width",
      );
      const length = upstream.headers.get("content-length");
      if (length) headers.set("Content-Length", length);
      const modified = upstream.headers.get("last-modified");
      if (modified) headers.set("Last-Modified", modified);
      const etag = upstream.headers.get("etag");
      if (etag) headers.set("ETag", etag);

      // Stream exact official ABC bytes through; do not parse or transform the source here.
      return new Response(upstream.body, { status: 200, headers });
    } catch (error) {
      failures.push(
        `${url}:${error instanceof Error ? error.name : "fetch_error"}`,
      );
    }
  }

  return Response.json(
    { error: "abc_upstream_unavailable", failures },
    { status: 502, headers: { "Cache-Control": "no-store" } },
  );
});
