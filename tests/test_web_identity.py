from paloma_data.web_identity import PageParser, _name_claims, _page_identity_score


def test_jsonld_local_business_name_is_high_confidence():
    parser = PageParser()
    parser.feed(
        '''
        <html><head>
        <script type="application/ld+json">
        {"@type":"Brewery","name":"Laughing Monk Brewing","address":{"streetAddress":"1235 Oakmead Parkway","addressLocality":"Sunnyvale","postalCode":"94085"},"telephone":"408-736-2739"}
        </script>
        </head><body>Laughing Monk Brewing 1235 Oakmead Parkway Sunnyvale CA 94085 (408) 736-2739</body></html>
        '''
    )
    claims = _name_claims(parser)
    assert any(claim.name == "Laughing Monk Brewing" and claim.confidence == 0.99 for claim in claims)


def test_page_must_match_establishment_identity_before_name_is_trusted():
    parser = PageParser()
    parser.feed(
        "<html><body>Laughing Monk Brewing 1235 Oakmead Parkway Sunnyvale CA 94085 (408) 736-2739</body></html>"
    )
    row = {
        "address": "1235 Oakmead Pkwy",
        "city": "Sunnyvale",
        "postal_code": "94085",
        "phone_e164": "+14087362739",
        "source_phones": [],
    }
    assert _page_identity_score(row, parser) >= 0.95


def test_unrelated_page_fails_identity_threshold():
    parser = PageParser()
    parser.feed("<html><body>Some Brewery 10 Other Street Oakland CA</body></html>")
    row = {
        "address": "1235 Oakmead Pkwy",
        "city": "Sunnyvale",
        "postal_code": "94085",
        "phone_e164": "+14087362739",
        "source_phones": [],
    }
    assert _page_identity_score(row, parser) < 0.75
