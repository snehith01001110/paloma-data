import pytest

from paloma_data.contributions import _normalize_value


def test_contributed_hours_must_normalize_before_review_acceptance():
    normalized = _normalize_value("hours", {"monday": [["16:00", "02:00"]]})
    assert normalized["value_json"]["schema_version"] == "paloma-hours-v1"
    assert normalized["value_json"]["weekly"][0]["closes_day_offset"] == 1


def test_contributed_price_rejects_values_outside_supported_range():
    with pytest.raises(ValueError, match="1 through 4"):
        _normalize_value("price_level", 5)
