from __future__ import annotations

import json

import typer

from paloma_data.adapters import (
    CaliforniaABCAdapter,
    DataSFAdapter,
    FoursquareAdapter,
    OvertureAdapter,
)
from paloma_data.config import Settings
from paloma_data.db import Database
from paloma_data.field_resolution import FieldResolver, RESOLUTION_VERSION
from paloma_data.geocoding import AddressGeocoder
from paloma_data.pipeline import Pipeline
from paloma_data.publication import PublicationResolver
from paloma_data.web_identity import OfficialWebEnricher

app = typer.Typer(no_args_is_help=True, help="Paloma establishment ingestion")
CORE_SOURCES = ("ca_abc", "datasf", "overture")
# Keep bootstrap version-aware so resolver upgrades force exactly one fresh catalog rebuild.


def _components() -> tuple[Settings, Pipeline]:
    settings = Settings.from_env()
    pipeline = Pipeline(
        Database(settings.database_url),
        allowed_cities=settings.allowed_cities,
        allowed_regions=settings.allowed_regions,
        allowed_countries=settings.allowed_countries,
    )
    return settings, pipeline


def _geocode_and_reconcile(pipeline: Pipeline) -> dict[str, object]:
    """Resolve addresses the sources left unplaced, then act on what that unlocked.

    A record with no coordinates cannot become an establishment, so geocoding is the step that
    lets an authoritative but unplaced source contribute a venue instead of a review item.
    """
    results: dict[str, object] = {}
    for source in CORE_SOURCES:
        geocoded = AddressGeocoder(pipeline.db).run(source)
        if not geocoded["considered"]:
            continue
        results[source] = {"geocoded": geocoded}
        if geocoded["matched"]:
            results[source]["reconciled"] = pipeline.reconcile_staged(source)
    return results


@app.command()
def geocode(
    source: str = typer.Argument("", help="One source, or empty for every source"),
) -> None:
    """Geocode staged records missing coordinates, then re-decide the ones that were waiting."""
    _, pipeline = _components()
    if source:
        geocoded = AddressGeocoder(pipeline.db).run(source)
        results: dict[str, object] = {source: {"geocoded": geocoded}}
        if geocoded["matched"]:
            results[source]["reconciled"] = pipeline.reconcile_staged(source)
    else:
        results = _geocode_and_reconcile(pipeline)
    results.update(_resolve_catalog(pipeline))
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command()
def bootstrap() -> None:
    """Backfill missing sources and force one full rebuild when the resolver schema advances."""
    settings, pipeline = _components()
    results: dict[str, object] = {}

    # A new resolver version changes the semantics of canonical fields, not just presentation.
    # Re-read every upstream source once so stale staged records cannot survive a resolver upgrade.
    force_rebuild = not _field_resolution_current(pipeline)
    results["forced_rebuild"] = force_rebuild
    results["resolution_version"] = RESOLUTION_VERSION

    for source in _configured_sources(settings):
        if not force_rebuild and _successful_backfill_exists(pipeline, source):
            results[source] = {"skipped": "already_backfilled"}
            continue
        adapter = _adapter(source, settings)
        results[source] = pipeline.run(adapter.source, "full", adapter.backfill())

    results["geocode"] = _geocode_and_reconcile(pipeline)
    results.update(_resolve_catalog(pipeline))
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command()
def backfill(source: str = typer.Argument(..., help="ca_abc, datasf, overture, or fsq")) -> None:
    """Run one source backfill, then recompute field-level provenance/confidence."""
    settings, pipeline = _components()
    adapter = _adapter(source, settings)
    result = {source: pipeline.run(adapter.source, "full", adapter.backfill())}
    result.update(_resolve_catalog(pipeline))
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("rebuild-catalog")
def rebuild_catalog() -> None:
    """Re-read configured bulk sources, then resolve fields and publication state."""
    settings, pipeline = _components()
    results: dict[str, object] = {}
    for source in _configured_sources(settings):
        adapter = _adapter(source, settings)
        results[source] = pipeline.run(adapter.source, "full", adapter.backfill())
    results["geocode"] = _geocode_and_reconcile(pipeline)
    results.update(_resolve_catalog(pipeline))
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command()
def sync(source: str = typer.Argument(..., help="ca_abc, datasf, overture, or fsq")) -> None:
    """Run one incremental source and recompute canonical field confidence."""
    settings, pipeline = _components()
    adapter = _adapter(source, settings)
    result = {source: pipeline.run(adapter.source, "incremental", adapter.incremental())}
    result.update(_resolve_catalog(pipeline))
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("sync-government")
def sync_government() -> None:
    """Run high-frequency government reconciliation, retaining names as legal evidence."""
    settings, pipeline = _components()
    results: dict[str, object] = {}
    for source in ("ca_abc", "datasf"):
        adapter = _adapter(source, settings)
        results[source] = pipeline.run(adapter.source, "incremental", adapter.incremental())
    results["geocode"] = _geocode_and_reconcile(pipeline)
    results.update(_resolve_catalog(pipeline))
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command("sync-all")
def sync_all() -> None:
    """Run configured bulk sources, then resolve fields and publication state."""
    settings, pipeline = _components()
    results: dict[str, object] = {}
    for source in _configured_sources(settings):
        adapter = _adapter(source, settings)
        results[source] = pipeline.run(adapter.source, "incremental", adapter.incremental())
    results["geocode"] = _geocode_and_reconcile(pipeline)
    results.update(_resolve_catalog(pipeline))
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command("enrich-web")
def enrich_web() -> None:
    """Optionally inspect first-party sites; this is not part of scheduled catalog ingestion."""
    _, pipeline = _components()
    result = {"official_web": OfficialWebEnricher(pipeline.db).run()}
    result.update(_resolve_catalog(pipeline))
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("resolve-fields")
def resolve_fields() -> None:
    """Rebuild field evidence, then recompute publication without fetching sources."""
    _, pipeline = _components()
    typer.echo(json.dumps(_resolve_catalog(pipeline), indent=2, sort_keys=True))


