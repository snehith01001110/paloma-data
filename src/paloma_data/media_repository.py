from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable
from uuid import UUID

from paloma_data.db import Database
from paloma_data.media_discovery import EstablishmentMediaTarget, OpenMediaCandidate


MEDIA_REVIEW_VERDICTS = frozenset(
    {"exact_storefront", "exact_building", "site_context", "not_venue", "unusable"}
)


@dataclass(frozen=True, slots=True)
class DiscoveryPersistenceResult:
    discovered: int
    inserted: int
    already_known: int


@dataclass(frozen=True, slots=True)
class MediaVariantRegistration:
    variant: str
    bucket_id: str
    object_path: str
    public_url: str
    mime_type: str
    width: int
    height: int
    byte_size: int
    sha256: str


class EstablishmentMediaRepository:
    """Database boundary for the provider-neutral establishment media workflow."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def targets(
        self,
        *,
        cities: Iterable[str] = (),
        missing_cover_only: bool = True,
        limit: int = 100,
    ) -> list[EstablishmentMediaTarget]:
        normalized_cities = sorted({city.strip() for city in cities if city.strip()})
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                select id::text as establishment_id, name, address, city,
                       st_y(location::geometry)::float as latitude,
                       st_x(location::geometry)::float as longitude
                from public.establishments
                where publication_state = 'published'
                  and (%s::text[] = '{}'::text[] or city = any(%s::text[]))
                  and (not %s or cover_image_url is null)
                order by city, name, id
                limit %s
                """,
                (normalized_cities, normalized_cities, missing_cover_only, limit),
            ).fetchall()
        return [EstablishmentMediaTarget(**dict(row)) for row in rows]

    def persist_candidates(
        self,
        target: EstablishmentMediaTarget,
        candidates: Iterable[OpenMediaCandidate],
    ) -> DiscoveryPersistenceResult:
        candidate_list = list(candidates)
        inserted = 0
        with self.db.connection() as conn:
            policy_ids = {
                row["source"]: row["source_policy_id"]
                for row in conn.execute(
                    """
                    select source, source_policy_id
                    from governance.current_source_field_policies
                    where field_name = 'cover_image_url'
                    """
                ).fetchall()
            }
            for candidate in candidate_list:
                policy_id = policy_ids.get(candidate.provider)
                if policy_id is None:
                    raise ValueError(
                        f"No current cover-image source policy for {candidate.provider}"
                    )
                metadata = {
                    "review_priority": candidate.review_priority,
                    "review_flags": list(candidate.review_flags),
                    "source_width": candidate.width,
                    "source_height": candidate.height,
                }
                row = conn.execute(
                    """
                    insert into catalog.establishment_media_sources (
                      establishment_id, source_policy_id, provider, source_asset_id,
                      source_page_url, creator, captured_at, latitude, longitude,
                      distance_meters, camera_heading_degrees, bearing_to_target_degrees,
                      heading_delta_degrees, license_id, license_url, terms_url,
                      commercial_use_allowed, derivatives_allowed, raw_persistence_allowed,
                      attribution_required, share_alike_required, attribution_text, metadata
                    ) values (
                      %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                    )
                    on conflict (establishment_id, provider, source_asset_id) do nothing
                    returning id
                    """,
                    (
                        target.establishment_id,
                        policy_id,
                        candidate.provider,
                        candidate.source_asset_id,
                        candidate.source_page_url,
                        candidate.creator,
                        candidate.captured_at,
                        candidate.latitude,
                        candidate.longitude,
                        candidate.distance_meters,
                        candidate.camera_heading_degrees,
                        candidate.bearing_to_target_degrees,
                        candidate.heading_delta_degrees,
                        candidate.rights.license_id,
                        candidate.rights.license_url,
                        candidate.rights.terms_url,
                        candidate.rights.commercial_use_allowed,
                        candidate.rights.derivatives_allowed,
                        candidate.rights.raw_persistence_allowed,
                        candidate.rights.attribution_required,
                        candidate.rights.share_alike_required,
                        candidate.attribution_text,
                        json.dumps(metadata, sort_keys=True),
                    ),
                ).fetchone()
                inserted += int(row is not None)
        return DiscoveryPersistenceResult(
            discovered=len(candidate_list),
            inserted=inserted,
            already_known=len(candidate_list) - inserted,
        )

    def record_source_review(
        self,
        source_id: str,
        *,
        verdict: str,
        reviewed_by: str,
        notes: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if verdict not in MEDIA_REVIEW_VERDICTS:
            raise ValueError(f"Unsupported media review verdict: {verdict}")
        if not reviewed_by.strip() or not notes.strip():
            raise ValueError("reviewed_by and notes are required")
        with self.db.connection() as conn:
            previous = conn.execute(
                """
                select id
                from review.establishment_media_source_reviews
                where source_id = %s::uuid
                order by reviewed_at desc, id desc
                limit 1
                """,
                (source_id,),
            ).fetchone()
            row = conn.execute(
                """
                insert into review.establishment_media_source_reviews (
                  source_id, verdict, reviewed_by, notes, supersedes_review_id, metadata
                )
                select %s::uuid, %s, %s, %s, %s, %s::jsonb
                where exists (
                  select 1 from catalog.establishment_media_sources where id = %s::uuid
                )
                returning id
                """,
                (
                    source_id,
                    verdict,
                    reviewed_by.strip(),
                    notes.strip(),
                    previous["id"] if previous else None,
                    json.dumps(metadata or {}, sort_keys=True),
                    source_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown media source: {source_id}")
        return int(row["id"])

    def register_source_file(
        self,
        source_id: str,
        *,
        bucket_id: str,
        object_path: str,
        mime_type: str,
        width: int,
        height: int,
        byte_size: int,
        sha256: str,
    ) -> None:
        with self.db.connection() as conn:
            row = conn.execute(
                """
                insert into catalog.establishment_media_source_files (
                  source_id, bucket_id, object_path, mime_type,
                  width, height, byte_size, sha256
                )
                select id, %s, %s, %s, %s, %s, %s, %s
                from catalog.establishment_media_sources
                where id = %s::uuid
                on conflict (source_id, sha256) do nothing
                returning id
                """,
                (
                    bucket_id,
                    object_path,
                    mime_type,
                    width,
                    height,
                    byte_size,
                    sha256,
                    source_id,
                ),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    """
                    select 1 from catalog.establishment_media_source_files
                    where source_id = %s::uuid and sha256 = %s
                    """,
                    (source_id, sha256),
                ).fetchone()
                if existing is None:
                    raise ValueError(f"Unknown media source: {source_id}")

    def work_queue(self, *, cities: Iterable[str] = ()) -> list[dict[str, Any]]:
        normalized_cities = sorted({city.strip() for city in cities if city.strip()})
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                select *
                from catalog.establishment_media_work_queue
                where %s::text[] = '{}'::text[] or city = any(%s::text[])
                order by
                  case next_action
                    when 'ready_to_publish' then 1
                    when 'generation_or_quality_review' then 2
                    when 'ready_for_generation' then 3
                    when 'needs_identity_review' then 4
                    when 'needs_source_discovery' then 5
                    else 6
                  end,
                  city, name, establishment_id
                """,
                (normalized_cities, normalized_cities),
            ).fetchall()
        return [dict(row) for row in rows]

    def register_rendered_asset(
        self,
        *,
        asset_id: UUID,
        establishment_id: str,
        source_id: str | None,
        asset_kind: str,
        generator: str,
        generator_version: str,
        prompt_sha256: str,
        input_sha256: str | None,
        attribution_text: str | None,
        disclosure_text: str,
        output_license_id: str,
        output_license_url: str | None,
        variants: Iterable[MediaVariantRegistration],
        metadata: dict[str, Any] | None = None,
    ) -> str:
        variant_list = list(variants)
        if {item.variant for item in variant_list} != {"hero", "card", "thumbnail"}:
            raise ValueError("Exactly one hero, card, and thumbnail variant is required")
        with self.db.connection() as conn:
            row = conn.execute(
                """
                insert into catalog.establishment_media_assets (
                  id, establishment_id, source_id, role, asset_kind, state,
                  generator, generator_version, prompt_sha256, input_sha256,
                  attribution_text, disclosure_text, output_license_id,
                  output_license_url, metadata
                )
                select %s::uuid, establishment.id, source.id, 'cover', %s, 'rendered',
                       %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                from public.establishments establishment
                left join catalog.establishment_media_sources source
                  on source.id = %s::uuid
                 and source.establishment_id = establishment.id
                where establishment.id = %s::uuid
                  and establishment.publication_state = 'published'
                  and (
                    (%s::uuid is null and %s = 'category_illustration')
                    or (%s::uuid is not null and source.id is not null)
                  )
                returning id::text
                """,
                (
                    str(asset_id),
                    asset_kind,
                    generator,
                    generator_version,
                    prompt_sha256,
                    input_sha256,
                    attribution_text,
                    disclosure_text,
                    output_license_id,
                    output_license_url,
                    json.dumps(metadata or {}, sort_keys=True),
                    source_id,
                    establishment_id,
                    source_id,
                    asset_kind,
                    source_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError(
                    "Unknown/unpublished establishment, mismatched source, or invalid source-less kind"
                )
            for variant in variant_list:
                conn.execute(
                    """
                    insert into catalog.establishment_media_variants (
                      asset_id, variant, bucket_id, object_path, public_url,
                      mime_type, width, height, byte_size, sha256
                    ) values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(asset_id),
                        variant.variant,
                        variant.bucket_id,
                        variant.object_path,
                        variant.public_url,
                        variant.mime_type,
                        variant.width,
                        variant.height,
                        variant.byte_size,
                        variant.sha256,
                    ),
                )
        return str(asset_id)

    def approve_asset(self, asset_id: str, *, reviewed_by: str, notes: str) -> None:
        if not reviewed_by.strip() or not notes.strip():
            raise ValueError("reviewed_by and notes are required")
        with self.db.connection() as conn:
            row = conn.execute(
                """
                select catalog.approve_establishment_media_asset(
                  %s::uuid, %s, %s
                )::text as id
                """,
                (asset_id, reviewed_by.strip(), notes.strip()),
            ).fetchone()
            if row is None:
                raise ValueError("Asset must exist and be rendered before quality approval")

    def publish_asset(self, asset_id: str) -> str:
        with self.db.connection() as conn:
            row = conn.execute(
                "select catalog.publish_establishment_cover_media(%s::uuid)::text as id",
                (asset_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unable to publish media asset: {asset_id}")
        return str(row["id"])
