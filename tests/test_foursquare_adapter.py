from paloma_data.adapters.foursquare import FoursquareAdapter


def _adapter() -> FoursquareAdapter:
    return FoursquareAdapter(
        catalog_uri="https://catalog.example.test",
        catalog_token="secret",
        table_name="places.places",
        bbox="-123.2,36.8,-121.1,38.9",
    )


def test_fsq_open_bar_maps_to_consumer_observation():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "abc123",
            "name": "The Public House",
            "latitude": 37.79,
            "longitude": -122.39,
            "address": "1 Market St",
            "locality": "San Francisco",
            "region": "CA",
            "postcode": "94105",
            "country": "US",
            "date_refreshed": "2026-08-01T00:00:00Z",
            "fsq_category_ids": ["13003"],
            "fsq_category_labels": ["Dining and Drinking > Bar > Cocktail Bar"],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.primary_type_slug == "cocktail_bar"
    assert record.source_family == "consumer_poi"
    assert record.consumer_facing is True
    assert record.public_access == "walk_in"
    assert record.source_status == "open"


def test_fsq_private_venue_flag_is_a_hard_negative():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "private123",
            "name": "Members Club Bar",
            "latitude": 37.79,
            "longitude": -122.39,
            "address": "2 Market St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-01T00:00:00Z",
            "fsq_category_labels": ["Dining and Drinking > Bar"],
            "unresolved_flags": ["privateVenue"],
        }
    )

    assert record is not None
    assert "privatevenue" in record.quality_flags
    assert record.public_access == "members_or_private"
