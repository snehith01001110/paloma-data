from paloma_data.taxonomy import classify_overture


def test_winery_plus_wine_bar_is_an_access_gated_tasting_room():
    classification = classify_overture(
        "Hestan Vineyards Tasting Room",
        {"dining_and_drinking", "winery", "bar", "wine_bar"},
        0.99,
    )

    assert classification.primary_type_slug == "tasting_room"
    assert classification.reason == "overture_taxonomy:winery+wine_bar"


def test_brewery_plus_bar_is_an_access_gated_taproom():
    classification = classify_overture(
        "Laughing Monk Brewing",
        {"dining_and_drinking", "brewery", "bar", "beer_bar"},
        0.99,
    )

    assert classification.primary_type_slug == "taproom"
    assert classification.reason == "overture_taxonomy:brewery+bar"


def test_brewery_plus_restaurant_is_an_access_gated_brewpub():
    classification = classify_overture(
        "Southern Pacific Brewing",
        {"dining_and_drinking", "brewery", "restaurant", "american_restaurant"},
        0.99,
    )

    assert classification.primary_type_slug == "brewpub"
    assert classification.reason == "overture_taxonomy:brewery+restaurant"


def test_bare_manufacturer_category_remains_generic():
    classification = classify_overture(
        "Ogden Wine Co. LLC",
        {"dining_and_drinking", "winery"},
        0.99,
    )

    assert classification.primary_type_slug == "winery"
