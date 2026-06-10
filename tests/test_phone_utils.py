from app.utils.phone import (
    classify_listing_phone,
    classify_us_phone,
    normalize_jp_phone_to_e164,
    normalize_us_phone_to_e164,
)


def test_proxy_number_0078_not_converted() -> None:
    assert classify_listing_phone("0078-6002-648302") == "proxy_or_special"
    assert normalize_jp_phone_to_e164("0078-6002-648302") is None


def test_landline_converted_to_e164() -> None:
    assert classify_listing_phone("011-780-1184") == "normal"
    assert normalize_jp_phone_to_e164("011-780-1184") == "+81117801184"


def test_mobile_converted_to_e164() -> None:
    assert classify_listing_phone("090-1234-5678") == "normal"
    assert normalize_jp_phone_to_e164("090-1234-5678") == "+819012345678"


def test_special_prefixes_rejected() -> None:
    for value in ["0120-111-222", "0800-111-222", "0570-111-222"]:
        assert classify_listing_phone(value) == "proxy_or_special"
        assert normalize_jp_phone_to_e164(value) is None


def test_us_phone_normalization_formats() -> None:
    assert classify_us_phone("(708) 716-4497") == "normal"
    assert normalize_us_phone_to_e164("(708) 716-4497") == "+17087164497"
    assert normalize_us_phone_to_e164("708-716-4497") == "+17087164497"
    assert normalize_us_phone_to_e164("1 708 716 4497") == "+17087164497"
