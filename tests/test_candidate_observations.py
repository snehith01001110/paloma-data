from paloma_data.candidate_observations import load_candidate_observation_manifest
from paloma_data.hours import normalize_hours


def test_east_bay_observation_manifest_is_bounded_and_structured():
    manifest = load_candidate_observation_manifest()

    assert manifest.manifest_id == "east-bay-pilot-field-observations-v1"
    assert len(manifest.sha256) == 64
    assert len(manifest.observations) == 18
    assert {item.city for item in manifest.observations} == {"Berkeley", "Oakland"}
    assert {item.field_name for item in manifest.observations} == {"hours"}
    assert len({item.candidate_id for item in manifest.observations}) == 18
    assert all(normalize_hours(item.value) for item in manifest.observations)


def test_manifest_preserves_split_service_and_special_closure():
    manifest = load_candidate_observation_manifest()
    cellarmaker = next(
        item
        for item in manifest.observations
        if item.candidate_id == "b227de6d-7fcb-4187-b011-6a58f245b02e"
    )
    drakes = next(
        item
        for item in manifest.observations
        if item.candidate_id == "d7ad07af-a1ef-4aea-b1fc-ab32a378bce7"
    )

    assert len(normalize_hours(cellarmaker.value)["weekly"]) == 9
    assert normalize_hours(drakes.value)["special"] == [
        {"date": "2026-10-22", "closed": True}
    ]
