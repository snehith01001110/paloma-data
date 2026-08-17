import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createRemoteJWKSet, jwtVerify } from "npm:jose@6.1.0";
import postgres from "npm:postgres@3.4.7";

const ISSUER = "https://token.actions.githubusercontent.com";
const AUDIENCE = "paloma-data-supabase";
const PROJECT_REF = "lighcnfzajgvfbdoekzt";
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

function unauthorized(): Response {
  return Response.json(
    { error: "unauthorized" },
    { status: 401, headers: { "Cache-Control": "no-store" } },
  );
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") {
    return new Response(null, { status: 405 });
  }

  const auth = req.headers.get("authorization") ?? "";
  if (!auth.startsWith("Bearer ")) return unauthorized();

  try {
    const { payload } = await jwtVerify(auth.slice(7), JWKS, {
      issuer: ISSUER,
      audience: AUDIENCE,
      algorithms: ["RS256"],
    });

    if (
      payload.repository !== EXPECTED_REPOSITORY ||
      String(payload.repository_id) !== EXPECTED_REPOSITORY_ID ||
      String(payload.repository_owner_id) !== EXPECTED_OWNER_ID ||
      payload.ref !== EXPECTED_REF ||
      payload.workflow_ref !== EXPECTED_WORKFLOW_REF ||
      payload.repository_visibility !== "private" ||
      payload.runner_environment !== "github-hosted" ||
      !ALLOWED_EVENTS.has(String(payload.event_name))
    ) {
      return unauthorized();
    }

    const directDbUrl = Deno.env.get("SUPABASE_DB_URL");
    if (!directDbUrl) {
      return Response.json(
        { error: "database unavailable" },
        { status: 503, headers: { "Cache-Control": "no-store" } },
      );
    }

    const sql = postgres(directDbUrl, {
      max: 1,
      prepare: false,
      idle_timeout: 2,
      connect_timeout: 5,
    });
    const rows = await sql`
      select decrypted_secret
      from vault.decrypted_secrets
      where name = 'paloma_ingest_db_password'
      limit 1
    `;
    await sql.end({ timeout: 1 });

    const password = rows[0]?.decrypted_secret;
    if (!password) {
      return Response.json(
        { error: "credential unavailable" },
        { status: 503, headers: { "Cache-Control": "no-store" } },
      );
    }

    const pooler = new URL(
      "postgresql://placeholder@aws-0-us-west-2.pooler.supabase.com:5432/postgres",
    );
    pooler.username = `paloma_ingest.${PROJECT_REF}`;
    pooler.password = String(password);
    pooler.searchParams.set("sslmode", "require");

    return Response.json(
      { database_url: pooler.toString() },
      {
        headers: {
          "Cache-Control": "no-store",
          Pragma: "no-cache",
        },
      },
    );
  } catch {
    return unauthorized();
  }
});
