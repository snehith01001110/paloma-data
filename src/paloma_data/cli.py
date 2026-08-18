from __future__ import annotations

import json
from typing import Any

import typer

from paloma_data.adapters import (
    CaliforniaABCAdapter,
    DataSFAdapter,
    FoursquareAdapter,
    OvertureAdapter,
)
from paloma_data.adapters.foursquare_api import FoursquarePlacesAPI
from paloma_data.catalog import CATALOG_DECISION_VERSION
from paloma_data.catalog_pipeline import CatalogPipeline
from paloma_data.catalog_repository import CatalogRepository
from paloma_data.config import Settings
from paloma_data.db import Database
from paloma_data.geocoding import AddressGeocoder
from paloma_data.neighborhoods import DataSFNeighborhoodAdapter, NeighborhoodStager
from paloma_data.staging import SourceStager


app = typer.Typer(no_args_is_help=True, help="Paloma accuracy-first establishment catalog")
SNAPSHOT_SOURCES = ("ca_abc", "datasf", "fsq", "overture")


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
    source: str = typer.Argument(..., help="ca_abc, datasf, fsq, or overture"),
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
    source: str = typer.Argument(..., help="ca_abc, datasf, fsq, or overture"),
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
    source: str = typer.Argument(..., help="ca_abc, datasf, fsq, or overture"),
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
    limit: int = typer.Option(20, min=1, max=50),
) -> None:
    """Run a bounded FSQ verification trial without mutating the consumer catalog."""
    settings, _, _, catalog = _components()
    if not settings.fsq_places_api_key:
        raise typer.BadParameter("FSQ_PLACES_API_KEY is required for a verification trial")
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
                "scope": {"city": city, "limit": limit},
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
) -> None:
    """Persist specifically licensed verification evidence; still does not publish."""
    settings, _, _, catalog = _components()
    if not settings.fsq_places_api_key:
        raise typer.BadParameter("FSQ_PLACES_API_KEY is required")
    if not settings.fsq_server_storage_licensed:
        raise typer.BadParameter(
            "FSQ_SERVER_STORAGE_LICENSED must be true only when a written agreement "
            "overrides the API's no-server-caching rule"
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
        )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


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
    confirm: str = typer.Option("", help="Must be exactly PUBLISH_VERIFIED"),
    limit: int = typer.Option(2_000, min=1),
) -> None:
    """Materialize only unexpired verified candidates into the consumer catalog."""
    if confirm != "PUBLISH_VERIFIED":
        raise typer.BadParameter("Pass --confirm PUBLISH_VERIFIED")
    _, _, _, catalog = _components()
    typer.echo(json.dumps(catalog.publish(limit=limit), indent=2, sort_keys=True))


@app.command("catalog-cutover")
def catalog_cutover(
    confirm: str = typer.Option("", help="Must be exactly REPLACE_PUBLIC_CATALOG"),
    minimum_verified: int = typer.Option(1, min=1),
) -> None:
    """Replace the pre-launch junk catalog with the verified v2 set."""
    if confirm != "REPLACE_PUBLIC_CATALOG":
        raise typer.BadParameter("Pass --confirm REPLACE_PUBLIC_CATALOG")
    _, _, _, catalog = _components()
    typer.echo(
        json.dumps(
            catalog.cutover(minimum_verified=minimum_verified),
            indent=2,
            sort_keys=True,
        )
    )


@app.command("catalog-sweep")
def catalog_sweep() -> None:
    """Immediately hide published rows whose verification lease expired."""
    _, db, _, _ = _components()
    repo = CatalogRepository(db)
    with db.connection() as conn:
        withdrawn = repo.withdraw_expired(conn)
        conn.commit()
    typer.echo(json.dumps({"expired_withdrawn": withdrawn}, indent=2))


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
              where candidate_state in ('verified', 'published')
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
            where candidate_state in ('verified', 'published')
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
                  and sr.normalized_address = c.normalized_address
                  and (
                    r.reason like '%%same_location_name_conflict'
                    or r.reason like '%%probable_identity_needs_review'
                  )
              ) as verified_candidates_with_exact_address_conflict
            from ingest.catalog_candidates c
            left join ingest.candidate_match_reviews r on r.candidate_id = c.id
            left join ingest.source_records sr
              on sr.source = r.source and sr.source_record_id = r.source_record_id
            where c.candidate_state in ('verified', 'published')
              and c.decision_version = %s
              and c.verification_expires_at > now()
            """,
            (CATALOG_DECISION_VERSION,),
        ).fetchone()
        exact_address_conflicts = conn.execute(
            """
            select c.id::text as candidate_id, c.name as candidate_name,
                   r.source, sr.name as conflicting_name, c.address,
                   r.reason, r.score::float
            from ingest.catalog_candidates c
            join ingest.candidate_match_reviews r on r.candidate_id = c.id
            join ingest.source_records sr
              on sr.source = r.source and sr.source_record_id = r.source_record_id
            where c.candidate_state in ('verified', 'published')
              and c.decision_version = %s
              and c.verification_expires_at > now()
              and r.state = 'pending'
              and sr.normalized_address = c.normalized_address
              and (
                r.reason like '%%same_location_name_conflict'
                or r.reason like '%%probable_identity_needs_review'
              )
            order by c.name, r.score desc, r.source
            limit 50
            """,
            (CATALOG_DECISION_VERSION,),
        ).fetchall()
        invariant_risk = conn.execute(
            """
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
              ) as automated_generic_manufacturers
            from ingest.catalog_candidates
            """,
            (CATALOG_DECISION_VERSION,),
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
                   count(*) filter (where publication_state = 'published' and neighborhood is not null) as neighborhood,
                   count(*) filter (where publication_state = 'published' and hours is not null) as hours,
                   count(*) filter (where publication_state = 'published' and price_level is not null) as price,
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
    # Overture is optional corroboration. It must never take down the ABC + FSQ truth path.
    values = ["ca_abc", "datasf"]
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
