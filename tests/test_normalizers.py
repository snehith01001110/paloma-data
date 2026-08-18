from paloma_data.normalizers import (
    consumer_display_name,
    normalize_address,
    normalize_name,
    normalize_phone,
    normalize_url,
)


def test_name_normalization():
    assert normalize_name("  The Alembic, LLC ") == "the alembic"
    assert normalize_name("A & B") == "a and b"


def test_consumer_display_name_removes_only_a_terminal_corporate_suffix():
    assert consumer_display_name("2211 Club, Llc") == "2211 Club"
    assert consumer_display_name("Bar Corporation") == "Bar"
    assert consumer_display_name("The Company Bar") == "The Company Bar"


def test_address_normalization():
    assert normalize_address("1725 Haight Street") == "1725 haight st"
    assert normalize_address("100 North Main Avenue, Suite 2") == "100 n main ave ste 2"


def test_phone_normalization():
    assert normalize_phone("(415) 555-1212") == "+14155551212"


def test_url_normalization():
    assert normalize_url("www.example.com/?utm_source=x") == "https://example.com"
