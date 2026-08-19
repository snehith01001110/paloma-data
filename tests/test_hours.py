import pytest

from paloma_data.hours import HoursFormatError, normalize_hours


def test_normalizes_split_and_overnight_hours():
    assert normalize_hours(
        {"friday": [["16:00", "02:00"], ["11:00", "14:00"]]}
    ) == {
        "schema_version": "paloma-hours-v1",
        "timezone": "America/Los_Angeles",
        "weekly": [
            {"day": 5, "opens": "11:00", "closes": "14:00", "closes_day_offset": 0},
            {"day": 5, "opens": "16:00", "closes": "02:00", "closes_day_offset": 1},
        ],
        "special": [],
    }


def test_preserves_special_closure_and_24_hour_end():
    value = {
        "schema_version": "paloma-hours-v1",
        "timezone": "America/Los_Angeles",
        "weekly": [
            {"day": 1, "opens": "00:00", "closes": "00:00", "closes_day_offset": 1}
        ],
        "special": [{"date": "2026-12-25", "closed": True}],
    }
    assert normalize_hours(value) == value


def test_rejects_provider_native_text_instead_of_guessing():
    with pytest.raises(HoursFormatError, match="structured"):
        normalize_hours("Mo-Su 16:00-02:00")
