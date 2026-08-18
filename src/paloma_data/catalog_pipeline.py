from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from paloma_data.adapters.foursquare_api import FoursquarePlacesAPI
from paloma_data.catalog import (
    CATALOG_DECISION_VERSION,
    CatalogDecision,
    IdentityDecision,
    LinkedSource,
    VerificationEvidence,
    decide_candidate,
    decide_identity,
    provider_verification,
)
from paloma_data.catalog_repository import CatalogRepository
from paloma_data.db import Database
from paloma_data.models import SourceRecord
from paloma_data.taxonomy import BAR_TYPES


class CatalogPipeline:
    """Private candidate construction, verification, and explicit promotion."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.repo = CatalogRepository(db)

    def discover(
        self,
        *,
        city: str | None,
        limit: int,
        evaluation_mode: str = "trial",
        anchor_sources: tuple[str, ...] = ("fsq",),
    ) -> dict[str, Any]:
        counters: dict[str, Any] = {
            "anchors_considered": 0,
            "candidates_created": 0,
            "anchors_linked": 0,
            "sources_linked": 0,
            "match_reviews": 0,
            "decisions": defaultdict(int),
            "candidate_ids": [],
        }
        with self.db.connection() as conn:
            anchors = self.repo.discoverable_anchors(
                conn,
                city=city,
                limit=limit,
                anchor_sources=anchor_sources,
            )
            for anchor in anchors:
                counters["anchors_considered"] += 1
                candidate_id, created = self._candidate_for_anchor(conn, anchor)
                counters["candidate_ids"].append(candidate_id)
                counters["candidates_created"] += int(created)
                self.repo.link_source(
                    conn,
                    candidate_id,
                    anchor,
                    confidence=1.0,
                    method="anchor_source_id",
                )
                counters["anchors_linked"] += 1
                self.repo.promote_anchor_if_better(conn, candidate_id, anchor)
                linked, reviews = self._correlate(conn, candidate_id, anchor)
                counters["sources_linked"] += linked
                counters["match_reviews"] += reviews

                links = self._validated_links(conn, candidate_id, anchor)
                decision = self._decide_candidate(
                    conn,
                    candidate_id,
                    links,
                    self.repo.verifications(conn, candidate_id),
                    mode="production",
                )
                self.repo.save_evaluation(
                    conn,
                    candidate_id,
                    decision,
                    mode=evaluation_mode,
                )
                counters["decisions"][decision.state] += 1
                if counters["anchors_considered"] % 100 == 0:
                    conn.commit()
            conn.commit()
        counters["decisions"] = dict(counters["decisions"])
        return counters

    def verify_with_foursquare(
        self,
        api: FoursquarePlacesAPI,
        *,
        city: str | None,
        limit: int,
        mode: str,
        lease_days: int,
        candidate_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if mode not in {"trial", "production"}:
            raise ValueError("verification mode must be trial or production")
        if mode == "production" and api.storage_policy != "contract":
            raise RuntimeError(
                "Production verification requires an explicit Foursquare storage contract"
            )

        results: list[dict[str, Any]] = []
        counters = {
            "considered": 0,
            "api_calls": 0,
            "missing_fsq_anchor": 0,
            "api_not_found": 0,
            "passed": 0,
            "failed": 0,
            "decisions": defaultdict(int),
        }
        with self.db.connection() as conn:
            ids = candidate_ids or self.repo.verification_candidate_ids(
                conn,
                city=city,
                limit=limit,
            )
        ids = list(dict.fromkeys(ids))[:limit]

        for candidate_id in ids:
            counters["considered"] += 1
            # Do not keep a database transaction open during a metered network request.
            with self.db.connection() as conn:
                anchor = self.repo.fsq_anchor(conn, candidate_id)
            if anchor is None:
                counters["missing_fsq_anchor"] += 1
                continue

            counters["api_calls"] += 1
            details = api.details(anchor.source_record_id)
            if details is None:
                counters["api_not_found"] += 1
                verification = _not_found_verification(
                    anchor.source_record_id,
                    api.storage_policy,
                    lease_days,
                )
            else:
                verification = provider_verification(
                    details,
                    candidate_anchor=anchor,
                    storage_policy=api.storage_policy,
                    lease_days=lease_days,
                )

            counters["passed" if verification.outcome == "pass" else "failed"] += 1
            with self.db.connection() as conn:
                conn.execute(
                    "select pg_advisory_xact_lock(hashtext('paloma_candidate:' || %s))",
                    (candidate_id,),
                )
                current_anchor = self.repo.fsq_anchor(conn, candidate_id)
                if (
                    current_anchor is None
                    or current_anchor.source_record_id != anchor.source_record_id
                ):
                    counters["missing_fsq_anchor"] += 1
                    conn.rollback()
                    continue

                self._correlate(conn, candidate_id, current_anchor)
                links = self._validated_links(conn, candidate_id, current_anchor)
                if api.storage_policy == "contract":
                    if details is not None:
                        self.db.stage_source_record(conn, details)
                        identity = decide_identity(current_anchor, details)
                        if identity.action == "match":
                            self.repo.link_source(
                                conn,
                                candidate_id,
                                details,
                                confidence=identity.score,
                                method=f"provider_verification:{identity.reason}",
                                metadata={"features": identity.features},
                            )
                            links = self._validated_links(
                                conn, candidate_id, current_anchor
                            )
                    # A real 404 is negative current evidence and must supersede an older pass.
                    self.repo.save_verification(conn, candidate_id, verification)

                existing = self.repo.verifications(conn, candidate_id)
                decision = self._decide_candidate(
                    conn,
                    candidate_id,
                    links,
                    existing
                    if api.storage_policy == "contract"
                    else [verification, *existing],
                    mode=mode,
                )
                self.repo.save_evaluation(
                    conn,
                    candidate_id,
                    decision,
                    mode=mode,
                    mutate_candidate=mode == "production",
                    persist_snapshot=mode == "production",
                )
                conn.commit()

            counters["decisions"][decision.state] += 1
            results.append(
                {
                    "candidate_id": candidate_id,
                    "anchor_name": anchor.name,
                    "verification": verification.outcome,
                    "checks": verification.checks,
                    "attribute_availability": {
                        key.removeprefix("has_"): value
                        for key, value in verification.checks.items()
                        if key.startswith("has_")
                    },
                    "decision": decision.state,
                    "reason": decision.reason,
                    "field_coverage": _field_coverage(decision),
                }
            )
        counters["decisions"] = dict(counters["decisions"])
        return {**counters, "results": results}

    def reevaluate(
        self,
        *,
        city: str | None,
        limit: int,
    ) -> dict[str, int]:
        counters: defaultdict[str, int] = defaultdict(int)
        with self.db.connection() as conn:
            ids = self.repo.candidate_ids(conn, city=city, limit=limit)
            for index, candidate_id in enumerate(ids, start=1):
                decision = self._evaluate_candidate(conn, candidate_id)
                counters[decision.state] += 1
                if index % 100 == 0:
                    conn.commit()
            conn.commit()
        return dict(counters)

    def publish(self, *, limit: int) -> dict[str, int]:
        counters = {"considered": 0, "published": 0, "skipped": 0, "expired_withdrawn": 0}
        with self.db.connection() as conn:
            counters["expired_withdrawn"] = self.repo.withdraw_expired(conn)
            ids = self.repo.candidate_ids(
                conn,
                limit=limit,
                states=("verified",),
                decision_version=CATALOG_DECISION_VERSION,
            )
            for candidate_id in ids:
                counters["considered"] += 1
                # Never trust a stored state at the write boundary. Source facts can change
                # after evaluation, and rule changes deliberately invalidate old versions.
                decision = self._evaluate_candidate(conn, candidate_id)
                if decision.state == "verified" and self.repo.materialize(conn, candidate_id):
                    counters["published"] += 1
                else:
                    counters["skipped"] += 1
            conn.commit()
        return counters

    def cutover(self, *, minimum_verified: int = 1) -> dict[str, int]:
        with self.db.connection() as conn:
            conn.execute("select pg_advisory_xact_lock(hashtext('paloma_catalog_cutover'))")
            # Recheck every row that could enter the replacement set before counting or deleting
            # anything. A stale historical `verified` state is not a publication authorization.
            recheck_ids = self.repo.candidate_ids(
                conn,
                limit=50_000,
                states=("verified", "published"),
            )
            for candidate_id in recheck_ids:
                self._evaluate_candidate(conn, candidate_id)
            row = conn.execute(
                """
                select count(*) as count
                from ingest.catalog_candidates
                where candidate_state in ('verified', 'published')
                  and decision_version = %s
                  and verification_expires_at > now()
                """,
                (CATALOG_DECISION_VERSION,),
            ).fetchone()
            verified = int(row["count"] or 0)
            if verified < minimum_verified:
                raise RuntimeError(
                    f"Cutover refused: {verified} verified candidates; "
                    f"minimum is {minimum_verified}"
                )
            self.repo.reset_public_catalog(conn)
            ids = self.repo.candidate_ids(
                conn,
                limit=max(verified, minimum_verified),
                states=("verified",),
                decision_version=CATALOG_DECISION_VERSION,
            )
            published = sum(self.repo.materialize(conn, candidate_id) for candidate_id in ids)
            if published < minimum_verified:
                raise RuntimeError(
                    f"Cutover rolled back: only {published} non-duplicate candidates "
                    f"materialized; minimum is {minimum_verified}"
                )
            conn.commit()
        return {
            "verified_before_cutover": verified,
            "considered": len(ids),
            "published": published,
            "skipped": len(ids) - published,
            "expired_withdrawn": 0,
        }

    def _evaluate_candidate(
        self, conn, candidate_id: str
    ) -> CatalogDecision:
        anchor = self.repo.fsq_anchor(conn, candidate_id)
        if anchor is not None:
            self.repo.refresh_candidate_anchor(conn, candidate_id, anchor)
            self._correlate(conn, candidate_id, anchor)
            links = self._validated_links(conn, candidate_id, anchor)
        else:
            links = self.repo.linked_sources(conn, candidate_id)
        decision = self._decide_candidate(
            conn,
            candidate_id,
            links,
            self.repo.verifications(conn, candidate_id),
            mode="production",
        )
        self.repo.save_evaluation(conn, candidate_id, decision, mode="production")
        return decision

    def _decide_candidate(
        self,
        conn,
        candidate_id: str,
        links: list[LinkedSource],
        verifications: list[VerificationEvidence],
        *,
        mode: str,
    ) -> CatalogDecision:
        decision = decide_candidate(links, verifications, mode=mode)
        if (
            decision.state == "verified"
            and self.repo.has_blocking_match_review(conn, candidate_id)
        ):
            return CatalogDecision(
                state="needs_review",
                reason=(
                    "unresolved_exact_address_identity_conflict:"
                    f"{CATALOG_DECISION_VERSION}"
                ),
                reasons=("unresolved_exact_address_identity_conflict",),
                identity_confidence=decision.identity_confidence,
            )
        return decision

    def _candidate_for_anchor(
        self, conn, anchor: SourceRecord
    ) -> tuple[str, bool]:
        scored: list[tuple[float, str, SourceRecord, IdentityDecision]] = []
        for candidate_id, candidate_anchor in self.repo.candidate_matches(conn, anchor):
            decision = decide_identity(anchor, candidate_anchor)
            if decision.action == "match" and anchor.source != candidate_anchor.source:
                scored.append((decision.score, candidate_id, candidate_anchor, decision))
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.03):
            return scored[0][1], False
        return self.repo.create_candidate(conn, anchor), True

    def _correlate(
        self, conn, candidate_id: str, anchor: SourceRecord
    ) -> tuple[int, int]:
        matches: defaultdict[str, list[tuple[SourceRecord, IdentityDecision]]] = defaultdict(list)
        reviews: list[tuple[SourceRecord, IdentityDecision]] = []
        for record in self.repo.potential_sources(conn, anchor):
            if not _types_compatible(anchor.primary_type_slug, record.primary_type_slug):
                continue
            decision = decide_identity(anchor, record)
            if decision.action == "match":
                matches[record.source].append((record, decision))
            elif decision.action == "review":
                reviews.append((record, decision))

        linked_count = 0
        review_count = 0
        for source, ranked in matches.items():
            ranked.sort(key=lambda item: item[1].score, reverse=True)
            if source == "ca_abc":
                selected = ranked
            elif len(ranked) == 1 or ranked[0][1].score - ranked[1][1].score >= 0.03:
                selected = ranked[:1]
                reviews.extend(ranked[1:])
            else:
                selected = []
                reviews.extend(ranked)
            for record, decision in selected:
                linked = self.repo.link_source(
                    conn,
                    candidate_id,
                    record,
                    confidence=decision.score,
                    method=decision.reason,
                    metadata={"features": decision.features},
                )
                if linked:
                    linked_count += 1
                else:
                    reviews.append(
                        (
                            record,
                            IdentityDecision(
                                "review",
                                decision.score,
                                "source_already_linked_to_another_candidate",
                                decision.features,
                            ),
                        )
                    )

        for record, decision in reviews:
            self.repo.enqueue_match_review(
                conn,
                candidate_id,
                record,
                reason=decision.reason,
                score=decision.score,
                evidence={"features": decision.features},
            )
            review_count += 1
        return linked_count, review_count

    def _validated_links(
        self,
        conn,
        candidate_id: str,
        anchor: SourceRecord,
    ) -> list[LinkedSource]:
        """Recompute identity from current source values; old scores are never permanent."""
        validated = []
        for link in self.repo.linked_sources(conn, candidate_id):
            record = link.record
            if (
                record.source == anchor.source
                and record.source_record_id == anchor.source_record_id
            ):
                validated.append(link)
                continue
            if not _types_compatible(anchor.primary_type_slug, record.primary_type_slug):
                identity = IdentityDecision(
                    "review", 0.0, "linked_type_now_conflicts", {}
                )
            else:
                identity = decide_identity(anchor, record)
            if identity.action == "match" and identity.score >= 0.96:
                self.repo.link_source(
                    conn,
                    candidate_id,
                    record,
                    confidence=identity.score,
                    method=f"revalidated:{identity.reason}",
                    metadata={"features": identity.features},
                )
                validated.append(
                    LinkedSource(
                        record,
                        identity.score,
                        f"revalidated:{identity.reason}",
                    )
                )
                continue
            self.repo.enqueue_match_review(
                conn,
                candidate_id,
                record,
                reason=f"link_no_longer_valid:{identity.reason}",
                score=identity.score,
                evidence={"features": identity.features},
            )
            self.repo.unlink_source(conn, candidate_id, record)
        return validated


def _types_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right or left == right:
        return True
    if left in BAR_TYPES and right in BAR_TYPES:
        return True
    compatible = {
        frozenset({"brewery", "taproom"}),
        frozenset({"brewery", "brewpub"}),
        frozenset({"winery", "tasting_room"}),
        frozenset({"distillery", "tasting_room"}),
    }
    return frozenset({left, right}) in compatible


def _not_found_verification(
    fsq_place_id: str, storage_policy: str, lease_days: int
) -> VerificationEvidence:
    now = datetime.now(timezone.utc)
    return VerificationEvidence(
        verifier="fsq_premium",
        verifier_record_id=fsq_place_id,
        outcome="fail",
        verification_tier="provider",
        checks={key: False for key in (
            "identity", "currently_operating", "public_access", "display_name", "venue_type"
        )},
        permitted_snapshot={},
        storage_policy=storage_policy,
        verified_at=now,
        expires_at=now + timedelta(days=lease_days),
    )


def _field_coverage(decision: CatalogDecision) -> dict[str, bool]:
    resolved = decision.resolved
    return {
        field: resolved.get(field) not in (None, "", [], {})
        for field in (
            "name",
            "primary_type_slug",
            "address",
            "latitude",
            "longitude",
            "phone_e164",
            "website_url",
            "neighborhood",
            "hours",
            "price_level",
            "setting_slugs",
            "cover_image_url",
        )
    }
