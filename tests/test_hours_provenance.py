from datetime import datetime, timedelta, timezone

from paloma_data.hours_provenance import hours_observation_provenance


def test_first_party_hours_provenance_keeps_only_bounded_public_metadata() -> None:
    observed = datetime(2026, 9, 3, tzinfo=timezone.utc)
    result = hours_observation_provenance(
        {
            "source": "manual",
            "observed_at": observed,
            "expires_at": observed + timedelta(days=30),
            "source_items": [
                {"kind": "first_party", "url": "https://example.com/hours"}
            ],
            "metadata": {"evidence_kind": "first_party"},
        }
    )

    assert result is not None
    assert result.source_kind == "first_party"
    assert result.source_url == "https://example.com/hours"
    assert result.verified_at == observed
    assert result.expires_at == observed + timedelta(days=30)


def test_unbounded_hours_observation_cannot_be_projected() -> None:
    observed = datetime(2026, 9, 3, tzinfo=timezone.utc)
    assert hours_observation_provenance(
        {
            "source": "manual",
            "observed_at": observed,
            "expires_at": None,
            "source_items": [],
            "metadata": {},
        }
    ) is None
