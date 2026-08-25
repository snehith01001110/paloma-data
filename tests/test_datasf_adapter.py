import httpx

import paloma_data.adapters.datasf as datasf_module
from paloma_data.adapters.datasf import DataSFAdapter


def test_datasf_analysis_neighborhood_is_preserved_as_a_claim():
    record = DataSFAdapter()._to_record(
        {
            "uniqueid": "123",
            "dba_name": "Example Wine Bar",
            "full_business_address": "1 Market St",
            "city": "San Francisco",
            "state": "CA",
            "naics_code": "722410",
            "neighborhoods_analysis_boundaries": "Financial District/South Beach",
            "data_as_of": "2026-08-17T00:00:00.000",
            "location": {"type": "Point", "coordinates": [-122.39, 37.79]},
        }
    )

    assert record is not None
    assert record.neighborhood == "Financial District/South Beach"


def test_datasf_page_retries_a_transient_read_timeout(monkeypatch):
    attempts = 0
    delays: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("transient DataSF timeout", request=request)
        return httpx.Response(200, json=[{"uniqueid": "123"}], request=request)

    monkeypatch.setattr(datasf_module, "sleep", delays.append)
    adapter = DataSFAdapter(page_size=10)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = adapter._fetch_page(client, 20)

    assert rows == [{"uniqueid": "123"}]
    assert attempts == 2
    assert delays == [1]


def test_datasf_page_does_not_retry_a_permanent_http_error(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, request=request)

    monkeypatch.setattr(datasf_module, "sleep", lambda _seconds: None)
    adapter = DataSFAdapter()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            adapter._fetch_page(client, 0)
        except httpx.HTTPStatusError:
            pass
        else:
            raise AssertionError("expected the permanent DataSF error to propagate")

    assert attempts == 1
