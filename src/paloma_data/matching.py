from __future__ import annotations

from paloma_data.models import CanonicalCandidate, MatchDecision, SourceRecord
from paloma_data.normalizers import (
    haversine_meters,
    normalize_address,
    normalize_name,
    normalize_phone,
    similarity,
    website_host,
)


def _geo_score(record: SourceRecord, candidate: CanonicalCandidate) -> float:
    if None in (record.latitude, record.longitude, candidate.latitude, candidate.longitude):
        return 0.0
    distance = haversine_meters(
        record.latitude,
        record.longitude,
        candidate.latitude,
        candidate.longitude,
    )
    if distance <= 20:
        return 1.0
    if distance >= 100:
        return 0.0
    return 1.0 - ((distance - 20.0) / 80.0)


def score_match(record: SourceRecord, candidate: CanonicalCandidate) -> tuple[float, dict[str, float]]:
    record_name = normalize_name(record.name)
    record_address = normalize_address(record.address)
    candidate_name = candidate.normalized_name or normalize_name(candidate.name)
    candidate_address = candidate.normalized_address or normalize_address(candidate.address)

    n = similarity(record_name, candidate_name)
    a = similarity(record_address, candidate_address)

    record_phone = normalize_phone(record.phone, record.country_code)
    p = 1.0 if record_phone and candidate.phone_e164 and record_phone == candidate.phone_e164 else 0.0

    record_host = website_host(record.website_url)
    candidate_host = website_host(candidate.website_url)
    w = 1.0 if record_host and candidate_host and record_host == candidate_host else 0.0

    g = _geo_score(record, candidate)
    score = (0.40 * n) + (0.30 * a) + (0.10 * p) + (0.10 * w) + (0.10 * g)
    return score, {"name": n, "address": a, "phone": p, "website": w, "geo": g}


def decide_match(record: SourceRecord, candidates: list[CanonicalCandidate]) -> MatchDecision:
    if not candidates:
        return MatchDecision("distinct", 0.0, reason="no_candidates")

    ranked: list[tuple[float, CanonicalCandidate, dict[str, float]]] = []
    for candidate in candidates:
        score, features = score_match(record, candidate)
        ranked.append((score, candidate, features))
    ranked.sort(key=lambda item: item[0], reverse=True)

    best_score, best, features = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0

    if features["address"] == 1.0 and features["name"] >= 0.96:
        return MatchDecision("auto_match", max(best_score, 0.97), best.id, "exact_address_strong_name")

    # Exact phone + strong physical location is an identity signal even after a business rebrand.
    # Requiring the new public name to resemble the former name would create duplicates precisely
    # when a venue changes operators or branding.
    if features["phone"] == 1.0 and (features["address"] >= 0.90 or features["geo"] >= 0.85):
        return MatchDecision("auto_match", max(best_score, 0.98), best.id, "exact_phone_location")

    if features["website"] == 1.0 and features["address"] >= 0.95:
        return MatchDecision("auto_match", max(best_score, 0.96), best.id, "exact_website_location")

    # A strong same-location signal plus a divergent name is more likely a rename/operator change
    # than an unrelated new establishment. Never call it distinct automatically; route it to review
    # unless phone/website already proved identity above.
    same_location = features["address"] >= 0.98 and (features["geo"] >= 0.80 or features["name"] >= 0.55)
    if same_location and features["name"] < 0.75:
        return MatchDecision(
            "review",
            max(best_score, 0.84),
            best.id,
            "same_location_name_conflict",
        )

    if best_score >= 0.92 and (best_score - second_score) >= 0.05:
        return MatchDecision("auto_match", best_score, best.id, "weighted_score")

    if best_score >= 0.80:
        return MatchDecision("review", best_score, best.id, "ambiguous_match")

    return MatchDecision("distinct", best_score, reason="below_match_threshold")
