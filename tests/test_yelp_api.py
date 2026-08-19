import httpx
import pytest

from paloma_data.adapters.yelp import (
    YelpAPIError,
    YelpMatchInput,
    YelpPlacesAPI,
    select_yelp_business_match,
)


EXPECTED = YelpMatchInput(
    establishment_id="00000000-0000-0000-0000-000000000001",
    name="Dogpatch Saloon",
    address="2496 3rd St",
    city="San Francisco",
    region="CA",
    postal_code="94107",
    country_code="US",
    latitude=37.757963,
    longitude=-122.388534,
    phone_e164="+14155551212",
)


def _business(**overrides):
    row = {
        "id": "WavvLdfdP6g8aZTtbBQHTw",
        "name": "Dogpatch Saloon",
        "is_closed": False,
        "coordinates": {"latitude": 37.75797, "longitude": -122.38853},
        "location": {"address1": "2496 3rd Street"},
        "phone": "+14155551212",
        "categories": [{"alias": "cocktailbars", "title": "Cocktail Bars"}],
    }
    row.update(overrides)
    return row


def test_selects_one_strong_identity_and_rejects_ambiguous_or_wrong_type():
    matched = select_yelp_business_match({"businesses": [_business()]}, EXPECTED)

    assert matched.outcome == "matched"
    assert matched.provider_place_id == "WavvLdfdP6g8aZTtbBQHTw"
    assert matched.confidence == 0.995

    ambiguous = select_yelp_business_match(
        {
            "businesses": [
                _business(),
                _business(id="AbcdLdfdP6g8aZTtbBQHTw"),
            ]
        },
        EXPECTED,
    )
    assert ambiguous.outcome == "ambiguous"

    wrong_type = select_yelp_business_match(
        {
            "businesses": [
                _business(categories=[{"alias": "thai", "title": "Thai Restaurant"}])
            ]
        },
        EXPECTED,
    )
    assert wrong_type.outcome == "rejected"
    assert wrong_type.reason == "rejected_type_mismatch"


def test_common_words_do_not_override_a_conflicting_consumer_name():
    result = select_yelp_business_match(
        {
            "businesses": [
                _business(
                    name="Dogpatch Wine Company",
                    phone=None,
                    location={"address1": "2500 3rd St"},
                )
            ]
        },
        EXPECTED,
    )

    assert result.outcome == "rejected"
    assert result.reason == "rejected_name_mismatch"


def test_client_uses_bounded_search_and_retries_throttling_without_leaking_content():
    calls = 0
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        assert request.url.params["term"] == "Dogpatch Saloon"
        assert request.url.params["radius"] == "500"
        assert request.url.params["limit"] == "5"
        return httpx.Response(200, json={"businesses": [_business()]}, request=request)

    client = httpx.Client(
        base_url="https://api.yelp.com/v3",
        transport=httpx.MockTransport(handler),
    )
    api = YelpPlacesAPI("secret-key", client=client, sleeper=delays.append)

    assert api.match(EXPECTED).outcome == "matched"
    assert calls == 2
    assert delays == [0.0]


def test_client_classifies_provider_failures_without_response_details():
    client = httpx.Client(
        base_url="https://api.yelp.com/v3",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                401,
                json={"error": {"description": "secret provider detail"}},
                request=request,
            )
        ),
    )
    api = YelpPlacesAPI("secret-key", client=client)

    with pytest.raises(YelpAPIError, match="unauthorized") as error:
        api.match(EXPECTED)

    assert "secret-key" not in str(error.value)
    assert "secret provider detail" not in str(error.value)


def test_details_audit_returns_only_identity_status_and_attribute_presence():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/businesses/WavvLdfdP6g8aZTtbBQHTw"
        return httpx.Response(
            200,
            json={**_business(), "hours": [{"open": []}], "price": "$$"},
            request=request,
        )

    client = httpx.Client(
        base_url="https://api.yelp.com/v3",
        transport=httpx.MockTransport(handler),
    )

    audit = YelpPlacesAPI("secret-key", client=client).audit_details(
        "WavvLdfdP6g8aZTtbBQHTw", EXPECTED
    )

    assert audit.identity_compatible
    assert audit.currently_operating
    assert audit.has_phone
    assert audit.has_hours
    assert audit.has_price
    assert not hasattr(audit, "phone")
    assert not hasattr(audit, "hours")
