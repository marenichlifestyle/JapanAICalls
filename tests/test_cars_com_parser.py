from app.services.cars_com_parser import parse_cars_com_deterministic


def test_cars_com_parser_extracts_core_fields() -> None:
    html = """
    <html>
      <head>
        <title>2023 Tesla Model Y Performance Dual Motor All-Wheel Drive | Cars.com</title>
        <script type="application/ld+json">
        {
          "@context":"https://schema.org",
          "@type":"Product",
          "name":"2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
          "vehicleIdentificationNumber":"7SAYGDEF1PF795089",
          "offers":{"price":"28972","priceCurrency":"USD"},
          "seller":{
            "@type":"AutoDealer",
            "name":"Continental Toyota",
            "url":"https://www.continentaltoyota.com",
            "address":{
              "streetAddress":"6701 South La Grange Road",
              "addressLocality":"Hodgkins",
              "addressRegion":"IL",
              "postalCode":"60525"
            }
          }
        }
        </script>
      </head>
      <body>
        <div>Price: $28,972</div>
        <div>Mileage: 67,157 mi</div>
        <div>VIN: 7SAYGDEF1PF795089</div>
        <div>Stock #: P11988A</div>
        <a href="https://www.continentaltoyota.com/used/Tesla/2023-Tesla-Model-Y-0c94d8c2.htm">See vehicle on dealership website</a>
        <a href="https://www.continentaltoyota.com/">Dealer website</a>
      </body>
    </html>
    """
    text = """
    2023 Tesla Model Y Performance Dual Motor All-Wheel Drive
    Price $28,972
    Mileage 67,157 mi
    VIN 7SAYGDEF1PF795089
    Stock # P11988A
    Dealer Continental Toyota
    Address 6701 South La Grange Road, Hodgkins, IL 60525
    """
    result = parse_cars_com_deterministic(
        "https://www.cars.com/vehicledetail/0c94d8c2-659a-44f7-871e-4392a355428a/",
        html,
        text,
    )
    assert result.source == "cars.com"
    assert result.vehicle_title == "2023 Tesla Model Y Performance Dual Motor All-Wheel Drive"
    assert result.dealer == "Continental Toyota"
    assert "6701 South La Grange Road" in (result.dealer_address or "")
    assert result.vin == "7SAYGDEF1PF795089"
    assert result.stock_number == "P11988A"
    assert result.price_total_jpy == 28972
    assert result.mileage == "67,157 mi"
    assert result.dealer_website_url == "https://www.continentaltoyota.com"
    assert result.dealer_vehicle_url == "https://www.continentaltoyota.com/used/Tesla/2023-Tesla-Model-Y-0c94d8c2.htm"
    assert result.phone_from_listing is None
