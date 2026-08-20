from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib import resources
import json
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from paloma_data.evidence_ledger import MANUAL_CANDIDATE_FIELDS
from paloma_data.hours import normalize_hours


_MANIFEST_RESOURCE = "data/east_bay_pilot_field_observations_v1.json"


@dataclass(frozen=True, slots=True)
class CandidateFieldObservation:
    candidate_id: str
    candidate_name: str
    city: str
    field_name: str
    value: Any
    evidence_urls: tuple[str, ...]
    note: str
    lease_days: int


@dataclass(frozen=True, slots=True)
class CandidateObservationManifest:
    manifest_id: str
    sha256: str
    observations: tuple[CandidateFieldObservation, ...]


def load_candidate_observation_manifest() -> CandidateObservationManifest:
    raw = resources.files("paloma_data").joinpath(_MANIFEST_RESOURCE).read_bytes()
    payload = json.loads(raw)
    if payload.get("schema_version") != 1:
        raise RuntimeError("Unsupported candidate-observation manifest schema")
    manifest_id = str(payload.get("manifest_id") or "").strip()
    if not manifest_id:
        raise RuntimeError("Candidate-observation manifest requires manifest_id")

    observations: list[CandidateFieldObservation] = []
    identities: set[tuple[str, str]] = set()
    for item in payload.get("observations") or ():
        candidate_id = str(item.get("candidate_id") or "")
        try:
            UUID(candidate_id)
        except ValueError as exc:
            raise RuntimeError(f"Invalid candidate UUID in {manifest_id}") from exc
        field_name = str(item.get("field_name") or "").strip()
        if field_name not in MANUAL_CANDIDATE_FIELDS:
            raise RuntimeError(f"Unsupported field {field_name!r} in {manifest_id}")
        identity = (candidate_id, field_name)
        if identity in identities:
            raise RuntimeError(f"Duplicate candidate field {identity!r} in {manifest_id}")
        identities.add(identity)
        evidence_urls = tuple(str(url).strip() for url in item.get("evidence_urls") or ())
        if not evidence_urls or len(evidence_urls) > 10 or any(
            urlsplit(url).scheme != "https" or not urlsplit(url).netloc
            for url in evidence_urls
        ):
            raise RuntimeError(f"Invalid evidence URLs for {identity!r} in {manifest_id}")
        lease_days = int(item.get("lease_days") or 0)
        if lease_days not in range(1, 366):
            raise RuntimeError(f"Invalid lease_days for {identity!r} in {manifest_id}")
        value = item.get("value")
        if field_name == "hours":
            normalize_hours(value)
        candidate_name = str(item.get("candidate_name") or "").strip()
        city = str(item.get("city") or "").strip()
        note = str(item.get("note") or "").strip()
        if not candidate_name or not city or not note:
            raise RuntimeError(f"Missing review guardrail for {identity!r} in {manifest_id}")
        observations.append(
            CandidateFieldObservation(
                candidate_id=candidate_id,
                candidate_name=candidate_name,
                city=city,
                field_name=field_name,
                value=value,
                evidence_urls=evidence_urls,
                note=note,
                lease_days=lease_days,
            )
        )
    if not observations:
        raise RuntimeError(f"Candidate-observation manifest {manifest_id} is empty")
    return CandidateObservationManifest(
        manifest_id=manifest_id,
        sha256=sha256(raw).hexdigest(),
        observations=tuple(observations),
    )
