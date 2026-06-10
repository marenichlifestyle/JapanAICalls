from app.services.carsensor_parser import parse_deterministic
from app.utils.text import clean_text_from_html
from app.utils.spoken import jpy_to_spoken_ru


def test_carsensor_fixture_parse() -> None:
    html = """
    <html>
      <head>
        <title>Mercedes-Benz E-Class E200 Avantgarde ISG MP202502 | カーセンサー</title>
      </head>
      <body>
        <div>支払総額 713.8万円</div>
        <div>車両本体価格 698.0万円</div>
        <div>販売店 Tokyo Auto</div>
        <div>住所 Tokyo</div>
        <div>年式 2022</div>
        <div>走行距離 1.2万km</div>
        <div>修復歴 なし</div>
        <div>車検 2027/04</div>
        <a href='tel:0438-41-1300'>0438-41-1300</a>
      </body>
    </html>
    """
    text = clean_text_from_html(html)
    result = parse_deterministic("https://www.carsensor.net/usedcar/detail/AU6925987049/", html, text)

    assert result.car_short == "Mercedes-Benz E-Class E200 Avantgarde"
    assert result.price_total_jpy == 7_138_000
    assert result.vehicle_price_jpy == 6_980_000
    assert result.price_used_jpy == 7_138_000
    assert result.price_used_type == "total_price"
    assert jpy_to_spoken_ru(result.price_used_jpy) == "семь миллионов сто тридцать восемь тысяч иен"


def test_carsensor_split_decimal_keeps_main_price() -> None:
    html = """
    <html>
      <head>
        <title>BMW 5 Series 523i | カーセンサー</title>
      </head>
      <body>
        <div>支払総額</div>
        <div>（税込）</div>
        <div>569</div>
        <div>.8</div>
        <div>万円</div>
        <div>（諸費用8万円含む）</div>
        <div>車両本体価格</div>
        <div>（税込）</div>
        <div>561</div>
        <div>.8</div>
        <div>万円</div>
        <div>販売店 Test Dealer</div>
        <div>住所 Tokyo</div>
        <a href='tel:0438-41-1300'>0438-41-1300</a>
        <div>他県在庫 支払総額 243.8 万円 本体価格 234.6 万円</div>
      </body>
    </html>
    """
    text = clean_text_from_html(html)
    result = parse_deterministic("https://www.carsensor.net/usedcar/detail/AU6958648985/index.html", html, text)

    assert result.price_total_jpy == 5_698_000
    assert result.vehicle_price_jpy == 5_618_000
    assert result.price_used_jpy == 5_698_000


def test_carsensor_proxy_phone_kept_as_listing_raw() -> None:
    html = """
    <html>
      <head>
        <title>BMW Premium Selection 札幌東 | カーセンサー</title>
      </head>
      <body>
        <div>支払総額 713.8万円</div>
        <div>販売店 Sapporo-Higashi BMW BMW Premium Selection 札幌東</div>
        <div>住所 北海道札幌市東区東苗穂3条3丁目2-15</div>
        <div>営業時間 10:00-18:00</div>
        <div>定休日 水曜日</div>
        <a href='tel:0078-6002-648302'>0078-6002-648302</a>
      </body>
    </html>
    """
    text = clean_text_from_html(html)
    result = parse_deterministic("https://www.carsensor.net/usedcar/detail/AU6938672228/index.html", html, text)
    assert result.phone_from_listing == "0078-6002-648302"
    assert result.carsensor_free_phone == "0078-6002-648302"
    assert result.dealer_direct_phone is None
