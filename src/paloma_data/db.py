from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
import json
from typing import Any, Iterator, Literal

import psycopg
from psycopg.rows import dict_row

from paloma_data.models import CanonicalCandidate, SourceRecord
from paloma_data.normalizers import normalize_address, normalize_name, normalize_phone, normalize_url


def execute_many(
    conn: psycopg.Connection,
    query: str,
    rows: Iterable[tuple[Any, ...]],
) -> None:
    """Run a Psycopg batch through a cursor; Connection has no executemany method."""
    with conn.cursor() as cursor:
        cursor.executemany(query, rows)


class Database:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            yield conn

    def start_run(
        self, conn: psycopg.Connection, source: str, mode: str, cursor: str | None = None
    ) -> str:
        # Source jobs hold a session advisory lock before calling this method. Any older
        # `running` row for that source therefore belongs to a worker that died before it could
        # finalize its audit record.
        conn.execute(
            """
            update ingest.ingestion_runs
            set status = 'failed',
                finished_at = now(),
                error_summary = coalesce(
                  nullif(error_summary, ''),
                  'superseded by a later run after worker interruption'
                )
            where source = %s and status = 'running'
            """,
            (source,),
        )
        row = conn.execute(
            """
            insert into ingest.ingestion_runs (source, mode, cursor_before)
            values (%s, %s, %s)
            returning id::text
            """,
            (source, mode, cursor),
        ).fetchone()
        return row["id"]

    def reset_catalog(self) -> None:
        """Delete only rebuildable catalog data; preserve auth, profiles, and vocabularies."""
        with self.connection() as conn:
            conn.execute(
                """
                truncate table
                  public.visit_experiences,
                  public.visits,
                  public.saved_establishments,
                  public.comparisons,
                  public.plan_members,
                  public.plans,
                  public.establishment_settings,
                  ingest.establishment_field_evidence,
                  ingest.establishment_review_queue,
                  ingest.establishment_sources,
                  ingest.source_records,
                  ingest.ingestion_runs,
                  public.establishments
                restart identity
                """
            )
            conn.commit()

    def finish_run(
        self,
        conn: psycopg.Connection,
        run_id: str,
        *,
        status: str,
        counters: dict[str, int],
        error: str | None = None,
    ) -> None:
        conn.execute(
            """
            update ingest.ingestion_runs
            set finished_at = now(), status = %s,
                fetched_count = %s, created_count = %s, updated_count = %s,
                unchanged_count = %s, review_count = %s, closed_count = %s,
                error_summary = %s
            where id = %s::uuid
            """,
            (
                status,
                counters.get("fetched", 0),
                counters.get("created", 0),
                counters.get("updated", 0),
                counters.get("unchanged", 0),
                counters.get("review", 0),
                counters.get("closed", 0),
                error,
                run_id,
            ),
        )

    def stage_source_record(
        self,
        conn: psycopg.Connection,
        record: SourceRecord,
        run_id: str | None = None,
    ) -> Literal["created", "updated", "unchanged"]:
        payload_hash = record.payload_hash()
        existing = conn.execute(
            """
            select payload_hash
            from ingest.source_records
            where source = %s and source_record_id = %s
            """,
            (record.source, record.source_record_id),
        ).fetchone()
        outcome: Literal["created", "updated", "unchanged"] = (
            "created"
            if existing is None
            else "updated"
            if existing["payload_hash"] != payload_hash
            else "unchanged"
        )

        conn.execute(
            """
            insert into ingest.source_records (
                source, source_record_id, source_status, source_updated_at,
                payload_hash, last_seen_at,
                name, normalized_name, address, normalized_address,
                city, region, postal_code, country_code,
                latitude, longitude, phone_e164, website_url,
                neighborhood, hours, price_level, setting_slugs,
                primary_type_slug, classification_confidence,
                source_family, consumer_facing, public_access, quality_flags,
                origin_keys, data_license, storage_scope, provider_veracity,
                last_seen_run_id, retired_at,
                category_evidence, permitted_metadata
            ) values (
                %(source)s, %(source_record_id)s, %(source_status)s, %(source_updated_at)s,
                %(payload_hash)s, now(),
                %(name)s, %(normalized_name)s, %(address)s, %(normalized_address)s,
                %(city)s, %(region)s, %(postal_code)s, %(country_code)s,
                %(latitude)s, %(longitude)s, %(phone_e164)s, %(website_url)s,
                %(neighborhood)s, %(hours)s::jsonb, %(price_level)s, %(setting_slugs)s,
                %(primary_type_slug)s, %(classification_confidence)s,
                %(source_family)s, %(consumer_facing)s, %(public_access)s, %(quality_flags)s,
                %(origin_keys)s, %(data_license)s, %(storage_scope)s, %(provider_veracity)s,
                %(last_seen_run_id)s::uuid, null,
                %(category_evidence)s::jsonb, %(permitted_metadata)s::jsonb
            )
            on conflict (source, source_record_id) do update set
                source_status = excluded.source_status,
                source_updated_at = excluded.source_updated_at,
                payload_hash = excluded.payload_hash,
                last_seen_at = now(),
                name = excluded.name,
                normalized_name = excluded.normalized_name,
                address = excluded.address,
                normalized_address = excluded.normalized_address,
                city = excluded.city,
                region = excluded.region,
                postal_code = excluded.postal_code,
                country_code = excluded.country_code,
                latitude = case
                  when excluded.latitude is not null and excluded.longitude is not null
                    then excluded.latitude
                  when source_records.normalized_address is distinct from excluded.normalized_address
                    or lower(source_records.city) is distinct from lower(excluded.city)
                    or source_records.region is distinct from excluded.region
                    or source_records.postal_code is distinct from excluded.postal_code
                    or source_records.country_code is distinct from excluded.country_code
                    then null
                  else source_records.latitude
                end,
                longitude = case
                  when excluded.latitude is not null and excluded.longitude is not null
                    then excluded.longitude
                  when source_records.normalized_address is distinct from excluded.normalized_address
                    or lower(source_records.city) is distinct from lower(excluded.city)
                    or source_records.region is distinct from excluded.region
                    or source_records.postal_code is distinct from excluded.postal_code
                    or source_records.country_code is distinct from excluded.country_code
                    then null
                  else source_records.longitude
                end,
                geocode_source = case
                  when excluded.latitude is not null and excluded.longitude is not null
                    or source_records.normalized_address is distinct from excluded.normalized_address
                    or lower(source_records.city) is distinct from lower(excluded.city)
                    or source_records.region is distinct from excluded.region
                    or source_records.postal_code is distinct from excluded.postal_code
                    or source_records.country_code is distinct from excluded.country_code
                    then null
                  else source_records.geocode_source
                end,
                geocoded_at = case
                  when excluded.latitude is not null and excluded.longitude is not null
                    or source_records.normalized_address is distinct from excluded.normalized_address
                    or lower(source_records.city) is distinct from lower(excluded.city)
                    or source_records.region is distinct from excluded.region
                    or source_records.postal_code is distinct from excluded.postal_code
                    or source_records.country_code is distinct from excluded.country_code
                    then null
                  else source_records.geocoded_at
                end,
                geocode_attempted_at = case
                  when excluded.latitude is not null and excluded.longitude is not null
                    or source_records.normalized_address is distinct from excluded.normalized_address
                    or lower(source_records.city) is distinct from lower(excluded.city)
                    or source_records.region is distinct from excluded.region
                    or source_records.postal_code is distinct from excluded.postal_code
                    or source_records.country_code is distinct from excluded.country_code
                    then null
                  else source_records.geocode_attempted_at
                end,
                geocode_matched_address = case
                  when excluded.latitude is not null and excluded.longitude is not null
                    or source_records.normalized_address is distinct from excluded.normalized_address
                    or lower(source_records.city) is distinct from lower(excluded.city)
                    or source_records.region is distinct from excluded.region
                    or source_records.postal_code is distinct from excluded.postal_code
                    or source_records.country_code is distinct from excluded.country_code
                    then null
                  else source_records.geocode_matched_address
                end,
                phone_e164 = excluded.phone_e164,
                website_url = excluded.website_url,
                neighborhood = excluded.neighborhood,
                hours = excluded.hours,
                price_level = excluded.price_level,
                setting_slugs = excluded.setting_slugs,
                primary_type_slug = excluded.primary_type_slug,
                classification_confidence = excluded.classification_confidence,
                source_family = excluded.source_family,
                consumer_facing = excluded.consumer_facing,
                public_access = excluded.public_access,
                quality_flags = excluded.quality_flags,
                origin_keys = excluded.origin_keys,
                data_license = excluded.data_license,
                storage_scope = excluded.storage_scope,
                provider_veracity = excluded.provider_veracity,
                last_seen_run_id = excluded.last_seen_run_id,
                retired_at = null,
                category_evidence = excluded.category_evidence,
                permitted_metadata = excluded.permitted_metadata,
                updated_at = now()
            """,
            {
                "source": record.source,
                "source_record_id": record.source_record_id,
                "source_status": record.source_status,
                "source_updated_at": record.source_updated_at,
                "payload_hash": payload_hash,
                "name": record.name,
                "normalized_name": normalize_name(record.name),
                "address": record.address,
                "normalized_address": normalize_address(record.address),
                "city": record.city,
                "region": record.region,
                "postal_code": record.postal_code,
                "country_code": record.country_code,
                "latitude": record.latitude,
                "longitude": record.longitude,
                "phone_e164": normalize_phone(record.phone, record.country_code),
                "website_url": normalize_url(record.website_url),
                "neighborhood": record.neighborhood,
                "hours": json.dumps(record.hours, sort_keys=True) if record.hours is not None else None,
                "price_level": record.price_level,
                "setting_slugs": sorted(set(record.setting_slugs)),
                "primary_type_slug": record.primary_type_slug,
                "classification_confidence": record.classification_confidence,
                "source_family": record.source_family,
                "consumer_facing": record.consumer_facing,
                "public_access": record.public_access,
                "quality_flags": list(record.quality_flags),
                "origin_keys": sorted(set(record.origin_keys or (record.source,))),
                "data_license": record.data_license,
                "storage_scope": record.storage_scope,
                "provider_veracity": record.provider_veracity,
                "last_seen_run_id": run_id,
                "category_evidence": json.dumps(record.category_evidence, sort_keys=True),
                "permitted_metadata": json.dumps(record.permitted_metadata, sort_keys=True),
            },
        )
        return outcome

    def complete_source_snapshot(
        self,
        conn: psycopg.Connection,
        *,
        source: str,
        run_id: str,
        record_count: int,
        release_id: str | None = None,
        cursor_after: str | None = None,
    ) -> int:
        """Retire rows absent from one fully-consumed source snapshot.

        This is intentionally called only after the adapter iterator completed successfully.
        A failed or partial download therefore cannot turn an upstream outage into mass closures.
        """
        retired = conn.execute(
            """
            update ingest.source_records
            set source_status = 'missing', retired_at = now(), updated_at = now()
            where source = %s
              and retired_at is null
              and last_seen_run_id is distinct from %s::uuid
            returning 1
            """,
            (source, run_id),
        ).fetchall()
        conn.execute(
            """
            insert into ingest.source_sync_state (
              source, last_complete_run_id, release_id, cursor_after,
              completed_at, record_count, updated_at
            ) values (%s, %s::uuid, %s, %s, now(), %s, now())
            on conflict (source) do update set
              last_complete_run_id = excluded.last_complete_run_id,
              release_id = excluded.release_id,
              cursor_after = excluded.cursor_after,
              completed_at = excluded.completed_at,
              record_count = excluded.record_count,
              updated_at = now()
            """,
            (source, run_id, release_id, cursor_after, record_count),
        )
        return len(retired)

    def linked_establishment_id(
        self, conn: psycopg.Connection, source: str, source_record_id: str
    ) -> str | None:
        row = conn.execute(
            """
            select establishment_id::text
            from ingest.establishment_sources
            where source = %s and source_record_id = %s
            """,
            (source, source_record_id),
        ).fetchone()
        return row["establishment_id"] if row else None

    def find_candidates(
        self, conn: psycopg.Connection, record: SourceRecord, limit: int = 25
    ) -> list[CanonicalCandidate]:
        name = normalize_name(record.name)
        address = normalize_address(record.address)
        geo_sql = ""
        params: list[object] = [record.city, record.country_code, name, address]
        if record.latitude is not None and record.longitude is not None:
            geo_sql = """
                or ST_DWithin(
                    location,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                    100
                )
            """
            params.extend([record.longitude, record.latitude])
        params.extend([name, address, limit])

        rows = conn.execute(
            f"""
            select id::text, name, normalized_name, address, normalized_address,
                   city, region, postal_code, trim(country_code) as country_code,
                   phone_e164, website_url, status,
                   case when location is null then null else ST_Y(location::geometry) end as latitude,
                   case when location is null then null else ST_X(location::geometry) end as longitude
            from public.establishments
            where lower(city) = lower(%s) and trim(country_code) = %s
              and (
                    coalesce(normalized_name, lower(name)) OPERATOR(extensions.%%) %s
                 or coalesce(normalized_address, lower(address)) OPERATOR(extensions.%%) %s
                 {geo_sql}
              )
            order by greatest(
                extensions.similarity(coalesce(normalized_name, lower(name)), %s),
                extensions.similarity(coalesce(normalized_address, lower(address)), %s)
            ) desc
            limit %s
            """,
            params,
        ).fetchall()
        return [CanonicalCandidate(**row) for row in rows]

    def find_source_corroboration(
        self, conn: psycopg.Connection, record: SourceRecord
    ) -> tuple[SourceRecord, float] | None:
        """Find one independent staged source that strongly supports the same real-world venue."""
        name = normalize_name(record.name)
        address = normalize_address(record.address)
        row = conn.execute(
            """
            select source, source_record_id, name, address, city, region, postal_code,
                   trim(country_code) as country_code, latitude, longitude,
                   phone_e164, website_url, source_status, source_updated_at,
                   primary_type_slug, classification_confidence,
                   source_family, consumer_facing, public_access, quality_flags,
                   origin_keys, data_license, storage_scope, provider_veracity,
                   category_evidence, permitted_metadata,
                   extensions.similarity(normalized_name, %s) as name_similarity,
                   extensions.similarity(normalized_address, %s) as address_similarity
            from ingest.source_records
            where source_family <> %s
              and lower(city) = lower(%s)
              and trim(country_code) = %s
              and normalized_address OPERATOR(extensions.%%) %s
              and normalized_name OPERATOR(extensions.%%) %s
            order by (
                0.55 * extensions.similarity(normalized_address, %s)
              + 0.45 * extensions.similarity(normalized_name, %s)
            ) desc
            limit 1
            """,
            (
                name,
                address,
                record.source_family,
                record.city,
                record.country_code,
                address,
                name,
                address,
                name,
            ),
        ).fetchone()
        if not row:
            return None

        name_similarity = float(row.pop("name_similarity"))
        address_similarity = float(row.pop("address_similarity"))
        score = (0.55 * address_similarity) + (0.45 * name_similarity)
        if not (
            (address_similarity >= 0.92 and name_similarity >= 0.70)
            or (address_similarity >= 0.85 and name_similarity >= 0.92)
        ):
            return None

        corroborating = SourceRecord(
            source=row["source"],
            source_record_id=row["source_record_id"],
            name=row["name"],
            address=row["address"],
            city=row["city"],
            region=row["region"],
            postal_code=row["postal_code"],
            country_code=row["country_code"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            phone=row["phone_e164"],
            website_url=row["website_url"],
            source_status=row["source_status"],
            source_updated_at=row["source_updated_at"],
            primary_type_slug=row["primary_type_slug"],
            classification_confidence=row["classification_confidence"],
            source_family=row["source_family"],
            consumer_facing=row["consumer_facing"],
            public_access=row["public_access"],
            quality_flags=tuple(row["quality_flags"] or ()),
            origin_keys=tuple(row["origin_keys"] or (row["source"],)),
            data_license=row["data_license"] or "unknown",
            storage_scope=row["storage_scope"] or "durable",
            provider_veracity=row["provider_veracity"],
            category_evidence=row["category_evidence"] or {},
            permitted_metadata=row["permitted_metadata"] or {},
        )
        return corroborating, score

    def upsert_source_link(
        self,
        conn: psycopg.Connection,
        establishment_id: str,
        record: SourceRecord,
        confidence: float,
        method: str,
    ) -> None:
        conn.execute(
            """
            insert into ingest.establishment_sources (
                establishment_id, source, source_record_id, source_status, source_updated_at,
                last_seen_at, last_verified_at, match_confidence, match_method,
                matching_version, payload_hash, permitted_metadata
            ) values (
                %s::uuid, %s, %s, %s, %s,
                now(), now(), %s, %s, 'v1', %s, %s::jsonb
            )
            on conflict (source, source_record_id) do update set
                establishment_id = excluded.establishment_id,
                source_status = excluded.source_status,
                source_updated_at = excluded.source_updated_at,
                last_seen_at = now(),
                last_verified_at = now(),
                match_confidence = excluded.match_confidence,
                match_method = excluded.match_method,
                matching_version = excluded.matching_version,
                payload_hash = excluded.payload_hash,
                permitted_metadata = excluded.permitted_metadata,
                updated_at = now()
            """,
            (
                establishment_id,
                record.source,
                record.source_record_id,
                record.source_status,
                record.source_updated_at,
                confidence,
                method,
                record.payload_hash(),
                json.dumps(record.permitted_metadata, sort_keys=True),
            ),
        )
        conn.execute(
            """
            update public.establishments
            set last_verified_at = now(),
                normalized_name = coalesce(normalized_name, %s),
                normalized_address = coalesce(normalized_address, %s),
                phone_e164 = coalesce(phone_e164, %s),
                website_url = coalesce(website_url, %s),
                updated_at = now()
            where id = %s::uuid
            """,
            (
                normalize_name(record.name),
                normalize_address(record.address),
                normalize_phone(record.phone, record.country_code),
                normalize_url(record.website_url),
                establishment_id,
            ),
        )
        # A queued record is waiting for an answer about where it belongs. Linking it is that
        # answer, so the queue entry is settled and must not keep asking a human for one.
        conn.execute(
            """
            update ingest.establishment_review_queue
            set state = 'superseded', resolved_at = now()
            where source = %s and source_record_id = %s and state = 'pending'
            """,
            (record.source, record.source_record_id),
        )

    def records_needing_geocode(
        self, conn: psycopg.Connection, source: str, retry_after_days: int = 30
    ) -> list[dict]:
        """Staged records with an address but no coordinates, skipping recent failed attempts."""
        return conn.execute(
            """
            select source_record_id, address, city, region, postal_code
            from ingest.source_records
            where source = %s
              and latitude is null
              and address <> ''
              and (
                geocode_attempted_at is null
                or geocode_attempted_at < now() - make_interval(days => %s)
              )
            order by source_record_id
            """,
            (source, retry_after_days),
        ).fetchall()

    def save_geocode(
        self,
        conn: psycopg.Connection,
        source: str,
        source_record_id: str,
        latitude: float,
        longitude: float,
        geocoder: str,
        matched_address: str,
    ) -> None:
        conn.execute(
            """
            update ingest.source_records
            set latitude = %s,
                longitude = %s,
                geocode_source = %s,
                geocode_matched_address = %s,
                geocoded_at = now(),
                geocode_attempted_at = now(),
                updated_at = now()
            where source = %s and source_record_id = %s and latitude is null
            """,
            (
                latitude,
                longitude,
                geocoder,
                matched_address,
                source,
                source_record_id,
            ),
        )

    def mark_geocode_attempted(
        self, conn: psycopg.Connection, source: str, source_record_ids: list[str]
    ) -> None:
        if not source_record_ids:
            return
        conn.execute(
            """
            update ingest.source_records
            set geocode_attempted_at = now(), updated_at = now()
            where source = %s and source_record_id = any(%s)
            """,
            (source, source_record_ids),
        )

    def unlinked_staged_records(self, conn: psycopg.Connection, source: str) -> list[SourceRecord]:
        """Staged records that never became part of an establishment.

        Enrichment such as geocoding changes what the pipeline can decide about a record long
        after it was first read, so these get another pass without re-fetching the source.
        """
        rows = conn.execute(
            """
            select sr.*
            from ingest.source_records sr
            left join ingest.establishment_sources es
              on es.source = sr.source and es.source_record_id = sr.source_record_id
            where sr.source = %s and es.establishment_id is null
            order by sr.source_record_id
            """,
            (source,),
        ).fetchall()
        return [
            SourceRecord(
                source=row["source"],
                source_record_id=row["source_record_id"],
                name=row["name"],
                address=row["address"],
                city=row["city"],
                region=row["region"],
                postal_code=row["postal_code"],
                country_code=(row["country_code"] or "US").strip(),
                latitude=row["latitude"],
                longitude=row["longitude"],
                phone=row["phone_e164"],
                website_url=row["website_url"],
                neighborhood=row["neighborhood"],
                hours=row["hours"],
                price_level=row["price_level"],
                setting_slugs=tuple(row["setting_slugs"] or ()),
                source_status=row["source_status"],
                source_updated_at=row["source_updated_at"],
                primary_type_slug=row["primary_type_slug"],
                classification_confidence=(
                    float(row["classification_confidence"])
                    if row["classification_confidence"] is not None
                    else None
                ),
                source_family=row["source_family"],
                consumer_facing=row["consumer_facing"],
                public_access=row["public_access"],
                quality_flags=tuple(row["quality_flags"] or ()),
                origin_keys=tuple(row["origin_keys"] or (row["source"],)),
                data_license=row["data_license"] or "unknown",
                storage_scope=row["storage_scope"] or "durable",
                provider_veracity=row["provider_veracity"],
                category_evidence=row["category_evidence"] or {},
                permitted_metadata=row["permitted_metadata"] or {},
            )
            for row in rows
        ]

    def create_establishment(
        self, conn: psycopg.Connection, record: SourceRecord, data_quality_score: float
    ) -> str:
        if not record.primary_type_slug:
            raise ValueError("primary_type_slug is required to create an establishment")
        type_row = conn.execute(
            "select id from public.primary_types where slug = %s",
            (record.primary_type_slug,),
        ).fetchone()
        if not type_row:
            raise ValueError(f"Unknown primary type: {record.primary_type_slug}")
        if record.latitude is None or record.longitude is None:
            raise ValueError("Coordinates are required for automatic canonical creation")

        row = conn.execute(
            """
            insert into public.establishments (
                name, normalized_name, primary_type_id, address, normalized_address,
                city, region, postal_code, country_code, location,
                phone_e164, website_url, status, last_verified_at, data_quality_score,
                publication_state, publication_reason, access_mode
            ) values (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s, %s, 'open', now(), %s,
                'candidate', 'awaiting_publication_evidence:v1', %s
            ) returning id::text
            """,
            (
                record.name,
                normalize_name(record.name),
                type_row["id"],
                record.address,
                normalize_address(record.address),
                record.city,
                record.region,
                record.postal_code,
                record.country_code,
                record.longitude,
                record.latitude,
                normalize_phone(record.phone, record.country_code),
                normalize_url(record.website_url),
                data_quality_score,
                record.public_access,
            ),
        ).fetchone()
        return row["id"]

    def enqueue_review(
        self,
        conn: psycopg.Connection,
        record: SourceRecord,
        reason: str,
        confidence: float | None,
        candidate_id: str | None,
        evidence: dict,
    ) -> None:
        conn.execute(
            """
            insert into ingest.establishment_review_queue (
                source, source_record_id, candidate_establishment_id,
                reason, confidence, evidence
            ) values (%s, %s, %s::uuid, %s, %s, %s::jsonb)
            on conflict (source, source_record_id, reason) where state = 'pending'
            do update set candidate_establishment_id = excluded.candidate_establishment_id,
                          confidence = excluded.confidence,
                          evidence = excluded.evidence
            """,
            (
                record.source,
                record.source_record_id,
                candidate_id,
                reason,
                confidence,
                json.dumps(evidence, sort_keys=True),
            ),
        )
        # Re-deciding a record replaces the question being asked about it rather than adding a
        # second one. The unique index is per reason, so without this a record that changes from
        # one blocker to another sits in the queue twice and is counted twice.
        conn.execute(
            """
            update ingest.establishment_review_queue
            set state = 'superseded', resolved_at = now()
            where source = %s and source_record_id = %s and state = 'pending' and reason <> %s
            """,
            (record.source, record.source_record_id, reason),
        )

    def reconcile_closure(self, conn: psycopg.Connection, establishment_id: str) -> bool:
        row = conn.execute(
            """
            select count(distinct source) filter (where source_status = 'closed') as closed_sources,
                   count(distinct source) filter (where source_status = 'open') as open_sources
            from ingest.establishment_sources
            where establishment_id = %s::uuid
            """,
            (establishment_id,),
        ).fetchone()
        if row["closed_sources"] >= 2 and row["open_sources"] == 0:
            changed = conn.execute(
                """
                update public.establishments
                set status = 'closed', closed_at = coalesce(closed_at, now()), updated_at = now()
                where id = %s::uuid and status <> 'closed'
                returning 1
                """,
                (establishment_id,),
            ).fetchone()
            return changed is not None
        return False
