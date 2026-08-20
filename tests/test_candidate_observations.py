from paloma_data.candidate_observations import load_candidate_observation_manifest
from paloma_data.hours import normalize_hours


def test_east_bay_observation_manifest_is_bounded_and_structured():
    manifest = load_candidate_observation_manifest()

    assert manifest.manifest_id == "east-bay-pilot-field-observations-v1"
    assert len(manifest.sha256) == 64
    assert len(manifest.observations) == 28
    assert {item.city for item in manifest.observations} == {"Berkeley", "Oakland"}
    assert {item.field_name for item in manifest.observations} == {
        "hours",
        "phone_e164",
        "website_url",
    }
    assert len({item.candidate_id for item in manifest.observations}) == 21
    assert all(
        normalize_hours(item.value)
        for item in manifest.observations
        if item.field_name == "hours"
    )


def test_manifest_preserves_split_service_and_special_closure():
    manifest = load_candidate_observation_manifest()
    cellarmaker = next(
        item
        for item in manifest.observations
        if item.candidate_id == "b227de6d-7fcb-4187-b011-6a58f245b02e"
        and item.field_name == "hours"
    )
    drakes = next(
        item
        for item in manifest.observations
        if item.candidate_id == "d7ad07af-a1ef-4aea-b1fc-ab32a378bce7"
        and item.field_name == "hours"
    )

    assert len(normalize_hours(cellarmaker.value)["weekly"]) == 9
    assert normalize_hours(drakes.value)["special"] == [
        {"date": "2026-10-22", "closed": True}
    ]
