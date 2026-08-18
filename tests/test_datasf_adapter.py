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
