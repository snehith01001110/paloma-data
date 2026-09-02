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


def test_explicit_brewpub_name_refines_generic_brewery_category():
    classification = classify_overture(
        "Lost Marbles Brewpub",
        {"dining_and_drinking", "brewery"},
        0.99,
    )

    assert classification.primary_type_slug == "brewpub"
    assert classification.reason == "overture_taxonomy:brewery+name_brewpub"


def test_generic_brewing_name_does_not_refine_brewery_access():
    classification = classify_overture(
        "Standard Deviant Brewing",
        {"dining_and_drinking", "brewery"},
        0.99,
    )

    assert classification.primary_type_slug == "brewery"


def test_explicit_tasting_room_name_refines_generic_winery_category():
    classification = classify_overture(
        "Hestan Vineyards Tasting Room",
        {"dining_and_drinking", "winery"},
        0.99,
    )

    assert classification.primary_type_slug == "tasting_room"


def test_explicit_pool_hall_category_is_a_billiards_bar():
    classification = classify_overture(
        "Crown Billiards",
        {"arts_and_entertainment", "pool_hall", "bar"},
        0.99,
    )

    assert classification.primary_type_slug == "billiards_bar"
    assert classification.reason == "overture_taxonomy:pool_hall"
