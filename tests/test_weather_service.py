from app.services.weather_service import WeatherService


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_coordinate_label_is_replaced_with_city_and_forecast_narrative(monkeypatch):
    forecast_payload = {
        'current': {
            'temperature_2m': 55.2,
            'relative_humidity_2m': 73,
            'weather_code': 61,
            'wind_speed_10m': 8.4,
            'apparent_temperature': 53.8,
            'time': '2026-10-16T18:00',
        },
        'daily': {
            'time': ['2026-10-16'],
            'weather_code': [61],
            'temperature_2m_max': [59.8],
            'temperature_2m_min': [44.6],
            'precipitation_probability_max': [72],
        },
    }
    reverse_payload = {
        'address': {
            'town': 'Marblehead',
            'state': 'Ohio',
            'ISO3166-2-lvl4': 'US-OH',
        },
    }

    def fake_get(url, **kwargs):
        if url == WeatherService.OPEN_METEO_FORECAST_API:
            return _FakeResponse(forecast_payload)
        if url == WeatherService.NOMINATIM_REVERSE_GEOCODING_API:
            assert kwargs['headers']['User-Agent'] == 'HomeHub/1.0'
            return _FakeResponse(reverse_payload)
        raise AssertionError(f'Unexpected URL: {url}')

    monkeypatch.setattr('app.services.weather_service.requests.get', fake_get)

    weather = WeatherService.fetch_weather(41.95, -81.49, '41.95, -81.49')

    assert weather is not None
    assert weather.location_name == 'Marblehead, OH'
    assert weather.narrative == (
        'It is 55 degrees Fahrenheit with slight rain. '
        "Today's high is 60 degrees with a low of 45. "
        'Rain or snow is likely, with a 72% chance of precipitation.'
    )
    assert weather.daily_forecast[0].precipitation_probability == 72


def test_reverse_geocode_uses_city_and_full_state_when_no_us_code(monkeypatch):
    monkeypatch.setattr(
        'app.services.weather_service.requests.get',
        lambda *args, **kwargs: _FakeResponse({
            'address': {'city': 'Cuyahoga Falls', 'state': 'Ohio'},
        }),
    )

    assert WeatherService.reverse_geocode(41.13, -81.48) == 'Cuyahoga Falls, Ohio'
