import json

import pytest

from paloma_data.cli import _parse_field_reviews


def test_parse_field_reviews_normalizes_a_bounded_batch() -> None:
    reviews = _parse_field_reviews(
        json.dumps(
            [
                {
                    "conflict_id": 17,
                    "city": " Dublin ",
                    "notes": " verified storefront ",
                    "selected_evidence_id": "00000000-0000-0000-0000-000000000001",
                },
                {
                    "conflict_id": 18,
                    "city": "San Ramon",
                    "notes": "Audited unknown",
                },
            ]
        )
    )

    assert reviews == [
        {
            "conflict_id": 17,
            "city": "Dublin",
            "notes": "verified storefront",
            "selected_evidence_id": "00000000-0000-0000-0000-000000000001",
        },
        {
            "conflict_id": 18,
            "city": "San Ramon",
            "notes": "Audited unknown",
            "selected_evidence_id": None,
        },
    ]


@pytest.mark.parametrize(
    "value, message",
    [
        ([], "1-200"),
        ([{"conflict_id": True, "city": "Dublin", "notes": "x"}], "positive"),
        (
            [
                {"conflict_id": 1, "city": "Dublin", "notes": "x"},
                {"conflict_id": 1, "city": "Dublin", "notes": "y"},
            ],
            "more than once",
        ),
        (
            [
                {
                    "conflict_id": 1,
                    "city": "Dublin",
                    "notes": "x",
                    "selected_evidence_id": "not-a-uuid",
                }
            ],
            "must be a UUID",
        ),
        ([{"conflict_id": 1, "city": "Dublin", "notes": "x", "extra": 1}], "shape"),
    ],
)
def test_parse_field_reviews_rejects_invalid_payloads(value, message) -> None:
    with pytest.raises(ValueError, match=message):
        _parse_field_reviews(json.dumps(value))
