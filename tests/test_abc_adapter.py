from paloma_data.adapters.ca_abc import CaliforniaABCAdapter


def test_raw_export_layout_maps_to_source_record():
    row = {
        "License Type": "23",
        "File Number": "12345678",
        "License or Application": "LIC",
        "Type Status": "ACTIVE",
        "Type Original Issue Dates": "15-JAN-2024",
        "Expiration Dates": "31-JAN-2027",
        "Geo Code": "3800",
        "District/Office Code": "24",
        "Primary Name": "EXAMPLE BREWING LLC",
        "Premise Street Address 1": "123 MARKET ST",
        "Premise Street Address 2": "STE 100",
        "Premise City": "SAN FRANCISCO",
        "Premise State": "CA",
        "Premise Zip": "94105",
        "DBA Name": "EXAMPLE BREWING",
        "Premise County": "SAN FRANCISCO",
    }

    record = CaliforniaABCAdapter()._to_record(row)

    assert record is not None
    assert record.source_record_id == "12345678:23"
    assert record.name == "EXAMPLE BREWING"
    assert record.address == "123 MARKET ST STE 100"
    assert record.city == "SAN FRANCISCO"
    assert record.source_status == "open"
    assert record.primary_type_slug == "brewery"
    assert record.classification_confidence == 0.99


def test_pending_application_is_not_open():
    row = {
        "License Type": "42",
        "File Number": "87654321",
        "License or Application": "APP",
        "Type Status": "PEND",
        "Primary Name": "BAR OWNER LLC",
        "Premise Street Address 1": "456 VALENCIA ST",
        "Premise City": "SAN FRANCISCO",
        "Premise State": "CA",
        "Premise Zip": "94110",
        "DBA Name": "SAMPLE WINE BAR",
    }

    record = CaliforniaABCAdapter()._to_record(row)

    assert record is not None
    assert record.source_status == "pending"
    assert record.primary_type_slug == "wine_bar"
