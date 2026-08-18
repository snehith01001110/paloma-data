import httpx

from paloma_data.adapters.neighborhoods import OvertureNeighborhoodAdapter
from paloma_data.adapters.osm import OSMAttributeAdapter
from paloma_data.attribute_enrichment import CatalogPlace, _match_osm, _place_grid


class _OverpassClient:
    def __init__(self):
        self.endpoints = []

    def post(self, endpoint, **_):
        self.endpoints.append(endpoint)
        request = httpx.Request("POST", endpoint)
        if len(self.endpoints) == 1:
            return httpx.Response(504, request=request)
        return httpx.Response(200, request=request, json={"elements": []})


def test_osm_observation_extracts_hours_phone_and_objective_settings():
    observation = OSMAttributeAdapter("-123.2,36.8,-121.1,38.9")._to_observation(
        {
            "type": "node",
            "id": 123,
            "lat": 37.7901,
            "lon": -122.3901,
            "timestamp": "2026-08-01T12:00:00Z",
            "tags": {
                "name": "The Public House",
                "contact:phone": "+1 415 555 1212",
                "opening_hours": "Mo-Su 16:00-02:00",
                "outdoor_seating": "garden",
                "location": "rooftop",
            },
        }
    )

    assert observation is not None
    assert observation.source_record_id == "node/123"
    assert observation.hours == "Mo-Su 16:00-02:00"
    assert observation.setting_slugs == ("garden", "outdoor_patio", "rooftop")


def test_osm_snapshot_fails_over_after_a_transient_primary_error():
    adapter = OSMAttributeAdapter("-123.2,36.8,-121.1,38.9")
    client = _OverpassClient()

    assert adapter._fetch_payload(client) == {"elements": []}
    assert client.endpoints == [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    ]


def test_osm_attribute_match_requires_identity_and_nearby_location():
    place = CatalogPlace(
        id="venue-1",
        name="The Public House",
        normalized_name="the public house",
        latitude=37.79,
        longitude=-122.39,
        phone_e164=None,
        website_url=None,
    )
    observation = OSMAttributeAdapter("-123.2,36.8,-121.1,38.9")._to_observation(
        {
            "type": "node",
            "id": 123,
            "lat": 37.7901,
            "lon": -122.3901,
            "tags": {"name": "The Public House", "opening_hours": "Fr-Sa 16:00-02:00"},
        }
    )

    assert observation is not None
    match = _match_osm(observation, _place_grid([place]))
    assert match is not None
    assert match[0].id == "venue-1"
    assert match[1] == 0.94


def test_overture_neighborhood_boundary_parses_only_polygon_subtypes():
    feature = {
        "type": "Feature",
        "id": "hood-1",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-122.5, 37.7], [-122.4, 37.7], [-122.4, 37.8], [-122.5, 37.7]]],
        },
        "properties": {
            "subtype": "neighborhood",
            "names": {"primary": "Mission"},
            "sources": [{"update_time": "2026-07-01T00:00:00Z"}],
        },
    }

    boundary = OvertureNeighborhoodAdapter("-123.2,36.8,-121.1,38.9")._to_boundary(feature)
    assert boundary is not None
    assert boundary.name == "Mission"
    assert boundary.subtype == "neighborhood"
