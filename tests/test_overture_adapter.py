from datetime import timezone

from pathlib import Path

from paloma_data.adapters import overture
from paloma_data.adapters.overture import OvertureAdapter
from paloma_data.taxonomy import classify_overture


def test_overture_cocktail_bar_parses_as_corroboration_record():
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-122.4194, 37.7749]},
        "properties": {
            "id": "99003ee6-e75b-4dd6-8a8a-53a5a716c50d",
            "version": 4,
            "names": {"primary": "Example Cocktail Bar"},
            "basic_category": "bar",
            "taxonomy": {
                "primary": "cocktail_bar",
                "hierarchy": ["food_and_drink", "bar", "cocktail_bar"],
                "alternates": [],
            },
            "confidence": 0.98,
            "websites": ["https://example.com"],
            "phones": ["+14155551212"],
            "addresses": [
                {
                    "freeform": "123 Valencia St",
                    "locality": "San Francisco",
                    "postcode": "94103",
                    "region": "CA",
                    "country": "US",
                }
            ],
            "operating_status": "open",
            "sources": [
                {
                    "dataset": "meta",
                    "record_id": "abc",
                    "update_time": "2026-06-01T00:00:00Z",
                }
            ],
        },
    }

    record = OvertureAdapter("-123.2,36.8,-121.1,38.9")._to_record(feature)

    assert record is not None
    assert record.source == "overture"
    assert record.primary_type_slug == "cocktail_bar"
    assert record.classification_confidence == 0.94
    assert record.source_status == "open"
    assert record.city == "San Francisco"
    assert record.latitude == 37.7749
    assert record.longitude == -122.4194
    assert record.consumer_facing is True
    assert record.public_access == "walk_in"


def test_overture_mixed_timestamp_formats_are_normalized_to_utc():
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-122.4194, 37.7749]},
        "properties": {
            "id": "mixed-timezones",
            "names": {"primary": "Example Brewery"},
            "basic_category": "brewery",
            "taxonomy": {"primary": "brewery"},
            "confidence": 0.98,
            "addresses": [
                {
                    "freeform": "123 Valencia St",
                    "locality": "San Francisco",
                    "postcode": "94103",
                    "region": "CA",
                    "country": "US",
                }
            ],
            "operating_status": "open",
            "sources": [
                {"dataset": "a", "update_time": "2026-06-01T00:00:00"},
                {"dataset": "b", "update_time": "2026-06-02T00:00:00Z"},
            ],
        },
    }

    record = OvertureAdapter("-123.2,36.8,-121.1,38.9")._to_record(feature)

    assert record is not None
    assert record.source_updated_at is not None
    assert record.source_updated_at.tzinfo == timezone.utc
    assert record.source_updated_at.isoformat() == "2026-06-02T00:00:00+00:00"


def test_generic_bar_is_candidate_not_specific_type():
    classification = classify_overture("The Place", {"food_and_drink", "bar"}, 0.99)
    assert classification.eligible is True
    assert classification.primary_type_slug == "bar"
    assert classification.confidence == 0.86


def test_generic_manufacturer_is_not_public_access_evidence():
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-122.4194, 37.7749]},
        "properties": {
            "id": "manufacturer-only",
            "names": {"primary": "Example Brewing LLC"},
            "basic_category": "brewery",
            "taxonomy": {"primary": "brewery"},
            "confidence": 0.99,
            "addresses": [
                {
                    "freeform": "123 Industrial St",
                    "locality": "San Francisco",
                    "region": "CA",
                    "country": "US",
                }
            ],
            "operating_status": "open",
            "sources": [{"dataset": "x", "update_time": "2026-06-01T00:00:00Z"}],
        },
    }

    record = OvertureAdapter("-123.2,36.8,-121.1,38.9")._to_record(feature)

    assert record is not None
    assert record.primary_type_slug == "brewery"
    assert record.consumer_facing is False
    assert record.public_access == "unknown"


def test_overture_bbox_validation():
    adapter = OvertureAdapter("-123.2,36.8,-121.1,38.9")
    assert adapter.bbox == "-123.2,36.8,-121.1,38.9"


def test_overture_release_discovery_uses_public_bucket_prefixes():
    document = """\
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <CommonPrefixes><Prefix>release/2026-07-22.0/</Prefix></CommonPrefixes>
      <CommonPrefixes><Prefix>release/2026-08-19.1/</Prefix></CommonPrefixes>
      <CommonPrefixes><Prefix>release/not-a-release/</Prefix></CommonPrefixes>
    </ListBucketResult>
    """

    assert overture._latest_release_from_index(document) == "2026-08-19.1"


def test_overture_download_bypasses_stac_with_explicit_release(monkeypatch, tmp_path):
    calls = {}

    class Reader:
        schema = object()

    class Writer:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    def fake_reader(type_, **kwargs):
        calls["reader"] = (type_, kwargs)
        return Reader()

    def fake_writer(format_, path, schema):
        calls["writer"] = (format_, path, schema)
        return Writer()

    monkeypatch.setattr(overture, "record_batch_reader", fake_reader)
    monkeypatch.setattr(overture, "get_writer", fake_writer)
    monkeypatch.setattr(overture, "copy", lambda reader, writer: calls.update(copied=True))

    output = tmp_path / "places.geojsonseq"
    overture._download_release(output, "-123.2,36.8,-121.1,38.9", "2026-07-22.0")

    assert calls["reader"] == (
        "place",
        {
            "bbox": [-123.2, 36.8, -121.1, 38.9],
            "release": "2026-07-22.0",
            "stac": False,
        },
    )
    assert calls["writer"] == ("geojsonseq", str(Path(output)), Reader.schema)
    assert calls["copied"] is True
