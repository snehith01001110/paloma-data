from datetime import datetime, timedelta, timezone

import pytest

from paloma_data.evidence_ledger import (
    _normalize_manual_value,
    append_manual_establishment_observation,
)


class _Connection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.params: list[tuple] = []

    def execute(self, query, params):
        self.queries.append(query)
        self.params.append(params)
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


def test_published_hours_accept_first_party_evidence_with_a_bounded_lease() -> None:
    conn = _Connection()
    result = append_manual_establishment_observation(
        conn,
        "00000000-0000-0000-0000-000000000001",
        field_name="hours",
        value={"monday": [["10:00", "00:00"]]},
        reviewer="github:reviewer",
        evidence_urls=("https://example.com/location",),
        evidence_kind="first_party",
        note="Reviewed the venue's current official location page.",
        lease_days=30,
        observed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )

    insert_index = next(
        index for index, query in enumerate(conn.queries) if "insert into" in query
    )
    serialized_params = " ".join(str(value) for value in conn.params[insert_index])
    assert '"kind": "first_party"' in serialized_params
    assert '"evidence_kind": "first_party"' in serialized_params
    assert result["value_json"]["schema_version"] == "paloma-hours-v1"
    assert result["expires_at"].startswith("2026-10-03")
