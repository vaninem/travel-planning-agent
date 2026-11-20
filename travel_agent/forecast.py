"""Destination-local forecasts from Open-Meteo's public forecast endpoint."""

from datetime import UTC, date, datetime, timedelta
from math import isfinite
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .domain import ProviderError
from .providers import _fetch


FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 16
WEATHER = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "中等毛毛雨", 55: "强毛毛雨", 56: "小冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "小冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "中等阵雨", 82: "强阵雨", 85: "小阵雪", 86: "强阵雪",
    95: "雷雨", 96: "雷雨伴小冰雹", 97: "强雷雨", 99: "雷雨伴强冰雹",
}
DAILY_FIELDS = "weather_code,temperature_2m_min,temperature_2m_max,precipitation_probability_max"


def _numeric_at(daily: dict, variable: str, index: int) -> float | int | None:
    values = daily.get(variable)
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    return value if type(value) in (float, int) and isfinite(value) else None


def _is_iso_date(value: object) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _open_meteo(client: httpx.Client, *, lng: float, lat: float, dates: list[str]) -> list[dict]:
    """Return available daily forecasts in requested order. Missing days stay absent."""
    wanted = list(dict.fromkeys(value for value in dates if _is_iso_date(value)))
    if not wanted:
        return []
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise ValueError("天气查询需要有效的经纬度")

    params = {
        "latitude": round(lat, 5), "longitude": round(lng, 5), "timezone": "auto",
        "forecast_days": FORECAST_DAYS, "past_days": 0, "temperature_unit": "celsius", "daily": DAILY_FIELDS,
    }
    request = client.build_request("GET", FORECAST_URL, params=params, timeout=7)
    payload = _fetch("Open-Meteo", lambda: client.send(request))
    daily = payload.get("daily")
    if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
        raise ProviderError("forecast_payload", "天气服务没有返回逐日预报", retryable=True)

    timezone = payload.get("timezone")
    try:
        local_zone = ZoneInfo(timezone) if isinstance(timezone, str) else None
    except (ValueError, ZoneInfoNotFoundError):
        local_zone = None
    if local_zone is None:
        raise ProviderError("forecast_timezone", "天气服务没有返回有效的目的地时区", retryable=True)

    retrieved = datetime.now(UTC)
    today = retrieved.astimezone(local_zone).date()
    window_end = today + timedelta(days=FORECAST_DAYS - 1)
    wanted = [day for day in wanted if today <= date.fromisoformat(day) <= window_end]
    positions = {day: index for index, day in enumerate(daily["time"]) if _is_iso_date(day)}
    result = []
    for day in wanted:
        if day not in positions:
            continue
        index = positions[day]
        code = _numeric_at(daily, "weather_code", index)
        low = _numeric_at(daily, "temperature_2m_min", index)
        high = _numeric_at(daily, "temperature_2m_max", index)
        if code not in WEATHER and low is None and high is None:
            continue
        chance = _numeric_at(daily, "precipitation_probability_max", index)
        result.append({
            "date": day, "summary": WEATHER.get(code, "天气状况未提供"), "low_c": low, "high_c": high,
            "rain_chance": round(chance) if chance is not None and 0 <= chance <= 100 else None,
            "timezone": timezone, "as_of": retrieved.isoformat(timespec="seconds"),
            "source_name": "Open-Meteo", "source_url": str(request.url), "evidence": "open_meteo",
        })
    return result


def _nws(client: httpx.Client, *, lng: float, lat: float, dates: list[str]) -> list[dict]:
    headers = {"User-Agent": "TravelPlanningPreview/1.0 (local itinerary forecast)", "Accept": "application/geo+json"}
    point = _fetch("NWS", lambda: client.get(f"https://api.weather.gov/points/{lat:.4f},{lng:.4f}", headers=headers, timeout=7))
    properties = point.get("properties") or {}
    url = properties.get("forecast")
    if not isinstance(url, str):
        return []
    info = urlsplit(url)
    if info.scheme != "https" or info.netloc != "api.weather.gov" or not info.path.startswith("/gridpoints/"):
        return []
    payload = _fetch("NWS", lambda: client.get(url, headers=headers, timeout=7))
    data = payload.get("properties") or {}
    records = {}
    for period in data.get("periods") or []:
        try:
            start = datetime.fromisoformat(period["startTime"])
        except (TypeError, ValueError, KeyError):
            continue
        day = start.date().isoformat()
        if day not in dates or start.tzinfo is None or start.date() < datetime.now(start.tzinfo).date():
            continue
        record = records.setdefault(day, {"date": day, "summary": "", "low_c": None, "high_c": None,
            "rain_chance": None, "source_name": "美国国家气象局", "source_url": url, "evidence": "nws",
            "as_of": data.get("updateTime") or data.get("updated"), "timezone": properties.get("timeZone")})
        if not record["summary"] or period.get("isDaytime"):
            record["summary"] = period.get("shortForecast") or ""
        temperature = period.get("temperature")
        if type(temperature) in (int, float) and isfinite(temperature):
            celsius = (temperature - 32) * 5 / 9 if period.get("temperatureUnit") == "F" else temperature
            record["high_c" if period.get("isDaytime") else "low_c"] = round(celsius)
        chance = (period.get("probabilityOfPrecipitation") or {}).get("value")
        if type(chance) in (int, float) and 0 <= chance <= 100:
            record["rain_chance"] = max(record["rain_chance"] or 0, round(chance))
    return [records[day] for day in dict.fromkeys(dates) if day in records]


def fetch_daily_forecast(client: httpx.Client, *, lng: float, lat: float, dates: list[str]) -> list[dict]:
    try:
        result = _open_meteo(client, lng=lng, lat=lat, dates=dates)
    except ProviderError:
        if -130 <= lng <= -60 and 24 <= lat <= 50:
            return _nws(client, lng=lng, lat=lat, dates=dates)
        raise
    return result
