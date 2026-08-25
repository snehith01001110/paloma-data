from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import typer

from paloma_data.adapters import (
    CaliforniaABCAdapter,
    DataSFAdapter,
    FoursquareAdapter,
    OvertureAdapter,
    WikidataAdapter,
)
from paloma_data.adapters.foursquare_api import FoursquarePlacesAPI
from paloma_data.adapters.yelp import YelpPlacesAPI
from paloma_data.attribute_enrichment import OpenAttributeEnricher
from paloma_data.catalog import CATALOG_DECISION_VERSION
from paloma_data.candidate_observations import load_candidate_observation_manifest
from paloma_data.catalog_pipeline import CatalogPipeline
from paloma_data.catalog_repository import (
    POTENTIAL_SOURCE_EXCLUDED_FLAGS,
    CatalogRepository,
)
from paloma_data.config import Settings
from paloma_data.contributions import ContributionReviewer
from paloma_data.db import Database
from paloma_data.expansion import ExpansionBlocked, ExpansionGate
from paloma_data.geocoding import AddressGeocoder
from paloma_data.field_resolution import FieldResolver
from paloma_data.field_review import FieldConflictReviewer
from paloma_data.jobs import (
    JobRequest,
    PipelineJobHandler,
    PipelineQueue,
    PipelineWorker,
    catalog_refresh_requests,
    default_requester,
    default_worker_id,
    utc_now_iso,
)
from paloma_data.neighborhoods import DataSFNeighborhoodAdapter, NeighborhoodStager
from paloma_data.provider_links import ProviderLinkSync, YelpProviderAudit
from paloma_data.runtime_health import live_details_runtime_health
from paloma_data.staging import SourceStager


app = typer.Typer(no_args_is_help=True, help="Paloma accuracy-first establishment catalog")
SNAPSHOT_SOURCES = ("ca_abc", "datasf", "fsq", "overture", "wikidata")


def _components() -> tuple[Settings, Database, SourceStager, CatalogPipeline]:
    settings = Settings.from_env()
    db = Database(settings.database_url)
    stager = SourceStager(
        db,
        allowed_cities=settings.allowed_cities,
        allowed_regions=settings.allowed_regions,
        allowed_countries=settings.allowed_countries,
        allow_snapshot_shrink=settings.allow_snapshot_shrink,
    )
    return settings, db, stager, CatalogPipeline(db)


@app.command("stage-source")
def stage_source(
    source: str = typer.Argument(..., help="ca_abc, datasf, fsq, overture, or wikidata"),
    mode: str = typer.Option("incremental", help="incremental or full"),
) -> None:
    """Refresh private source evidence only; never create a product establishment."""
    settings, _, stager, _ = _components()
    if mode not in {"incremental", "full"}:
        raise typer.BadParameter("mode must be incremental or full")
    adapter = _adapter(source, settings)
    records = adapter.backfill() if mode == "full" else adapter.incremental()
    result = stager.run_snapshot(adapter.source, mode, records)
    typer.echo(json.dumps({source: result}, indent=2, sort_keys=True))


