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


def test_fsq_os_adapter_never_mislabels_rich_fields_as_apache_data():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "rich123",
            "name": "Roof Garden",
            "latitude": 37.79,
            "longitude": -122.39,
            "address": "3 Market St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-01T00:00:00Z",
            "fsq_category_labels": ["Dining and Drinking > Bar > Rooftop Bar"],
            "unresolved_flags": [],
            "hours": '{"friday":[["16:00","02:00"]]}',
            "price": "Expensive",
            "outdoorseating": True,
        }
    )

    assert record is not None
    assert record.hours is None
    assert record.price_level is None
    assert record.setting_slugs == ("rooftop",)


def test_fsq_stale_or_missing_refresh_date_is_not_open():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "stale123",
            "name": "Old Bar",
            "latitude": 37.79,
            "longitude": -122.39,
            "address": "4 Market St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2020-01-01T00:00:00Z",
            "fsq_category_labels": ["Dining and Drinking > Bar"],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.source_status == "unknown"
    assert "stale" in record.quality_flags


def test_fsq_secondary_bar_category_does_not_turn_restaurant_into_candidate():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "restaurant123",
            "name": "Kokkari Estiatorio",
            "latitude": 37.79,
            "longitude": -122.39,
            "address": "200 Jackson St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-10T00:00:00Z",
            "fsq_category_labels": [
                "Dining and Drinking > Restaurant > Greek Restaurant",
                "Dining and Drinking > Bar > Cocktail Bar",
            ],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.consumer_facing is False
    assert record.public_access == "unknown"
    assert "consumer_identity_conflict" in record.quality_flags


def test_fsq_generic_bar_category_requires_consumer_name_signal():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "coffee123",
            "name": "Bluestone Lane",
            "latitude": 37.79,
            "longitude": -122.39,
            "address": "227 Front St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-07T00:00:00Z",
            "fsq_category_labels": [
                "Dining and Drinking > Cafe, Coffee, and Tea House > Coffee Shop",
                "Dining and Drinking > Bar",
            ],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.consumer_facing is False
    assert "consumer_identity_conflict" in record.quality_flags


def test_fsq_bar_name_cannot_override_restaurant_category_conflict():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "bar123",
            "name": "Eclipse Kitchen & Bar",
            "latitude": 37.79,
            "longitude": -122.39,
            "address": "5 Embarcadero Ctr",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-05T00:00:00Z",
            "fsq_category_labels": [
                "Dining and Drinking > Bar > Hotel Bar",
                "Dining and Drinking > Restaurant > American Restaurant",
            ],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.consumer_facing is False
    assert record.public_access == "unknown"
    assert "consumer_identity_conflict" in record.quality_flags


def test_fsq_supermarket_with_secondary_wine_bar_category_is_not_a_candidate():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "cal-mart",
            "name": "Cal-Mart",
            "latitude": 37.786,
            "longitude": -122.452,
            "address": "3585 California St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-10T00:00:00Z",
            "fsq_category_labels": [
                "Retail > Food and Beverage Retail > Supermarket",
                "Dining and Drinking > Bar > Wine Bar",
            ],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.primary_type_slug == "wine_bar"
    assert record.consumer_facing is False
    assert record.public_access == "unknown"
    assert "consumer_identity_conflict" in record.quality_flags


def test_fsq_parent_hotel_record_with_bar_category_is_not_a_candidate():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "hotel-parent",
            "name": "InterContinental Bar @ InterContinental San Francisco",
            "latitude": 37.782,
            "longitude": -122.404,
            "address": "888 Howard St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-10T00:00:00Z",
            "fsq_category_labels": [
                "Dining and Drinking > Bar > Hotel Bar",
                "Travel and Transportation > Lodging > Hostel",
                "Sports and Recreation > Gym and Studio > Gym",
            ],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.consumer_facing is False
    assert "consumer_identity_conflict" in record.quality_flags


def test_fsq_standalone_hotel_bar_remains_a_candidate():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "hotel-bar",
            "name": "Top of the Mark",
            "latitude": 37.792,
            "longitude": -122.410,
            "address": "999 California St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-10T00:00:00Z",
            "fsq_category_labels": ["Dining and Drinking > Bar > Hotel Bar"],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.consumer_facing is True
    assert record.public_access == "walk_in"


def test_fsq_social_club_with_secondary_bar_category_is_not_a_candidate():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "social-club",
            "name": "Power Exchange",
            "latitude": 37.783,
            "longitude": -122.412,
            "address": "220 Jones St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-10T00:00:00Z",
            "fsq_category_labels": [
                "Arts and Entertainment > Night Club",
                "Dining and Drinking > Bar",
                "Community and Government > Social Club",
            ],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.consumer_facing is False
    assert "consumer_identity_conflict" in record.quality_flags


def test_fsq_brewery_restaurant_becomes_access_gated_brewpub_candidate():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "brewpub",
            "name": "Southern Pacific Brewing",
            "latitude": 37.76,
            "longitude": -122.41,
            "address": "620 Treat Ave",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-10T00:00:00Z",
            "fsq_category_labels": [
                "Dining and Drinking > Brewery",
                "Dining and Drinking > Restaurant > American Restaurant",
            ],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.primary_type_slug == "brewpub"
    assert record.consumer_facing is True
    assert record.public_access == "walk_in"


def test_fsq_explicit_brewpub_name_refines_generic_brewery_category():
    record = _adapter()._to_record(
        {
            "fsq_place_id": "lost-marbles",
            "name": "Lost Marbles Brewpub",
            "latitude": 37.782,
            "longitude": -122.467,
            "address": "823 Clement St",
            "locality": "San Francisco",
            "region": "CA",
            "country": "US",
            "date_refreshed": "2026-08-10T00:00:00Z",
            "fsq_category_labels": ["Dining and Drinking > Brewery"],
            "unresolved_flags": [],
        }
    )

    assert record is not None
    assert record.primary_type_slug == "brewpub"
    assert record.category_evidence["reason"] == "fsq_taxonomy:brewery+name_brewpub"
