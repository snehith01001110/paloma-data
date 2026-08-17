from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import json
from typing import Any


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
    source_status: str | None = None
    source_updated_at: datetime | None = None
    primary_type_slug: str | None = None
    classification_confidence: float | None = None
    category_evidence: dict[str, Any] = field(default_factory=dict)
    permitted_metadata: dict[str, Any] = field(default_factory=dict)

    def stable_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "address": self.address,
            "city": self.city,
            "region": self.region,
            "postal_code": self.postal_code,
            "country_code": self.country_code,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "phone": self.phone,
            "website_url": self.website_url,
            "source_status": self.source_status,
            "source_updated_at": self.source_updated_at.isoformat() if self.source_updated_at else None,
            "primary_type_slug": self.primary_type_slug,
            "classification_confidence": self.classification_confidence,
            "category_evidence": self.category_evidence,
            "permitted_metadata": self.permitted_metadata,
        }

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