@app.command()
def bootstrap() -> None:
    """Stage configured bulk sources and build private candidates; do not publish."""
    settings, _, stager, catalog = _components()
    results: dict[str, Any] = {"publication_mutated": False}
    for source in _configured_sources(settings):
        adapter = _adapter(source, settings)
        results[source] = stager.run_snapshot(source, "full", adapter.backfill())
    results["geocode"] = _geocode_only(catalog.db)
    results["discovery"] = catalog.discover(city=None, limit=25_000, evaluation_mode="production")
    if not _fsq_bulk_configured(settings):
        results["blocked"] = "FSQ OS is not configured; no candidate can pass the catalog gate"
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command()
def backfill(
    source: str = typer.Argument(..., help="ca_abc, datasf, fsq, overture, or wikidata"),
) -> None:
    """Full private source snapshot followed by private candidate discovery."""
    settings, _, stager, catalog = _components()
    adapter = _adapter(source, settings)
    result: dict[str, Any] = {
        source: stager.run_snapshot(source, "full", adapter.backfill()),
        "publication_mutated": False,
    }
    if source == "ca_abc":
        result["geocode"] = AddressGeocoder(catalog.db).run("ca_abc")
    result["discovery"] = catalog.discover(
        city=None,
        limit=25_000,
        evaluation_mode="production",
        anchor_sources=("fsq",),
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command()
def sync(
    source: str = typer.Argument(..., help="ca_abc, datasf, fsq, overture, or wikidata"),
) -> None:
    """Refresh one complete source snapshot and reevaluate private candidates."""
    settings, _, stager, catalog = _components()
    adapter = _adapter(source, settings)
    result: dict[str, Any] = {
        source: stager.run_snapshot(source, "incremental", adapter.incremental()),
        "publication_mutated": False,
    }
    if source == "ca_abc":
        result["geocode"] = AddressGeocoder(catalog.db).run("ca_abc")
    if source == "fsq":
        result["discovery"] = catalog.discover(
            city=None,
            limit=25_000,
            evaluation_mode="production",
            anchor_sources=("fsq",),
        )
    result["reevaluation"] = catalog.reevaluate(city=None, limit=50_000)
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("sync-government")
def sync_government() -> None:
    """Refresh ABC/DataSF evidence; negative changes withdraw but never create public rows."""
    settings, _, stager, catalog = _components()
    results: dict[str, Any] = {"publication_created": False}
    for source in ("ca_abc", "datasf"):
        adapter = _adapter(source, settings)
        results[source] = stager.run_snapshot(source, "incremental", adapter.incremental())
    results["geocode"] = _geocode_only(catalog.db)
    results["reevaluation"] = catalog.reevaluate(city=None, limit=50_000)
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command("resolve-fields")
def resolve_fields() -> None:
    """Append admissible observations and re-resolve the existing catalog only."""
    _, db, _, _ = _components()
    result = FieldResolver(db).refresh_and_resolve()
    typer.echo(json.dumps(result, indent=2, sort_keys=True, default=str))


@app.command("field-coverage")
def field_coverage() -> None:
    """Report the published cohort's durable field coverage and pending conflicts."""
    _, db, _, _ = _components()
    with db.connection() as conn:
        coverage = conn.execute(
            """
            select field_name, coverage_status, count(*) as establishments
            from catalog.establishment_field_coverage
            group by field_name, coverage_status
            order by field_name, coverage_status
            """
        ).fetchall()
        conflicts = conn.execute(
            """
            select field_name, priority, count(*) as conflicts
            from review.field_conflicts
            where state = 'pending'
            group by field_name, priority
            order by priority desc, field_name
            """
        ).fetchall()
    typer.echo(
        json.dumps(
            {
                "coverage": [dict(row) for row in coverage],
                "pending_conflicts": [dict(row) for row in conflicts],
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


@app.command("live-details-health")
def live_details_health(
    require_healthy: bool = typer.Option(
        False,
        help="Exit nonzero when the private live-details eligibility contract is unhealthy",
    ),
) -> None:
    """Check runtime RLS, least privilege, and eligible public coverage."""
    _, db, _, _ = _components()
    with db.connection() as conn:
        result = live_details_runtime_health(conn)
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
    if require_healthy and not result["healthy"]:
        raise typer.Exit(code=1)


@app.command("review-field-conflict")
def review_field_conflict(
    conflict_id: int = typer.Option(...),
    city: str = typer.Option(..., help="Exact city of the conflict's establishment"),
    reviewer: str = typer.Option(...),
    notes: str = typer.Option(...),
    selected_evidence_id: str | None = typer.Option(None),
    confirm: str = typer.Option(""),
) -> None:
    """Select current evidence, or leave it omitted to record an audited unknown."""
    if confirm != "REVIEW_FIELD_CONFLICT":
        raise typer.BadParameter("Pass --confirm REVIEW_FIELD_CONFLICT")
    _, db, _, _ = _components()
    result = FieldConflictReviewer(db).review(
        conflict_id,
        reviewer=reviewer,
        notes=notes,
        selected_evidence_id=selected_evidence_id,
        expected_city=city,
    )
    typer.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@app.command("review-merchant-claim")
def review_merchant_claim(
    claim_id: str = typer.Option(...),
    decision: str = typer.Option(..., help="verified or rejected"),
    reviewer: str = typer.Option(...),
    reason: str = typer.Option(...),
    confirm: str = typer.Option(""),
) -> None:
    """Record a human merchant-claim decision with an immutable audit entry."""
    if confirm != "REVIEW_MERCHANT_CLAIM":
        raise typer.BadParameter("Pass --confirm REVIEW_MERCHANT_CLAIM")
    _, db, _, _ = _components()
    result = ContributionReviewer(db).review_merchant_claim(
        claim_id, decision=decision, reviewer=reviewer, reason=reason
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("review-contribution")
def review_contribution(
    contribution_id: str = typer.Option(...),
    decision: str = typer.Option(..., help="accepted or rejected"),
    reviewer: str = typer.Option(...),
    reason: str = typer.Option(...),
    confirm: str = typer.Option(""),
) -> None:
    """Review a firsthand or merchant fact and append accepted evidence."""
    if confirm != "REVIEW_CONTRIBUTION":
        raise typer.BadParameter("Pass --confirm REVIEW_CONTRIBUTION")
    _, db, _, _ = _components()
    result = ContributionReviewer(db).review_contribution(
        contribution_id, decision=decision, reviewer=reviewer, reason=reason
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("catalog-discover")
def catalog_discover(
    city: str | None = typer.Option(None, help="Optional exact city guardrail"),
    limit: int = typer.Option(500, min=1, max=25_000),
) -> None:
    """Turn current FSQ OS rows into private candidates and correlate legal evidence."""
    settings, _, _, catalog = _components()
    if not _fsq_bulk_configured(settings):
        raise typer.BadParameter(
            "Configure FSQ_CATALOG_URI, FSQ_CATALOG_TOKEN, and FSQ_PLACES_TABLE first"
        )
    result = catalog.discover(
        city=city,
        limit=limit,
        evaluation_mode="production",
        anchor_sources=("fsq",),
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("catalog-trial")
def catalog_trial(
    city: str = typer.Option("San Francisco", help="Exact trial city"),
    limit: int = typer.Option(20, min=1, max=100),
    verified_only: bool = typer.Option(
        False,
        help="Audit only candidates already eligible for publication; do not discover new ones",
    ),
) -> None:
    """Run a bounded FSQ verification trial without mutating the consumer catalog."""
    settings, _, _, catalog = _components()
    if not settings.fsq_places_api_key:
        raise typer.BadParameter("FSQ_PLACES_API_KEY is required for a verification trial")
    if verified_only:
        discovery: dict[str, object] = {
            "skipped": True,
            "reason": "verified_only",
            "candidate_ids": [],
        }
        with catalog.db.connection() as conn:
            candidate_ids = CatalogRepository(catalog.db).candidate_ids(
                conn,
                city=city,
                limit=limit,
                states=("verified", "published"),
                decision_version=CATALOG_DECISION_VERSION,
            )
    else:
        discovery = catalog.discover(
            city=city,
            limit=limit,
            evaluation_mode="trial",
            anchor_sources=("fsq",),
        )
        candidate_ids = list(discovery.get("candidate_ids") or ())
        if len(candidate_ids) < limit:
            with catalog.db.connection() as conn:
                existing = CatalogRepository(catalog.db).candidate_ids(
                    conn,
                    city=city,
                    limit=limit,
                    states=("needs_verification", "needs_review", "verified", "published"),
                )
            candidate_ids = list(dict.fromkeys([*candidate_ids, *existing]))[:limit]
    if not candidate_ids:
        raise RuntimeError(
            "No current FSQ OS candidates are staged for this city; run `paloma-data backfill fsq`"
        )
    storage_policy = (
        "contract" if settings.fsq_server_storage_licensed else "ephemeral"
    )
    with FoursquarePlacesAPI(
        settings.fsq_places_api_key,
        storage_policy=storage_policy,
    ) as api:
        verification = catalog.verify_with_foursquare(
            api,
            city=city,
            limit=limit,
            mode="trial",
            lease_days=settings.catalog_provider_lease_days,
            candidate_ids=candidate_ids,
        )
    typer.echo(
        json.dumps(
            {
                "scope": {
                    "city": city,
                    "limit": limit,
                    "verified_only": verified_only,
                },
                "publication_mutated": False,
                "api_storage_policy": storage_policy,
                "discovery": discovery,
                "verification": verification,
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("catalog-verify")
def catalog_verify(
    city: str | None = typer.Option(None, help="Optional exact city guardrail"),
    limit: int = typer.Option(250, min=1, max=2_000),
    existing_only: bool = typer.Option(
        False,
        help="Verify only candidates already materialized in the existing catalog cohort",
    ),
) -> None:
    """Persist specifically licensed verification evidence; still does not publish."""
    settings, db, _, catalog = _components()
    if not settings.fsq_places_api_key:
        raise typer.BadParameter("FSQ_PLACES_API_KEY is required")
    if not settings.fsq_server_storage_licensed:
        raise typer.BadParameter(
            "FSQ_SERVER_STORAGE_LICENSED must be true only when a written agreement "
            "overrides the API's no-server-caching rule"
        )
    candidate_ids: list[str] | None = None
    if existing_only:
        repository = CatalogRepository(db)
        with db.connection() as conn:
            candidate_ids = repository.materialized_candidate_ids(
                conn,
                city=city,
                limit=limit,
                publication_states=("published", "suppressed"),
            )
    with FoursquarePlacesAPI(
        settings.fsq_places_api_key,
        storage_policy="contract",
    ) as api:
        result = catalog.verify_with_foursquare(
            api,
            city=city,
            limit=limit,
            mode="production",
            lease_days=settings.catalog_provider_lease_days,
            candidate_ids=candidate_ids,
        )
    typer.echo(
        json.dumps(
            {"scope": {"existing_only": existing_only}, **result},
            indent=2,
            sort_keys=True,
        )
    )


@app.command("catalog-reevaluate")
def catalog_reevaluate(
    city: str | None = typer.Option(None),
    limit: int = typer.Option(50_000, min=1),
) -> None:
    """Re-run hard gates from stored evidence and withdraw hard negatives."""
    _, _, _, catalog = _components()
    typer.echo(json.dumps(catalog.reevaluate(city=city, limit=limit), indent=2, sort_keys=True))


@app.command("catalog-publish")
def catalog_publish(
    release_id: str = typer.Option(..., help="Approved release ID from the Bay Area manifest"),
    confirm: str = typer.Option("", help="Must be exactly PUBLISH_VERIFIED"),
    limit: int = typer.Option(2_000, min=1),
) -> None:
    """Materialize a bounded, authorized release of unexpired verified candidates."""
    if confirm != "PUBLISH_VERIFIED":
        raise typer.BadParameter("Pass --confirm PUBLISH_VERIFIED")
    _, _, _, catalog = _components()
    try:
        result = catalog.publish(release_id=release_id, limit=limit)
    except ExpansionBlocked as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        json.dumps(
            result,
            indent=2,
            sort_keys=True,
        )
    )


@app.command("expansion-status")
def expansion_status(
    release_id: str | None = typer.Option(None, help="Optional single release ID"),
    require_ready: bool = typer.Option(
        False,
        help="Exit nonzero unless every selected release is ready",
    ),
) -> None:
    """Report immutable authorizations, safety checks, and remaining batch slots."""
    _, db, _, _ = _components()
    gate = ExpansionGate(db)
    if release_id:
        with db.connection() as conn:
            status = gate.status(conn, release_id)
        payload: dict[str, Any] = {
            "manifest_id": gate.manifest.manifest_id,
            "manifest_sha256": gate.manifest.sha256,
            "releases": {release_id: status},
        }
    else:
        payload = gate.all_statuses()
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if require_ready and not all(
        bool(status.get("ready")) for status in payload["releases"].values()
    ):
        raise typer.Exit(code=1)


@app.command("catalog-sweep")
def catalog_sweep() -> None:
    """Immediately hide published rows whose verification lease expired."""
    _, db, _, _ = _components()
    repo = CatalogRepository(db)
    with db.connection() as conn:
        withdrawn = repo.withdraw_expired(conn)
        conn.commit()
    typer.echo(json.dumps({"expired_withdrawn": withdrawn}, indent=2))


@app.command("provider-links-sync")
def provider_links_sync(
    provider: str = typer.Option("yelp", help="Provider whose durable IDs should be synced"),
    city: str | None = typer.Option(None, help="Optional exact city guardrail"),
    limit: int = typer.Option(25, min=1, max=500, help="Maximum paid API calls"),
) -> None:
    """Resolve durable provider IDs ahead of user traffic; never retain search payloads."""
    settings, db, _, _ = _components()
    if provider != "yelp":
        raise typer.BadParameter("yelp is the only proactive provider matcher currently reviewed")
    if not settings.yelp_api_key:
        raise typer.BadParameter("YELP_API_KEY is required")
    with YelpPlacesAPI(settings.yelp_api_key) as api:
        result = ProviderLinkSync(db).run(api, city=city, limit=limit)
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("provider-audit")
def provider_audit(
    provider: str = typer.Option("yelp", help="Provider to audit"),
    city: str | None = typer.Option(None, help="Optional exact city guardrail"),
    limit: int = typer.Option(100, min=1, max=500, help="Maximum calls per audit section"),
) -> None:
    """Audit live Yelp coverage and rejected matches without retaining provider fields."""
    settings, db, _, _ = _components()
    if provider != "yelp":
        raise typer.BadParameter("yelp is the only provider audit currently implemented")
    if not settings.yelp_api_key:
        raise typer.BadParameter("YELP_API_KEY is required")
    with YelpPlacesAPI(settings.yelp_api_key) as api:
        result = YelpProviderAudit(db).run(api, city=city, limit=limit)
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("pipeline-enqueue-catalog")
def pipeline_enqueue_catalog(
    city: str | None = typer.Option(None, help="Optional exact city guardrail"),
    limit: int = typer.Option(50_000, min=1, max=50_000),
    include_unpublished: bool = typer.Option(
        False,
        help="Queue private candidates too; refresh still cannot publish a new identity",
    ),
    include_suppressed: bool = typer.Option(
        False,
        help="Also refresh previously materialized rows that are currently suppressed",
    ),
    max_attempts: int = typer.Option(5, min=1, max=25),
) -> None:
    """Queue one idempotent refresh for each selected catalog identity."""
    _, db, _, _ = _components()
    repository = CatalogRepository(db)
    with db.connection() as conn:
        if include_unpublished:
            candidate_ids = repository.candidate_ids(conn, city=city, limit=limit)
        else:
            candidate_ids = repository.materialized_candidate_ids(
                conn,
                city=city,
                limit=limit,
                publication_states=("published", "suppressed")
                if include_suppressed
                else ("published",),
            )
    if not candidate_ids:
        typer.echo(
            json.dumps(
                {
                    "run_id": None,
                    "scope": {"city": city, "limit": limit},
                    "requested": 0,
                    "created": 0,
                    "deduplicated": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    queue = PipelineQueue(db)
    run_id = queue.create_run(
        "catalog_refresh",
        requested_by=default_requester(),
        metadata={
            "city": city,
            "limit": limit,
            "include_unpublished": include_unpublished,
            "include_suppressed": include_suppressed,
            "decision_version": CATALOG_DECISION_VERSION,
            "enqueued_at": utc_now_iso(),
        },
    )
    counts = queue.enqueue_many(
        run_id,
        catalog_refresh_requests(
            candidate_ids,
            decision_version=CATALOG_DECISION_VERSION,
            max_attempts=max_attempts,
        ),
    )
    typer.echo(
        json.dumps(
            {"run_id": run_id, "scope": {"city": city, "limit": limit}, **counts},
            indent=2,
            sort_keys=True,
        )
    )


@app.command("pipeline-enqueue-sweep")
def pipeline_enqueue_sweep(
    max_attempts: int = typer.Option(5, min=1, max=25),
) -> None:
    """Queue the fail-closed verification-expiry sweep."""
    _, db, _, _ = _components()
    queue = PipelineQueue(db)
    run_id = queue.create_run(
        "catalog_sweep",
        requested_by=default_requester(),
        metadata={"enqueued_at": utc_now_iso()},
    )
    counts = queue.enqueue_many(
        run_id,
        [
            JobRequest(
                job_type="catalog_sweep",
                dedupe_key="catalog_sweep",
                payload={},
                max_attempts=max_attempts,
            )
        ],
    )
    typer.echo(json.dumps({"run_id": run_id, **counts}, indent=2, sort_keys=True))


@app.command("pipeline-enqueue-provider-links")
def pipeline_enqueue_provider_links(
    city: str | None = typer.Option(None, help="Optional exact city guardrail"),
    limit: int = typer.Option(250, min=1, max=500),
    max_attempts: int = typer.Option(5, min=1, max=25),
) -> None:
    """Queue a bounded Yelp durable-ID sync without storing provider attributes."""
    settings, db, _, _ = _components()
    if not settings.yelp_api_key:
        raise typer.BadParameter("YELP_API_KEY is required")
    queue = PipelineQueue(db)
    run_id = queue.create_run(
        "provider_links_sync",
        requested_by=default_requester(),
        metadata={"city": city, "limit": limit, "enqueued_at": utc_now_iso()},
    )
    counts = queue.enqueue_many(
        run_id,
        [
            JobRequest(
                job_type="provider_links_sync",
                dedupe_key=f"yelp:{city or 'all'}",
                payload={"city": city, "limit": limit},
                max_attempts=max_attempts,
            )
        ],
    )
    typer.echo(json.dumps({"run_id": run_id, **counts}, indent=2, sort_keys=True))


@app.command("pipeline-worker")
def pipeline_worker(
    drain: bool = typer.Option(
        False,
        "--drain",
        help="Exit when no messages are immediately visible",
    ),
    max_jobs: int | None = typer.Option(None, min=1),
    batch_size: int = typer.Option(1, min=1, max=100),
    visibility_seconds: int = typer.Option(900, min=30, max=7_200),
    poll_seconds: float = typer.Option(2.0, min=0.0, max=60.0),
    idle_timeout_seconds: float | None = typer.Option(
        None,
        min=1.0,
        help="Wait this long for delayed work before exiting",
    ),
    worker_id: str | None = typer.Option(None, help="Stable runtime instance identifier"),
    fail_on_error: bool = typer.Option(
        False,
        help="Exit non-zero when a job remains retrying or is dead-lettered",
    ),
) -> None:
    """Consume reviewed background job types from the private durable queue."""
    settings, db, _, _ = _components()
    queue = PipelineQueue(db)
    worker = PipelineWorker(
        queue,
        PipelineJobHandler(settings, db),
        worker_id=worker_id or default_worker_id(),
        visibility_seconds=visibility_seconds,
        batch_size=batch_size,
        poll_seconds=poll_seconds,
    )
    result = worker.run(
        drain=drain,
        max_jobs=max_jobs,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True, default=str))
    if fail_on_error and (result["unresolved_retries"] or result["dead"]):
        raise typer.Exit(code=1)


@app.command("pipeline-status")
def pipeline_status(
    recent_runs: int = typer.Option(20, min=1, max=100),
) -> None:
    """Report queue latency, logical runs, and recent terminal failures."""
    _, db, _, _ = _components()
    typer.echo(
        json.dumps(
            PipelineQueue(db).status(recent_runs=recent_runs),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


@app.command("catalog-status")
def catalog_status() -> None:
    """Report source readiness, private decisions, publication, and field coverage."""
    settings, db, _, _ = _components()
    with db.connection() as conn:
        schema = conn.execute(
            "select to_regclass('ingest.catalog_candidates')::text as table_name"
        ).fetchone()
        if not schema["table_name"]:
            raise RuntimeError("catalog v2 migration is not applied")
        sources = conn.execute(
            """
            select source, count(*) filter (where retired_at is null) as current_records,
                   max(last_seen_at) as last_seen_at,
                   count(*) filter (where source_status = 'open' and retired_at is null) as open_records
            from ingest.source_records
            group by source order by source
            """
        ).fetchall()
        sync_state = conn.execute(
            """
            select source, completed_at, record_count, release_id, cursor_after
            from ingest.source_sync_state order by source
            """
        ).fetchall()
        decisions = conn.execute(
            """
            select candidate_state, count(*) as count
            from ingest.catalog_candidates group by candidate_state order by candidate_state
            """
        ).fetchall()
        blockers = conn.execute(
            """
            select decision_reason, count(*) as count
            from ingest.catalog_candidates
            where candidate_state not in ('verified', 'published')
            group by decision_reason
            order by count(*) desc, decision_reason
            limit 15
            """
        ).fetchall()
        work = conn.execute(
            """
            select
              (select count(*) from ingest.candidate_match_reviews where state = 'pending')
                as pending_match_reviews,
              count(*) filter (
                where candidate_state in ('needs_verification', 'needs_review')
                   or (
                     candidate_state in ('verified', 'published')
                     and (verification_expires_at is null
                          or verification_expires_at <= now() + interval '14 days')
                   )
              ) as provider_calls_due,
              count(*) filter (
                where candidate_state in ('verified', 'published')
                  and verification_expires_at > now()
              ) as currently_verified
            from ingest.catalog_candidates
            """
        ).fetchone()
        private_fields = conn.execute(
            """
            with current_verified as (
              select *
              from ingest.catalog_candidates
              where candidate_state = 'verified'
                and decision_version = %s
                and verification_expires_at > now()
            )
            select
              count(*) as verified,
              count(*) filter (
                where nullif(trim(resolved_snapshot->>'neighborhood'), '') is not null
              ) as neighborhood,
              count(*) filter (
                where nullif(trim(resolved_snapshot->>'phone_e164'), '') is not null
              ) as phone,
              count(*) filter (
                where nullif(trim(resolved_snapshot->>'website_url'), '') is not null
              ) as website,
              count(*) filter (
                where resolved_snapshot->'hours' is not null
                  and resolved_snapshot->'hours' <> 'null'::jsonb
                  and resolved_snapshot->'hours' <> '{}'::jsonb
                  and resolved_snapshot->'hours' <> '[]'::jsonb
              ) as hours,
              count(*) filter (
                where jsonb_typeof(resolved_snapshot->'price_level') = 'number'
              ) as price,
              count(*) filter (
                where case
                  when jsonb_typeof(resolved_snapshot->'setting_slugs') = 'array'
                    then jsonb_array_length(resolved_snapshot->'setting_slugs') > 0
                  else false
                end
              ) as settings,
              count(*) filter (
                where nullif(trim(resolved_snapshot->>'cover_image_url'), '') is not null
              ) as cover_image,
              count(*) filter (
                where nullif(trim(resolved_snapshot->>'name'), '') is null
                   or nullif(trim(resolved_snapshot->>'primary_type_slug'), '') is null
                   or nullif(trim(resolved_snapshot->>'address'), '') is null
                   or nullif(trim(resolved_snapshot->>'city'), '') is null
                   or nullif(trim(resolved_snapshot->>'country_code'), '') is null
                   or resolved_snapshot->'latitude' is null
                   or resolved_snapshot->'latitude' = 'null'::jsonb
                   or resolved_snapshot->'longitude' is null
                   or resolved_snapshot->'longitude' = 'null'::jsonb
              ) as missing_required
            from current_verified
            """,
            (CATALOG_DECISION_VERSION,),
        ).fetchone()
        verified_types = conn.execute(
            """
            select resolved_snapshot->>'primary_type_slug' as primary_type,
                   count(*) as count
            from ingest.catalog_candidates
            where candidate_state = 'verified'
              and decision_version = %s
              and verification_expires_at > now()
            group by resolved_snapshot->>'primary_type_slug'
            order by count(*) desc, primary_type
            """,
            (CATALOG_DECISION_VERSION,),
        ).fetchall()
        review_risk = conn.execute(
            """
            select
              count(*) filter (where r.state = 'pending') as pending_items,
              count(distinct c.id) filter (where r.state = 'pending')
                as verified_candidates_with_pending_items,
              count(distinct c.id) filter (
                where r.state = 'pending'
                  and sr.retired_at is null
                  and sr.source_status = 'open'
                  and not (sr.quality_flags && %s::text[])
                  and sr.normalized_address = c.normalized_address
                  and (
                    r.reason like '%%same_location_name_conflict'
                    or r.reason like '%%probable_identity_needs_review'
                  )
              ) as verified_candidates_with_exact_address_conflict,
              (
                select count(*)
                from ingest.catalog_candidates blocked
                where blocked.decision_version = %s
                  and blocked.decision_reason = %s
              ) as candidates_blocked_by_exact_address_conflict
            from ingest.catalog_candidates c
            left join ingest.candidate_match_reviews r on r.candidate_id = c.id
            left join ingest.source_records sr
              on sr.source = r.source and sr.source_record_id = r.source_record_id
            where c.candidate_state = 'verified'
              and c.decision_version = %s
              and c.verification_expires_at > now()
            """,
            (
                sorted(POTENTIAL_SOURCE_EXCLUDED_FLAGS),
                CATALOG_DECISION_VERSION,
                (
                    "unresolved_exact_address_identity_conflict:"
                    f"{CATALOG_DECISION_VERSION}"
                ),
                CATALOG_DECISION_VERSION,
            ),
        ).fetchone()
        exact_address_conflicts = conn.execute(
            """
            select r.id as review_id, c.id::text as candidate_id,
                   c.name as candidate_name,
                   r.source, sr.name as conflicting_name, c.address,
                   r.reason, r.score::float
            from ingest.catalog_candidates c
            join ingest.candidate_match_reviews r on r.candidate_id = c.id
            join ingest.source_records sr
              on sr.source = r.source and sr.source_record_id = r.source_record_id
            where c.decision_version = %s
              and (
                (
                  c.candidate_state in ('verified', 'published')
                  and c.verification_expires_at > now()
                )
                or c.decision_reason = %s
              )
              and r.state = 'pending'
              and sr.retired_at is null
              and sr.source_status = 'open'
              and not (sr.quality_flags && %s::text[])
              and sr.normalized_address = c.normalized_address
              and (
                r.reason like '%%same_location_name_conflict'
                or r.reason like '%%probable_identity_needs_review'
              )
            order by c.name, r.score desc, r.source
            limit 50
            """,
            (
                CATALOG_DECISION_VERSION,
                (
                    "unresolved_exact_address_identity_conflict:"
                    f"{CATALOG_DECISION_VERSION}"
                ),
                sorted(POTENTIAL_SOURCE_EXCLUDED_FLAGS),
            ),
        ).fetchall()
        invariant_risk = conn.execute(
            """
            with current_verified as (
              select c.*,
                     sr.normalized_name as anchor_normalized_name,
                     sr.normalized_address as anchor_normalized_address,
                     sr.primary_type_slug as anchor_primary_type_slug,
                     sr.latitude as anchor_latitude,
                     sr.longitude as anchor_longitude
              from ingest.catalog_candidates c
              left join ingest.source_records sr
                on sr.source = c.anchor_source
               and sr.source_record_id = c.anchor_source_record_id
              where c.candidate_state in ('verified', 'published')
                and c.decision_version = %s
                and c.verification_expires_at > now()
            ), probable_duplicates as (
              select a.id as left_id, b.id as right_id
              from current_verified a
              join current_verified b
                on a.id < b.id
               and lower(a.city) = lower(b.city)
               and a.country_code = b.country_code
              where (
                a.normalized_address = b.normalized_address
                and extensions.similarity(a.normalized_name, b.normalized_name) >= 0.75
              ) or (
                ST_DWithin(a.location, b.location, 20)
                and extensions.similarity(a.normalized_name, b.normalized_name) >= 0.92
              )
            )
            select
              count(*) filter (
                where candidate_state in ('verified', 'published')
                  and decision_version is distinct from %s
              ) as stale_decision_version,
              count(*) filter (
                where candidate_state in ('verified', 'published')
                  and (
                    verification_expires_at is null
                    or verification_expires_at <= now()
                  )
              ) as expired_verified_state,
              count(*) filter (
                where candidate_state in ('verified', 'published')
                  and resolved_snapshot->>'primary_type_slug'
                    in ('brewery', 'winery', 'distillery')
                  and verification_tier is distinct from 'manual'
              ) as automated_generic_manufacturers,
              (select count(*) from probable_duplicates) as probable_duplicate_pairs,
              (
                select count(*)
                from current_verified current
                where current.anchor_normalized_name is null
                   or current.normalized_name is distinct from current.anchor_normalized_name
                   or current.normalized_address
                        is distinct from current.anchor_normalized_address
                   or current.primary_type_slug
                        is distinct from current.anchor_primary_type_slug
                   or current.anchor_latitude is null
                   or current.anchor_longitude is null
                   or ST_Distance(
                        current.location,
                        ST_SetSRID(
                          ST_MakePoint(
                            current.anchor_longitude,
                            current.anchor_latitude
                          ),
                          4326
                        )::geography
                      ) > 1
              ) as candidate_anchor_drift,
              (
                select count(*)
                from current_verified current
                where (
                  nullif(trim(current.resolved_snapshot->>'phone_e164'), '') is not null
                  and nullif(
                    trim(current.resolved_snapshot#>>'{field_sources,phone}'), ''
                  ) is null
                ) or (
                  nullif(trim(current.resolved_snapshot->>'website_url'), '') is not null
                  and nullif(
                    trim(current.resolved_snapshot#>>'{field_sources,website}'), ''
                  ) is null
                ) or (
                  current.resolved_snapshot->'hours' is not null
                  and current.resolved_snapshot->'hours' <> 'null'::jsonb
                  and nullif(
                    trim(current.resolved_snapshot#>>'{field_sources,hours}'), ''
                  ) is null
                ) or (
                  current.resolved_snapshot->'price_level' is not null
                  and current.resolved_snapshot->'price_level' <> 'null'::jsonb
                  and nullif(
                    trim(current.resolved_snapshot#>>'{field_sources,price}'), ''
                  ) is null
                ) or (
                  nullif(trim(current.resolved_snapshot->>'neighborhood'), '') is not null
                  and nullif(
                    trim(current.resolved_snapshot#>>'{field_sources,neighborhood}'), ''
                  ) is null
                ) or (
                  current.resolved_snapshot->'setting_slugs' is not null
                  and current.resolved_snapshot->'setting_slugs' <> 'null'::jsonb
                  and current.resolved_snapshot->'setting_slugs' <> '[]'::jsonb
                  and nullif(
                    trim(current.resolved_snapshot#>>'{field_sources,settings}'), ''
                  ) is null
                )
              ) as optional_fields_missing_provenance
            from ingest.catalog_candidates
            """,
            (CATALOG_DECISION_VERSION, CATALOG_DECISION_VERSION),
        ).fetchone()
        publication = conn.execute(
            """
            select count(*) as rows_total,
                   count(*) filter (where publication_state = 'published' and status = 'open') as live,
                   count(*) filter (
                     where publication_state = 'published' and catalog_candidate_id is not null
                   ) as v2_live,
                   count(*) filter (
                     where publication_state = 'published' and catalog_candidate_id is null
                   ) as unsafe_legacy_live,
                   count(*) filter (
                     where publication_state = 'published'
                       and verification_expires_at > now()
                   ) as unexpired,
                   count(*) filter (
                     where publication_state = 'published'
                       and (verification_expires_at is null or verification_expires_at <= now())
                   ) as unsafe_expired_live,
                   count(*) filter (where publication_state = 'published' and phone_e164 is not null) as phone,
                   count(*) filter (where publication_state = 'published' and website_url is not null) as website,
                   count(*) filter (where publication_state = 'published' and neighborhood is not null) as neighborhood,
                   count(*) filter (where publication_state = 'published' and hours is not null) as hours,
                   count(*) filter (where publication_state = 'published' and price_level is not null) as price,
                   count(*) filter (
                     where publication_state = 'published'
                       and exists (
                         select 1 from public.establishment_settings setting
                         where setting.establishment_id = establishments.id
                       )
                   ) as settings,
                   count(*) filter (where publication_state = 'published' and cover_image_url is not null) as cover_image
            from public.establishments
            """
        ).fetchone()
    typer.echo(
        json.dumps(
            {
                "decision_version": CATALOG_DECISION_VERSION,
                "configuration": {
                    "fsq_os": _fsq_bulk_configured(settings),
                    "fsq_api": bool(settings.fsq_places_api_key),
                    "fsq_server_storage_licensed": settings.fsq_server_storage_licensed,
                    "sf_neighborhoods": bool(settings.sf_neighborhoods_url),
                },
                "sources": [dict(row) for row in sources],
                "source_snapshots": [dict(row) for row in sync_state],
                "candidates": [dict(row) for row in decisions],
                "top_blockers": [dict(row) for row in blockers],
                "work_queue": dict(work),
                "private_verified_field_coverage": dict(private_fields),
                "private_verified_types": [dict(row) for row in verified_types],
                "private_review_risk": dict(review_risk),
                "private_exact_address_conflicts": [
                    dict(row) for row in exact_address_conflicts
                ],
                "private_invariant_risk": dict(invariant_risk),
                "public": dict(publication),
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


@app.command("catalog-review-resolve")
def catalog_review_resolve(
    review_id: int = typer.Option(..., min=1),
    reviewer: str = typer.Option(..., help="Identified Paloma reviewer"),
    resolution: str = typer.Option(
        ...,
        help="same_place or not_same_or_stale",
    ),
    city: str | None = typer.Option(None, help="Optional exact candidate-city guardrail"),
    note: str | None = typer.Option(None, help="Optional short decision rationale"),
    confirm: str = typer.Option("", help="Must be exactly RESOLVE_MATCH_REVIEW"),
) -> None:
    """Resolve one exact-premise conflict; never publish the resulting candidate."""
    if confirm != "RESOLVE_MATCH_REVIEW":
        raise typer.BadParameter("Pass --confirm RESOLVE_MATCH_REVIEW")
    if resolution not in {"same_place", "not_same_or_stale"}:
        raise typer.BadParameter("resolution must be same_place or not_same_or_stale")
    _, _, _, catalog = _components()
    typer.echo(
        json.dumps(
            catalog.resolve_match_review(
                review_id,
                resolution=resolution,
                reviewer=reviewer,
                expected_city=city,
                note=note,
            ),
            indent=2,
            sort_keys=True,
        )
    )


@app.command("catalog-attest")
def catalog_attest(
    candidate_id: str = typer.Option(..., help="Exact private catalog candidate UUID"),
    reviewer: str = typer.Option(..., help="Identified Paloma reviewer"),
    evidence_url: list[str] = typer.Option(
        ...,
        "--evidence-url",
        help="Current first-party or authoritative HTTPS evidence; repeat as needed",
    ),
    city: str | None = typer.Option(None, help="Optional exact city guardrail"),
    outcome: str = typer.Option("pass", help="pass for current venue; fail for hard negative"),
    venue_type: str | None = typer.Option(
        None,
        help="Reviewed consumer venue type when correcting a coarse anchor type",
    ),
    note: str | None = typer.Option(
        None,
        help="Short review rationale; do not copy provider detail fields",
    ),
    lease_days: int = typer.Option(90, min=1, max=90),
    confirm: str = typer.Option("", help="Must be exactly MANUAL_ATTESTATION"),
) -> None:
    """Append a bounded manual hard-gate attestation; never publish the candidate."""
    if confirm != "MANUAL_ATTESTATION":
        raise typer.BadParameter("Pass --confirm MANUAL_ATTESTATION")
    if outcome not in {"pass", "fail"}:
        raise typer.BadParameter("outcome must be pass or fail")
    _, _, _, catalog = _components()
    typer.echo(
        json.dumps(
            catalog.attest_candidate(
                candidate_id,
                reviewer=reviewer,
                evidence_urls=tuple(evidence_url),
                expected_city=city,
                outcome=outcome,
                venue_type=venue_type,
                note=note,
                lease_days=lease_days,
            ),
            indent=2,
            sort_keys=True,
        )
    )


@app.command("catalog-observe-field")
def catalog_observe_field(
    candidate_id: str = typer.Option(..., help="Exact private catalog candidate UUID"),
    field_name: str = typer.Option(
        ...,
        help="phone_e164, website_url, neighborhood, hours, price_level, or setting_slug",
    ),
    value_json: str = typer.Option(
        ...,
        "--value-json",
        help="One JSON scalar/object/array containing the independently reviewed fact",
    ),
    reviewer: str = typer.Option(..., help="Identified Paloma reviewer"),
    evidence_url: list[str] = typer.Option(
        ...,
        "--evidence-url",
        help="Current factual-reference HTTPS URL; repeat as needed",
    ),
    city: str | None = typer.Option(None, help="Optional exact candidate-city guardrail"),
    note: str | None = typer.Option(
        None,
        help="Short independent-review rationale; never paste provider/page payloads",
    ),
    lease_days: int | None = typer.Option(None, min=1, max=365),
    confirm: str = typer.Option("", help="Must be exactly RECORD_FIELD_OBSERVATION"),
) -> None:
    """Append a rights-checked atomic fact to a private candidate; never publish it."""
    if confirm != "RECORD_FIELD_OBSERVATION":
        raise typer.BadParameter("Pass --confirm RECORD_FIELD_OBSERVATION")
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("--value-json must contain valid JSON") from exc
    _, _, _, catalog = _components()
    typer.echo(
        json.dumps(
            catalog.observe_candidate_field(
                candidate_id,
                field_name=field_name,
                value=value,
                reviewer=reviewer,
                evidence_urls=tuple(evidence_url),
                expected_city=city,
                note=note,
                lease_days=lease_days,
            ),
            indent=2,
            sort_keys=True,
        )
    )


@app.command("catalog-observe-manifest")
def catalog_observe_manifest(
    reviewer: str = typer.Option(..., help="Identified Paloma reviewer"),
    confirm: str = typer.Option("", help="Must be exactly RECORD_FIELD_MANIFEST"),
) -> None:
    """Apply the checked-in East Bay private field batch; never publish it."""
    if confirm != "RECORD_FIELD_MANIFEST":
        raise typer.BadParameter("Pass --confirm RECORD_FIELD_MANIFEST")
    manifest = load_candidate_observation_manifest()
    _, _, _, catalog = _components()
    typer.echo(
        json.dumps(
            catalog.observe_candidate_manifest(manifest, reviewer=reviewer),
            indent=2,
            sort_keys=True,
        )
    )


@app.command("catalog-audit")
def catalog_audit(
    city: str | None = typer.Option(None, help="Optional exact city guardrail"),
    limit: int = typer.Option(500, min=1, max=5_000),
) -> None:
    """Export the current private verified set and its decisive evidence for review."""
    _, db, _, _ = _components()
    with db.connection() as conn:
        rows = conn.execute(
            """
            select
              c.id::text as candidate_id,
              c.name,
              c.primary_type_slug,
              c.address,
              c.city,
              c.resolved_snapshot->>'neighborhood' as neighborhood,
              consumer.source_record_id as consumer_source_record_id,
              consumer.source_updated_at as consumer_refreshed_at,
              consumer.classification_confidence::float
                as consumer_classification_confidence,
              consumer.quality_flags as consumer_quality_flags,
              c.identity_confidence::float as identity_confidence,
              c.verification_tier,
              c.verified_at,
              c.verification_expires_at,
              jsonb_build_object(
                'phone', nullif(trim(c.resolved_snapshot->>'phone_e164'), '') is not null,
                'website', nullif(trim(c.resolved_snapshot->>'website_url'), '') is not null,
                'hours', c.resolved_snapshot->'hours' is not null
                  and c.resolved_snapshot->'hours' <> 'null'::jsonb
                  and c.resolved_snapshot->'hours' <> '{}'::jsonb
                  and c.resolved_snapshot->'hours' <> '[]'::jsonb,
                'price', jsonb_typeof(c.resolved_snapshot->'price_level') = 'number',
                'settings', case
                  when jsonb_typeof(c.resolved_snapshot->'setting_slugs') = 'array'
                    then jsonb_array_length(c.resolved_snapshot->'setting_slugs') > 0
                  else false
                end
              ) as optional_field_coverage,
              coalesce((
                select jsonb_agg(
                  jsonb_build_object(
                    'record_id', sr.source_record_id,
                    'name', sr.name,
                    'license_type', sr.permitted_metadata->>'license_type',
                    'type_status', sr.permitted_metadata->>'type_status',
                    'license_or_application',
                      sr.permitted_metadata->>'license_or_application',
                    'identity_confidence', csl.identity_confidence::float
                  ) order by sr.source_record_id
                )
                from ingest.candidate_source_links csl
                join ingest.source_records sr
                  on sr.source = csl.source
                 and sr.source_record_id = csl.source_record_id
                where csl.candidate_id = c.id and sr.source = 'ca_abc'
              ), '[]'::jsonb) as abc_licenses,
              array(
                select distinct csl.source
                from ingest.candidate_source_links csl
                where csl.candidate_id = c.id
                order by csl.source
              ) as linked_sources,
              (
                select count(*)
                from ingest.candidate_match_reviews r
                where r.candidate_id = c.id and r.state = 'pending'
              ) as pending_review_items,
              coalesce((
                select jsonb_agg(
                  jsonb_build_object(
                    'review_id', r.id,
                    'source', r.source,
                    'name', sr.name,
                    'address', sr.address,
                    'reason', r.reason,
                    'score', r.score::float,
                    'distance_m', r.evidence#>'{features,distance_m}'
                  ) order by r.score desc, r.source, r.id
                )
                from ingest.candidate_match_reviews r
                join ingest.source_records sr
                  on sr.source = r.source and sr.source_record_id = r.source_record_id
                where r.candidate_id = c.id and r.state = 'pending'
              ), '[]'::jsonb) as pending_reviews,
              (
                select count(*)
                from ingest.candidate_match_reviews r
                join ingest.source_records sr
                  on sr.source = r.source and sr.source_record_id = r.source_record_id
                where r.candidate_id = c.id
                  and r.state = 'pending'
                  and sr.retired_at is null
                  and sr.source_status = 'open'
                  and not (sr.quality_flags && %s::text[])
                  and sr.normalized_address = c.normalized_address
                  and (
                    r.reason like '%%same_location_name_conflict'
                    or r.reason like '%%probable_identity_needs_review'
                  )
              ) as blocking_exact_address_conflicts
            from ingest.catalog_candidates c
            join ingest.source_records consumer
              on consumer.source = c.anchor_source
             and consumer.source_record_id = c.anchor_source_record_id
            where (%s::text is null or lower(c.city) = lower(%s::text))
              and c.candidate_state in ('verified', 'published')
              and c.decision_version = %s
              and c.verification_expires_at > now()
            order by c.name, c.address, c.id
            limit %s
            """,
            (
                sorted(POTENTIAL_SOURCE_EXCLUDED_FLAGS),
                city,
                city,
                CATALOG_DECISION_VERSION,
                limit,
            ),
        ).fetchall()
    typer.echo(
        json.dumps(
            {
                "decision_version": CATALOG_DECISION_VERSION,
                "scope": {"city": city, "limit": limit},
                "publication_mutated": False,
                "count": len(rows),
                "candidates": [dict(row) for row in rows],
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


@app.command("sync-neighborhoods")
def sync_neighborhoods() -> None:
    """Refresh civic boundary evidence used for deterministic neighborhood labels."""
    settings, db, _, _ = _components()
    stager = NeighborhoodStager(
        db,
        allow_snapshot_shrink=settings.allow_snapshot_shrink,
    )
    with DataSFNeighborhoodAdapter(settings.sf_neighborhoods_url) as adapter:
        result = stager.run(adapter)
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("enrich-open-attributes")
def enrich_open_attributes(
    candidate_limit: int = typer.Option(5_000, min=1, max=50_000),
) -> None:
    """Refresh reviewed open attribute layers and re-resolve; never publish."""
    settings, db, _, catalog = _components()
    enrichment = OpenAttributeEnricher(
        db,
        bbox=settings.overture_bbox,
        overpass_url=settings.osm_overpass_url,
    ).run()
    candidates = catalog.reevaluate(
        city=None,
        limit=candidate_limit,
        states=("verified",),
    )
    fields = FieldResolver(db).refresh_and_resolve()
    typer.echo(
        json.dumps(
            {
                "enrichment": enrichment,
                "candidate_decisions": candidates,
                "public_field_resolution": fields,
                "publication_mutated": False,
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command()
def geocode(source: str = typer.Argument("ca_abc")) -> None:
    """Geocode private source evidence; never reconcile directly into public rows."""
    _, db, _, _ = _components()
    typer.echo(json.dumps(AddressGeocoder(db).run(source), indent=2, sort_keys=True))


def _adapter(source: str, settings: Settings):
    if source == "ca_abc":
        return CaliforniaABCAdapter(settings.abc_reports_url)
    if source == "datasf":
        return DataSFAdapter(settings.datasf_dataset_id)
    if source == "overture":
        return OvertureAdapter(settings.overture_bbox)
    if source == "wikidata":
        return WikidataAdapter(settings.overture_bbox)
    if source == "fsq":
        if not _fsq_bulk_configured(settings):
            raise typer.BadParameter(
                "FSQ_CATALOG_URI, FSQ_CATALOG_TOKEN, and FSQ_PLACES_TABLE are required"
            )
        return FoursquareAdapter(
            catalog_uri=settings.fsq_catalog_uri or "",
            catalog_token=settings.fsq_catalog_token or "",
            table_name=settings.fsq_places_table or "",
            warehouse=settings.fsq_catalog_warehouse,
            bbox=settings.overture_bbox,
        )
    raise typer.BadParameter(f"Unsupported source: {source}")


def _configured_sources(settings: Settings) -> tuple[str, ...]:
    # Open corroboration is staged independently; source failures never weaken the legal anchors.
    values = ["ca_abc", "datasf", "overture", "wikidata"]
    if _fsq_bulk_configured(settings):
        values.append("fsq")
    return tuple(values)


def _fsq_bulk_configured(settings: Settings) -> bool:
    return bool(
        settings.fsq_catalog_uri
        and settings.fsq_catalog_token
        and settings.fsq_places_table
    )


def _geocode_only(db: Database) -> dict[str, Any]:
    return {
        source: result
        for source in ("ca_abc", "datasf")
        if (result := AddressGeocoder(db).run(source))["considered"]
    }


if __name__ == "__main__":
    app()
