from app.utils.timezone import resolve_office_timezone


def test_resolve_office_timezone_carsensor_is_japan_default() -> None:
    tz, reason = resolve_office_timezone(
        source="carsensor",
        dealer_address="北海道札幌市東区東苗穂3条3丁目2-15",
        listing_url="https://www.carsensor.net/usedcar/detail/AU1/",
        jp_default="Asia/Tokyo",
        us_fallback="America/New_York",
    )
    assert tz == "Asia/Tokyo"
    assert reason == "source_default_japan"


def test_resolve_office_timezone_cars_com_by_zip_and_state() -> None:
    tz, reason = resolve_office_timezone(
        source="cars.com",
        dealer_address="6701 South La Grange Road, Hodgkins, IL 60525",
        listing_url="https://www.cars.com/vehicledetail/abc/",
        jp_default="Asia/Tokyo",
        us_fallback="America/New_York",
    )
    assert tz == "America/Chicago"
    assert reason.startswith("zip_and_state:")


def test_resolve_office_timezone_cars_com_fallback_when_no_zip() -> None:
    tz, reason = resolve_office_timezone(
        source="cars.com",
        dealer_address="Some dealer address without postal code",
        listing_url="https://www.cars.com/vehicledetail/abc/",
        jp_default="Asia/Tokyo",
        us_fallback="America/New_York",
    )
    assert tz == "America/New_York"
    assert reason == "fallback_us_timezone"
