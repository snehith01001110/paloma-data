from datetime import datetime, timedelta, timezone

import pytest

from paloma_data.evidence_ledger import (
    _normalize_manual_value,
    append_manual_establishment_observation,
)


class _Connection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, query, params):
        self.queries.append(query)
        if "from public.establishments" in query:
            return _Result({"name": "Closed Pub", "city": "San Francisco"})
        if "current_source_field_policies" in query:
            return _Result(
                {
                    "source_policy_id": 9,
                    "policy_version": "paloma-curation-v1",
                    "authority": 1.0,
                    "recommended_max_age": timedelta(days=90),
                    "normalized_persistence_allowed": True,
                    "source_derivation_allowed": True,
                    "durable_storage_allowed": True,
                    "canonical_derivation_allowed": True,
                }
            )
        if "insert into catalog.field_observations" in query:
            return _Result({"id": "00000000-0000-0000-0000-000000000009"})
        raise AssertionError(query)


class _Result:
    def __init__(self, row) -> None:
        self.row = row

    def fetchone(self):
        return self.row


def test_manual_operating_status_is_strictly_normalized() -> None:
    assert _normalize_manual_value("operating_status", " CLOSED ")["value_text"] == "closed"
    with pytest.raises(ValueError, match="operating_status must be"):
        _normalize_manual_value("operating_status", "maybe")


def test_append_manual_establishment_status_uses_establishment_column() -> None:
    conn = _Connection()
    result = append_manual_establishment_observation(
        conn,
        "00000000-0000-0000-0000-000000000001",
        field_name="operating_status",
        value="closed",
        reviewer="github:reviewer",
        evidence_urls=("https://example.com/closure",),
        note="Owner-confirmed closure reported by an authoritative publication.",
        lease_days=90,
        observed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    insert = next(query for query in conn.queries if "insert into" in query)
    assert "{entity_column}" not in insert
    assert "establishment_id, field_name" in insert
    assert result["value_text"] == "closed"
    assert result["establishment_name"] == "Closed Pub"