@app.command("resolve-publication")
def resolve_publication() -> None:
    """Recompute only the consumer-catalog publication gate."""
    _, pipeline = _components()
    typer.echo(json.dumps(PublicationResolver(pipeline.db).resolve(), indent=2, sort_keys=True))


def _field_resolution_current(pipeline: Pipeline) -> bool:
    """True only when every ingestion-backed canonical row has the current resolver version."""
    with pipeline.db.connection() as conn:
        row = conn.execute(
            """
            select
              count(*) filter (where exists (
                select 1 from ingest.establishment_sources es where es.establishment_id = e.id
              )) as ingestion_backed,
              count(*) filter (
                where exists (
                  select 1 from ingest.establishment_sources es where es.establishment_id = e.id
                )
                and e.field_resolution_version = %s
              ) as current_rows
            from public.establishments e
            """,
            (RESOLUTION_VERSION,),
        ).fetchone()
    ingestion_backed = int(row["ingestion_backed"] or 0)
    current_rows = int(row["current_rows"] or 0)
    return ingestion_backed > 0 and ingestion_backed == current_rows


def _successful_backfill_exists(pipeline: Pipeline, source: str) -> bool:
    with pipeline.db.connection() as conn:
        row = conn.execute(
            """
            select exists (
              select 1
              from ingest.ingestion_runs
              where source = %s
                and mode = 'full'
                and status = 'succeeded'
                and fetched_count > 0
            ) as complete
            """,
            (source,),
        ).fetchone()
    return bool(row["complete"])


def _adapter(source: str, settings: Settings):
    if source == "ca_abc":
        return CaliforniaABCAdapter(settings.abc_reports_url)
    if source == "datasf":
        return DataSFAdapter(settings.datasf_dataset_id)
    if source == "overture":
        return OvertureAdapter(settings.overture_bbox)
    if source == "fsq":
        if not _fsq_configured(settings):
            raise typer.BadParameter(
                "FSQ_CATALOG_URI, FSQ_CATALOG_TOKEN, and FSQ_PLACES_TABLE are required for fsq"
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
    return (*CORE_SOURCES, "fsq") if _fsq_configured(settings) else CORE_SOURCES


def _fsq_configured(settings: Settings) -> bool:
    return bool(settings.fsq_catalog_uri and settings.fsq_catalog_token and settings.fsq_places_table)


def _resolve_catalog(pipeline: Pipeline) -> dict[str, object]:
    return {
        "field_resolution": FieldResolver(pipeline.db).refresh_and_resolve(),
        "publication": PublicationResolver(pipeline.db).resolve(),
    }


if __name__ == "__main__":
    app()
