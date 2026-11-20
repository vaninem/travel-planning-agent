from datetime import UTC, datetime

import httpx
import pytest

from travel_agent import forecast
from travel_agent.domain import ProviderError


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 17, 5, 30, tzinfo=UTC).astimezone(tz)


def test_forecast_uses_destination_dates_and_celsius(monkeypatch):
    monkeypatch.setattr(forecast, "datetime", FixedClock)

    def respond(request):
        assert request.url.host == "api.open-meteo.com"
        assert request.url.params["timezone"] == "auto"
        assert request.url.params["temperature_unit"] == "celsius"
        assert request.url.params["past_days"] == "0"
        return httpx.Response(200, json={
            "timezone": "America/Los_Angeles",
            "daily": {
                "time": ["2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18", "2026-12-02"],
                "weather_code": [3, 61, 2, None, 3],
                "temperature_2m_min": [1, 5.2, 4.8, None, 2],
                "temperature_2m_max": [8, 11.1, 10.0, None, 8],
                "precipitation_probability_max": [20, 71, 8, None, 30],
            },
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        days = forecast.fetch_daily_forecast(client, lng=-122.33, lat=47.61, dates=[
            "2026-11-17", "2026-11-16", "2026-11-15", "2026-11-18", "2026-12-02", "invalid", "2026-11-17",
        ])
    assert [item["date"] for item in days] == ["2026-11-17", "2026-11-16"]
    assert days[1]["summary"] == "小雨"
    assert (days[1]["low_c"], days[1]["high_c"], days[1]["rain_chance"]) == (5.2, 11.1, 71)
    assert days[1]["timezone"] == "America/Los_Angeles"
    assert days[1]["as_of"] == "2026-11-17T05:30:00+00:00"
    assert days[1]["source_url"].startswith("https://api.open-meteo.com/v1/forecast?")


def test_forecast_network_failure_is_retryable():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        with pytest.raises(ProviderError) as error:
            forecast.fetch_daily_forecast(client, lng=-122.33, lat=47.61, dates=["2026-11-16"])
    assert error.value.retryable
