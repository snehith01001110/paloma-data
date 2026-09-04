from datetime import datetime, timezone

import httpx

from paloma_data.media_discovery import (
    EstablishmentMediaTarget,
    MapillaryMediaClient,
    WikimediaCommonsMediaClient,
    _bbox,
)


TARGET = EstablishmentMediaTarget(
    establishment_id="454159f6-5572-4852-b8f3-3bdb8faf92cd",
    name="Crown Billiards",
    address="2416 San Ramon Valley Blvd",
    city="San Ramon",
    latitude=37.7748,
    longitude=-121.9752,
)


def test_mapillary_candidates_are_rights_qualified_but_never_identity_qualified():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "OAuth test-token"
        assert "bbox" in request.url.params
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "near-facing",
                        "computed_geometry": {
                            "type": "Point",
                            "coordinates": [-121.9752, 37.77435],
                        },
                        "captured_at": 1_767_225_600_000,
                        "computed_compass_angle": 0,
                        "creator": {"username": "open_mapper"},
                        "camera_type": "perspective",
                        "is_pano": False,
                        "thumb_2048_url": "https://images.example/near.jpg",
                        "width": 2048,
                        "height": 1536,
                    },
                    {
                        "id": "same-point-facing-away",
                        "computed_geometry": {
                            "type": "Point",
                            "coordinates": [-121.9752, 37.7744],
                        },
                        "captured_at": 1_767_225_600_000,
                        "computed_compass_angle": 180,
                        "creator": {"username": "open_mapper"},
                        "camera_type": "perspective",
                        "is_pano": False,
                        "thumb_2048_url": "https://images.example/away.jpg",
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = MapillaryMediaClient("test-token", client=http_client)
        candidates = client.search(
            TARGET,
            radius_meters=180,
            limit=10,
            now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        )

    assert [candidate.source_asset_id for candidate in candidates] == [
        "near-facing",
        "same-point-facing-away",
    ]
    first = candidates[0]
    assert first.rights.license_id == "CC-BY-SA-4.0"
    assert first.rights.share_alike_required is True
    assert first.generation_allowed_after_visual_review is True
    assert "visual_identity_review_required" in first.review_flags
    assert "target_likely_out_of_frame" in candidates[1].review_flags
    assert "thumb_2048_url" not in first.source_page_url


def test_mapillary_manifest_can_exclude_expiring_preview_url():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "frame-1",
                        "computed_geometry": {
                            "type": "Point",
                            "coordinates": [-121.9752, 37.77435],
                        },
                        "computed_compass_angle": 0,
                        "thumb_2048_url": "https://signed.example/frame-1.jpg",
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        candidate = MapillaryMediaClient("token", client=http_client).search(TARGET)[0]

    assert "preview_url" in candidate.manifest()
    assert "preview_url" not in candidate.manifest(include_ephemeral_preview=False)
    assert candidate.source_page_url.endswith("pKey=frame-1&focus=photo")


def test_commons_keeps_attribution_and_rejects_noncommercial_files():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "pageid": 1,
                            "title": "File:Crown Billiards exterior.jpg",
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.example/crown-2048.jpg",
                                    "url": "https://upload.example/crown.jpg",
                                    "descriptionurl": "https://commons.example/File:Crown",
                                    "width": 4000,
                                    "height": 3000,
                                    "timestamp": "2025-06-01T00:00:00Z",
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                        "LicenseUrl": {
                                            "value": (
                                                "https://creativecommons.org/licenses/"
                                                "by-sa/4.0/"
                                            )
                                        },
                                        "Artist": {"value": "<b>Jane Mapper</b>"},
                                        "Credit": {"value": "Jane Mapper / Commons"},
                                    },
                                }
                            ],
                        },
                        {
                            "pageid": 2,
                            "title": "File:Crown Billiards restricted.jpg",
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.example/restricted.jpg",
                                    "descriptionurl": "https://commons.example/File:Restricted",
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY-NC-SA 4.0"},
                                        "LicenseUrl": {
                                            "value": (
                                                "https://creativecommons.org/licenses/"
                                                "by-nc-sa/4.0/"
                                            )
                                        },
                                    },
                                }
                            ],
                        },
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        candidates = WikimediaCommonsMediaClient(client=http_client).search(TARGET)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.creator == "Jane Mapper"
    assert candidate.rights.license_id == "CC-BY-SA-4.0"
    assert candidate.attribution_text.startswith("Jane Mapper / Commons")
    assert "generated_derivative_must_remain_share_alike" in candidate.review_flags


def test_commons_rejects_same_brand_photo_geotagged_in_another_city():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "pageid": 3,
                            "title": "File:Crown Billiards exterior.jpg",
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.example/crown.jpg",
                                    "descriptionurl": "https://commons.example/File:Crown",
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY 4.0"},
                                        "LicenseUrl": {
                                            "value": "https://creativecommons.org/licenses/by/4.0/"
                                        },
                                        "GPSLatitude": {"value": "38.301586"},
                                        "GPSLongitude": {"value": "-122.282325"},
                                    },
                                }
                            ],
                        }
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        candidates = WikimediaCommonsMediaClient(client=http_client).search(TARGET)

    assert candidates == []


def test_commons_rejects_weak_filename_matches_before_review():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "pageid": 4,
                            "title": "File:Crown molding in a games room.jpg",
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.example/unrelated.jpg",
                                    "descriptionurl": "https://commons.example/File:Unrelated",
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY 4.0"},
                                        "LicenseUrl": {
                                            "value": "https://creativecommons.org/licenses/by/4.0/"
                                        },
                                    },
                                }
                            ],
                        }
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        candidates = WikimediaCommonsMediaClient(client=http_client).search(TARGET)

    assert candidates == []


def test_bbox_contains_target_and_respects_requested_radius():
    west, south, east, north = (float(value) for value in _bbox(37.7748, -121.9752, 180).split(","))
    assert west < -121.9752 < east
    assert south < 37.7748 < north
    assert 0.003 < east - west < 0.005
    assert 0.003 < north - south < 0.004
