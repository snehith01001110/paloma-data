from __future__ import annotations

import json

import typer

from paloma_data.adapters import CaliforniaABCAdapter, DataSFAdapter, OvertureAdapter
from paloma_data.config import Settings
from paloma_data.db import Database
from paloma_data.field_resolution import FieldResolver, RESOLUTION_VERSION
from paloma_data.pipeline import Pipeline
from paloma_data.web_identity import OfficialWebEnricher

app = typer.Typer(no_args_is_help=True, help="Paloma establishment ingestion")
SOURCES = ("ca_abc", "datasf", "overture")


def _components() -> tuple[Settings, Pipeline]:
    settings = Settings.from_env()
    pipeline = Pipeline(
        Database(settings.database_url),
        allowed_cities=settings.allowed_cities,
        allowed_regions=settings.allowed_regions,
        allowed_countries=settings.allowed_countries,
    )
    return settings, pipeline


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

    for source in SOURCES:
        if not force_rebuild and _successful_backfill_exists(pipeline, source):
            results[source] = {"skipped": "already_backfilled"}
            continue
        adapter = _adapter(source, settings)
        results[source] = pipeline.run(adapter.source, "full", adapter.backfill())

    results["field_resolution_before_web"] = FieldResolver(pipeline.db).refresh_and_resolve()
    results["official_web"] = OfficialWebEnricher(pipeline.db).run()
    results["field_resolution"] = FieldResolver(pipeline.db).refresh_and_resolve()
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command()
def backfill(source: str = typer.Argument(..., help="ca_abc, datasf, or overture")) -> None:
    """Run one source backfill, then recompute field-level provenance/confidence."""
    settings, pipeline = _components()
    adapter = _adapter(source, settings)
    result = {
        source: pipeline.run(adapter.source, "full", adapter.backfill()),
        "field_resolution": FieldResolver(pipeline.db).refresh_and_resolve(),
    }
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("rebuild-catalog")
def rebuild_catalog() -> None:
    """Re-read all primary sources, verify first-party web identities, and resolve canonical fields."""
    settings, pipeline = _components()
    results: dict[str, object] = {}
    for source in SOURCES:
        adapter = _adapter(source, settings)
        results[source] = pipeline.run(adapter.source, "full", adapter.backfill())
    results["field_resolution_before_web"] = FieldResolver(pipeline.db).refresh_and_resolve()
    results["official_web"] = OfficialWebEnricher(pipeline.db).run()
    results["field_resolution"] = FieldResolver(pipeline.db).refresh_and_resolve()
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command()
def sync(source: str = typer.Argument(..., help="ca_abc, datasf, or overture")) -> None:
    """Run one incremental source and recompute canonical field confidence."""
    settings, pipeline = _components()
    adapter = _adapter(source, settings)
    result = {
        source: pipeline.run(adapter.source, "incremental", adapter.incremental()),
        "field_resolution": FieldResolver(pipeline.db).refresh_and_resolve(),
    }
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("sync-government")
def sync_government() -> None:
    """Run high-frequency government reconciliation, retaining names as legal evidence."""
    settings, pipeline = _components()
    results: dict[str, object] = {}
    for source in ("ca_abc", "datasf"):
        adapter = _adapter(source, settings)
        results[source] = pipeline.run(adapter.source, "incremental", adapter.incremental())
    results["field_resolution"] = FieldResolver(pipeline.db).refresh_and_resolve()
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command("sync-all")
def sync_all() -> None:
    """Run all sources, first-party identity verification, and field resolution."""
    settings, pipeline = _components()
    results: dict[str, object] = {}
    for source in SOURCES:
        adapter = _adapter(source, settings)
        results[source] = pipeline.run(adapter.source, "incremental", adapter.incremental())
    results["field_resolution_before_web"] = FieldResolver(pipeline.db).refresh_and_resolve()
    results["official_web"] = OfficialWebEnricher(pipeline.db).run()
    results["field_resolution"] = FieldResolver(pipeline.db).refresh_and_resolve()
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


@app.command("enrich-web")
def enrich_web() -> None:
    """Refresh verified first-party public-facing names and resolve the catalog."""
    _, pipeline = _components()
    result = {
        "official_web": OfficialWebEnricher(pipeline.db).run(),
        "field_resolution": FieldResolver(pipeline.db).refresh_and_resolve(),
    }
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("resolve-fields")
def resolve_fields() -> None:
    """Rebuild source field evidence and resolve canonical confidence without fetching sources."""
    _, pipeline = _components()
    typer.echo(json.dumps(FieldResolver(pipeline.db).refresh_and_resolve(), indent=2, sort_keys=True))


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
    raise typer.BadParameter(f"Unsupported source: {source}")


if __name__ == "__main__":
    app()
