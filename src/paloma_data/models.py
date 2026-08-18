from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any


def _json_safe(value: Any) -> Any:
    """Normalize database/native values into deterministic JSON-compatible primitives."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@dataclass(slots=True)
class SourceRecord:
    source: str
    source_record_id: str
    name: str
    address: str
    city: str
    region: str | None = None
    postal_code: str | None = None
    country_code: str = "US"
    latitude: float | None = None
    longitude: float | None = None
    phone: str | None = None
    website_url: str | None = None
    neighborhood: str | None = None
    hours: dict[str, Any] | list[Any] | str | None = None
    price_level: int | None = None
    setting_slugs: tuple[str, ...] = ()
    source_status: str | None = None
    source_updated_at: datetime | None = None
    primary_type_slug: str | None = None
    classification_confidence: float | None = None
    # These fields describe what a source row can prove. They deliberately separate a legal
    # premises record from a consumer place that people can actually visit.
    source_family: str = "unknown"
    consumer_facing: bool = False
    public_access: str = "unknown"
    quality_flags: tuple[str, ...] = ()
    category_evidence: dict[str, Any] = field(default_factory=dict)
    permitted_metadata: dict[str, Any] = field(default_factory=dict)

    def stable_payload(self) -> dict[str, Any]:
        return _json_safe(
            {
                "name": self.name,
                "address": self.address,
                "city": self.city,
                "region": self.region,
                "postal_code": self.postal_code,
                "country_code": self.country_code,
                "latitude": float(self.latitude) if self.latitude is not None else None,
                "longitude": float(self.longitude) if self.longitude is not None else None,
                "phone": self.phone,
                "website_url": self.website_url,
                "neighborhood": self.neighborhood,
                "hours": self.hours,
                "price_level": self.price_level,
                "setting_slugs": sorted(set(self.setting_slugs)),
                "source_status": self.source_status,
                "source_updated_at": self.source_updated_at,
                "primary_type_slug": self.primary_type_slug,
                "classification_confidence": (
                    float(self.classification_confidence)
                    if self.classification_confidence is not None
                    else None
                ),
                "source_family": self.source_family,
                "consumer_facing": self.consumer_facing,
                "public_access": self.public_access,
                "quality_flags": sorted(set(self.quality_flags)),
                "category_evidence": self.category_evidence,
                "permitted_metadata": self.permitted_metadata,
            }
        )

    def payload_hash(self) -> str:
        encoded = json.dumps(
            self.stable_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        return sha256(encoded).hexdigest()


@dataclass(slots=True)
class CanonicalCandidate:
    id: str
    name: str
    normalized_name: str | None
    address: str
    normalized_address: str | None
    city: str
    region: str | None
    postal_code: str | None
    country_code: str
    latitude: float | None
    longitude: float | None
    phone_e164: str | None
    website_url: str | None
    status: str


@dataclass(slots=True)
class MatchDecision:
    action: str  # exact | auto_match | review | distinct
    score: float
    candidate_id: str | None = None
    reason: str | None = None
