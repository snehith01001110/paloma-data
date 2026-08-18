import httpx
import pytest

from paloma_data.adapters.foursquare_api import FoursquarePlacesAPI


def test_places_api_parser_handles_current_flat_and_nested_fields():
    adapter = FoursquarePlacesAPI(
        "test-key",
        storage_policy="contract",
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )
    record = adapter._to_record(
        {
            "fsq_place_id": "fsq-1",
            "name": "The Public House",
            "geocodes": {"main": {"latitude": 37.79, "longitude": -122.39}},
            "location": {
                "address": "1 Market St",
                "locality": "San Francisco",
                "region": "CA",
                "postcode": "94105",
                "country": "US",
                "neighborhood": ["SoMa"],
            },
            "categories": [{"id": "13009", "name": "Cocktail Bar"}],
            "tel": "+14155551212",
            "website": "https://publichouse.example",
            "hours": {"friday": [["16:00", "02:00"]]},
            "price": 3,
            "attributes": {"outdoor_seating": True},
            "veracity_rating": 5,
        }
    )

    assert record is not None
    assert record.primary_type_slug == "cocktail_bar"
    assert record.neighborhood == "SoMa"
    assert record.provider_veracity == 5
    assert record.hours == {"friday": [["16:00", "02:00"]]}
    assert record.price_level == 3
    assert record.setting_slugs == ("outdoor_patio",)
    assert record.storage_scope == "contract"


def test_places_api_details_404_is_distinct_from_a_transport_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    client = httpx.Client(
        base_url="https://places-api.foursquare.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = FoursquarePlacesAPI("test-key", client=client)

    assert adapter.details("missing") is None


def test_places_api_canonicalizes_regular_hours_and_retries_throttling():
    calls = 0
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "fsq_place_id": "fsq-2",
                "name": "Night Owl",
                "latitude": 37.79,
                "longitude": -122.39,
                "location": {
                    "address": "2 Market St",
                    "locality": "San Francisco",
                    "region": "CA",
                    "country": "US",
                },
                "categories": [{"id": "13009", "name": "Cocktail Bar"}],
                "hours": {
                    "regular": [
                        {"day": 5, "open": "1600", "close": "+0200"},
                        {"day": 6, "open": "1600", "close": "0200"},
                    ]
                },
                "veracity_rating": 5,
            },
        )

    client = httpx.Client(
        base_url="https://places-api.foursquare.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = FoursquarePlacesAPI(
        "test-key",
        client=client,
        sleeper=delays.append,
    )

    record = adapter.details("fsq-2")

    assert calls == 2
    assert delays == [0.0]
    assert record is not None
    assert record.hours == {
        "friday": [["16:00", "+02:00"]],
        "saturday": [["16:00", "02:00"]],
    }


def test_places_api_reports_sanitized_provider_error_after_retries():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "0"},
            json={"error": {"message": "Billing must be enabled"}},
            request=request,
        )

    client = httpx.Client(
        base_url="https://places-api.foursquare.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = FoursquarePlacesAPI(
        "secret-key-that-must-not-leak",
        client=client,
        sleeper=lambda _: None,
    )

    with pytest.raises(
        RuntimeError,
        match="Foursquare Places API returned HTTP 429: Billing must be enabled",
    ) as error:
        adapter.details("fsq-2")

    assert calls == 3
    assert "secret-key-that-must-not-leak" not in str(error.value)
    assert "fsq-2" not in str(error.value)


def test_places_api_rejects_a_success_response_without_identity_fields():
    client = httpx.Client(
        base_url="https://places-api.foursquare.com",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={}, request=request)
        ),
    )
    adapter = FoursquarePlacesAPI("test-key", client=client)

    with pytest.raises(ValueError, match="lacked required identity fields"):
        adapter.details("broken")


def test_places_api_cannot_verify_a_restaurant_with_secondary_bar_category():
    adapter = FoursquarePlacesAPI(
        "test-key",
        storage_policy="contract",
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )

    record = adapter._to_record(
        {
            "fsq_place_id": "restaurant-bar",
            "name": "Osha Thai Restaurant & Lounge",
            "latitude": 37.79,
            "longitude": -122.39,
            "location": {
                "address": "4 Embarcadero Ctr",
                "locality": "San Francisco",
                "region": "CA",
                "country": "US",
            },
            "categories": [
                {
                    "id": "thai",
                    "label": "Dining and Drinking > Restaurant > Asian Restaurant > Thai Restaurant",
                },
                {
                    "id": "cocktails",
                    "label": "Dining and Drinking > Bar > Cocktail Bar",
                },
            ],
            "hours": {"friday": [["16:00", "02:00"]]},
            "veracity_rating": 5,
        }
    )

    assert record is not None
    assert record.consumer_facing is False
    assert record.public_access == "unknown"
