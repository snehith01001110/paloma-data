import httpx

from paloma_data.neighborhoods import DataSFNeighborhoodAdapter


def test_datasf_neighborhood_adapter_requires_valid_named_polygons():
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Mission", "link": "mission"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-122.43, 37.75],
                            [-122.40, 37.75],
                            [-122.40, 37.78],
                            [-122.43, 37.75],
                        ]
                    ],
                },
            },
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Point", "coordinates": [-122.4, 37.7]},
            },
        ],
    }
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=payload,
                headers={"Last-Modified": "Sun, 17 Aug 2026 20:00:00 GMT"},
                request=request,
            )
        )
    )

    with DataSFNeighborhoodAdapter("https://example.test/neighborhoods", client=client) as adapter:
        rows = list(adapter.boundaries())

    assert len(rows) == 1
    assert rows[0].name == "Mission"
    assert rows[0].source_record_id == "mission"
    assert rows[0].jurisdiction == "San Francisco"
    assert rows[0].source_updated_at is not None
