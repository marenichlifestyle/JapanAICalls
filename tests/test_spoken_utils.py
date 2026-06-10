from app.utils.spoken import (
    car_name_to_spoken_ru,
    compact_car_name_for_call,
    compact_intro_car_spoken,
    ensure_brand_in_car_name,
    ensure_brand_in_spoken,
    ensure_ien_in_spoken_price,
    infer_brand_spoken_prefix,
    jpy_to_spoken_ru,
    normalize_model_year,
    normalize_spoken_text,
    spoken_has_brand,
    validate_price_spoken_text,
)


def test_spoken_price_7138000() -> None:
    assert jpy_to_spoken_ru(7_138_000) == "семь миллионов сто тридцать восемь тысяч иен"


def test_spoken_price_6980000() -> None:
    assert jpy_to_spoken_ru(6_980_000) == "шесть миллионов девятьсот восемьдесят тысяч иен"


def test_car_spoken() -> None:
    assert (
        car_name_to_spoken_ru("Mercedes-Benz E-Class E200 Avantgarde")
        == "мерседес бенц е класс е двести авангард"
    )


def test_compact_car_name_removes_options_tail() -> None:
    value = "BMW 523i Exclusive Rent UP Select Paket Panoramic Sunroof Comfort"
    assert compact_car_name_for_call(value) == "BMW 523i Exclusive"


def test_compact_car_name_dedup_brand() -> None:
    value = "MINI MINI Cooper S 3Doors Premium Plus Package"
    assert compact_car_name_for_call(value) == "MINI Cooper S 3Doors"


def test_bmw_523i_spoken() -> None:
    assert car_name_to_spoken_ru("BMW 523i Exclusive") == "бэ эм вэ пятьсот двадцать три ай эксклюзив"


def test_compact_spoken_bmw_long_tail() -> None:
    value = (
        "бэ эм вэ пятая серия пятьсот двадцать три ай эксклюзив рента ап селект пакет "
        "панорама гласс санруф плюс пакет комфорт"
    )
    assert compact_car_name_for_call(value, max_tokens=12) == "бэ эм вэ пятая серия пятьсот двадцать три ай эксклюзив"


def test_compact_spoken_mini_long_tail() -> None:
    value = (
        "мини мини купер эс три двери премиум плюс пакет ди си ти "
        "сертифицированный подержанный"
    )
    assert compact_car_name_for_call(value, max_tokens=12) == "мини купер эс три двери"


def test_ensure_brand_from_full_name() -> None:
    full = "BMW 5 Series 523i Exclusive"
    short_without_brand = "5 Series 523i"
    assert ensure_brand_in_car_name(short_without_brand, full) == "BMW 5 Series 523i"


def test_ensure_brand_no_duplicate() -> None:
    full = "BMW 5 Series 523i Exclusive"
    short_with_brand = "BMW 5 Series 523i"
    assert ensure_brand_in_car_name(short_with_brand, full) == "BMW 5 Series 523i"


def test_brand_detection_from_japanese_name() -> None:
    full = "メルセデス・ベンツ GLS 400 d 4マチック AMGライン"
    assert infer_brand_spoken_prefix(full) == "мерседес бенц"


def test_ensure_brand_in_spoken_from_japanese_name() -> None:
    full = "メルセデス・ベンツ GLS 400 d 4マチック AMGライン"
    spoken = "четыреста"
    fixed = ensure_brand_in_spoken(spoken, full)
    assert "мерседес" in fixed
    assert "четыреста" in fixed
    assert spoken_has_brand(fixed, full) is True


def test_yen_variants_equivalent() -> None:
    a = "шесть миллионов девятьсот шестьдесят семь тысяч йен"
    b = "шесть миллионов девятьсот шестьдесят семь тысяч иен"
    assert normalize_spoken_text(a) == normalize_spoken_text(b)


def test_thousand_forms_equivalent() -> None:
    a = "четыре миллиона девятьсот семьдесят три тысяч иен"
    b = "четыре миллиона девятьсот семьдесят три тысячи иен"
    assert normalize_spoken_text(a) == normalize_spoken_text(b)


def test_spoken_price_with_yen_is_valid() -> None:
    ok, reason = validate_price_spoken_text(7_138_000, "семь миллионов сто тридцать восемь тысяч йен")
    assert ok is True
    assert reason == ""


def test_spoken_price_with_digits_invalid() -> None:
    ok, reason = validate_price_spoken_text(7_138_000, "7 миллионов иен")
    assert ok is False
    assert "latin letters or digits" in reason


def test_spoken_price_with_latin_invalid() -> None:
    ok, reason = validate_price_spoken_text(7_138_000, "seven миллионов иен")
    assert ok is False
    assert "latin letters or digits" in reason


def test_spoken_price_without_currency_is_normalized() -> None:
    ok, reason = validate_price_spoken_text(6_967_000, "шесть миллионов девятьсот шестьдесят семь тысяч")
    assert ok is True
    assert reason == ""
    assert (
        ensure_ien_in_spoken_price("шесть миллионов девятьсот шестьдесят семь тысяч")
        == "шесть миллионов девятьсот шестьдесят семь тысяч иен"
    )


def test_compact_intro_car_spoken_limits_to_tokens() -> None:
    value = (
        "мерседес бенц глс четыреста амг лайн панорама люк "
        "подогрев сидений эппл карплей"
    )
    assert compact_intro_car_spoken(value) == "мерседес бенц глс четыреста амг лайн панорама люк"


def test_compact_intro_car_spoken_dedups_brand() -> None:
    value = "MINI MINI Cooper S Premium Plus Package"
    assert compact_intro_car_spoken(value) == "MINI Cooper S Premium Plus Package"


def test_compact_intro_car_spoken_japanese_token_limit() -> None:
    value = "メルセデス ベンツ GLS 400 d AMG ライン ワンオーナー サンルーフ"
    assert compact_intro_car_spoken(value) == "メルセデス ベンツ GLS 400 d AMG ライン ワンオーナー"


def test_normalize_model_year_extracts_yyyy() -> None:
    assert normalize_model_year("две тысячи двадцать шестой (2026)") == "2026"
    assert normalize_model_year("2021年") == "2021"
