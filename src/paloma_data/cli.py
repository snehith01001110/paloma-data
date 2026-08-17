from __future__ import annotations

import json

import typer

from paloma_data.adapters import CaliforniaABCAdapter, DataSFAdapter
from paloma_data.config import Settings
from paloma_data.db import Database
from paloma_data.pipeline import Pipeline

app = typer.Typer(no_args_is_help=True, help="Paloma establishment ingestion")


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
def backfill(source: str = typer.Argument(..., help="ca_abc or datasf")) -> None:
    """Run the initial source backfill. Safe to rerun; writes are idempotent."""
    settings, pipeline = _components()
    adapter = _adapter(source, settings)
    counters = pipeline.run(adapter.source, "full", adapter.backfill())
    typer.echo(json.dumps(counters, indent=2, sort_keys=True))


@app.command()
def sync(source: str = typer.Argument(..., help="ca_abc or datasf")) -> None:
    """Run the ongoing incremental/reconciliation path."""
    settings, pipeline = _components()
    adapter = _adapter(source, settings)
    counters = pipeline.run(adapter.source, "incremental", adapter.incremental())
    typer.echo(json.dumps(counters, indent=2, sort_keys=True))


@app.command("sync-all")
def sync_all() -> None:
    """Run all currently production-enabled incremental sources."""
    settings, pipeline = _components()
    results = {}
    for source in ("ca_abc", "datasf"):
        adapter = _adapter(source, settings)
        results[source] = pipeline.run(adapter.source, "incremental", adapter.incremental())
    typer.echo(json.dumps(results, indent=2, sort_keys=True))


def _adapter(source: str, settings: Settings):
    if source == "ca_abc":
        return CaliforniaABCAdapter(settings.abc_reports_url)
    if source == "datasf":
        return DataSFAdapter(settings.datasf_dataset_id)
    raise typer.BadParameter(f"Unsupported source: {source}")


if __name__ == "__main__":
    app()
