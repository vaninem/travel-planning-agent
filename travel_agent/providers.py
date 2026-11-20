"""固定样本、高德 Web 服务和原生工具调用模型的传输边界。"""

from datetime import date
import hashlib
import math
import os
from typing import Any

import httpx

from .domain import ProviderError, TripRequest
from .pricing import (
    BookingPricing, amap_source, booking_available, lodging_scope, lodging_search,
    official_admission, sample_admission, sample_lodging,
)


# 地图演示用固定数据。每条记录自带样本身份，线上地点由高德服务返回。
PLACES = {
    "上海": [
        ("上海博物馆", 121.4753, 31.2303, "博物馆"),
        ("上海自然博物馆", 121.4633, 31.2349, "博物馆"),
        ("上海城市规划展示馆", 121.4759, 31.2323, "展览馆"),
        ("豫园", 121.4922, 31.2276, "园林"),
        ("外滩", 121.4906, 31.2379, "夜景"),
        ("上海当代艺术博物馆", 121.4987, 31.2087, "美术馆"),
        ("新天地", 121.4754, 31.2189, "历史街区"),
        ("陆家嘴滨江", 121.5049, 31.2407, "滨水"),
    ],
    "北京": [
        ("故宫博物院", 116.3971, 39.9187, "文博建筑群"),
        ("中国国家博物馆", 116.4010, 39.9052, "博物馆"),
        ("首都博物馆", 116.3416, 39.9067, "博物馆"),
        ("中国美术馆", 116.4085, 39.9251, "美术馆"),
        ("北京天文馆", 116.3394, 39.9390, "科技馆"),
        ("景山公园", 116.3967, 39.9250, "公园"),
        ("天坛公园", 116.4108, 39.8829, "园林"),
        ("什刹海", 116.3856, 39.9429, "历史街区"),
    ],
    "杭州": [
        ("灵隐寺", 120.1007, 30.2400, "文博建筑群"),
        ("浙江省博物馆孤山馆区", 120.1404, 30.2536, "博物馆"),
        ("中国丝绸博物馆", 120.1478, 30.2224, "博物馆"),
        ("浙江美术馆", 120.1577, 30.2273, "美术馆"),
        ("杭州博物馆", 120.1673, 30.2401, "博物馆"),
        ("西湖断桥", 120.1479, 30.2594, "滨水"),
        ("河坊街", 120.1725, 30.2400, "历史街区"),
        ("花港观鱼", 120.1377, 30.2286, "园林"),
    ],
    "成都": [
        ("成都博物馆", 104.0639, 30.6575, "博物馆"),
        ("四川博物院", 104.0298, 30.6593, "博物馆"),
        ("四川科技馆", 104.0654, 30.6599, "科技馆"),
        ("成都天府美术馆", 104.0349, 30.7218, "美术馆"),
        ("人民公园", 104.0527, 30.6608, "公园"),
        ("宽窄巷子", 104.0517, 30.6652, "历史街区"),
        ("杜甫草堂", 104.0274, 30.6592, "园林"),
        ("成都大熊猫繁育研究基地", 104.1430, 30.7386, "动物园"),
    ],
    "西安": [
        ("陕西历史博物馆", 108.9528, 34.2250, "博物馆"),
        ("西安碑林博物馆", 108.9510, 34.2529, "博物馆"),
        ("西安博物院", 108.9378, 34.2374, "博物馆"),
        ("陕西科技馆", 108.9581, 34.2605, "科技馆"),
        ("西安城墙永宁门", 108.9442, 34.2514, "文博建筑群"),
        ("大雁塔北广场", 108.9632, 34.2230, "广场"),
        ("大唐芙蓉园", 108.9783, 34.2087, "园林"),
        ("西安钟楼", 108.9458, 34.2602, "文博建筑群"),
    ],
    "广州": [
        ("广东省博物馆", 113.3267, 23.1173, "博物馆"),
        ("广州博物馆", 113.2652, 23.1387, "博物馆"),
        ("广东美术馆二沙岛馆区", 113.3098, 23.1118, "美术馆"),
        ("南越王博物院王墓展区", 113.2606, 23.1408, "博物馆"),
        ("广州塔", 113.3246, 23.1065, "夜景"),
        ("沙面", 113.2422, 23.1047, "历史街区"),
        ("越秀公园", 113.2662, 23.1431, "公园"),
        ("海珠湖公园", 113.3322, 23.0715, "滨水"),
    ],
    "深圳": [
        ("深圳博物馆", 114.0601, 22.5458, "博物馆"),
        ("深圳关山月美术馆", 114.0615, 22.5512, "美术馆"),
        ("深圳科学馆", 114.0967, 22.5411, "科技馆"),
        ("南山博物馆", 113.9255, 22.5318, "博物馆"),
        ("莲花山公园", 114.0561, 22.5541, "公园"),
        ("深圳湾公园", 113.9894, 22.5136, "滨水"),
        ("南头古城", 113.9226, 22.5466, "历史街区"),
        ("欢乐海岸", 113.9934, 22.5200, "街区"),
    ],
    "南京": [
        ("南京博物院", 118.8171, 32.0414, "博物馆"),
        ("六朝博物馆", 118.7980, 32.0420, "博物馆"),
        ("南京市博物馆", 118.7768, 32.0355, "博物馆"),
        ("江苏省美术馆", 118.7956, 32.0448, "美术馆"),
        ("夫子庙秦淮风光带", 118.7881, 32.0204, "历史街区"),
        ("瞻园", 118.7848, 32.0214, "园林"),
        ("中山陵", 118.8487, 32.0656, "文博建筑群"),
        ("玄武湖", 118.8047, 32.0836, "滨水"),
    ],
    "苏州": [
        ("苏州博物馆", 120.6292, 31.3254, "博物馆"),
        ("苏州丝绸博物馆", 120.6227, 31.3284, "博物馆"),
        ("苏州园林博物馆", 120.6307, 31.3257, "博物馆"),
        ("苏州美术馆", 120.6226, 31.3345, "美术馆"),
        ("拙政园", 120.6318, 31.3258, "园林"),
        ("平江路", 120.6337, 31.3182, "历史街区"),
        ("留园", 120.5907, 31.3185, "园林"),
        ("金鸡湖", 120.7049, 31.3063, "滨水"),
    ],
    "重庆": [
        ("重庆中国三峡博物馆", 106.5516, 29.5635, "博物馆"),
        ("重庆美术馆", 106.5755, 29.5590, "美术馆"),
        ("重庆科技馆", 106.5801, 29.5699, "科技馆"),
        ("重庆大轰炸惨案遗址纪念馆", 106.5680, 29.5550, "博物馆"),
        ("重庆市人民大礼堂", 106.5544, 29.5637, "文博建筑群"),
        ("洪崖洞", 106.5787, 29.5612, "夜景"),
        ("磁器口古镇", 106.4448, 29.5837, "历史街区"),
        ("鹅岭公园", 106.5349, 29.5537, "公园"),
    ],
}

INDOOR = frozenset({"博物馆", "美术馆", "科技馆", "展览馆"})
OUTDOOR_HINTS = ("公园", "湿地", "广场", "滨江", "滨水", "园林", "街区", "动物园")
ALIASES = {
    "文化": {"博物馆", "文博建筑群", "历史街区", "美术馆"},
    "文博": {"博物馆", "文博建筑群", "历史街区", "美术馆"},
    "亲子": {"科技馆", "博物馆", "动物园", "公园"},
    "雨天": INDOOR,
    "室内": INDOOR,
    "夜景": {"夜景", "街区", "滨水"},
    "自然": {"滨水", "公园", "动物园", "园林"},
    "历史": {"博物馆", "文博建筑群", "历史街区"},
    "美食": {"历史街区", "街区"},
    "艺术": {"美术馆", "展览馆"},
}

AMAP_URL = "https://restapi.amap.com"
AMAP_RETRYABLE = {"10002", "10003", "10004", "10009", "10010", "10014", "10015", "10016", "10017", "10019", "10020", "10021", "10029"}


def city_key(city: str) -> str:
    return city.strip().removesuffix("市")


def poi_matches(place: dict, query: str) -> bool:
    if query in place["name"] or query in place.get("category", ""):
        return True
    return any(phrase in query and place.get("category") in categories for phrase, categories in ALIASES.items())


def distance_m(origin: dict, destination: dict) -> int:
    lon1, lat1, lon2, lat2 = map(math.radians, (origin["lng"], origin["lat"], destination["lng"], destination["lat"]))
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(12_742_000 * math.asin(min(1.0, math.sqrt(haversine))))


class FixtureProvider:
    def _places(self, city: str) -> list[dict]:
        city = city_key(city)
        return [
            {
                "poi_id": f"fixture-{list(PLACES).index(city)+1:02d}-{index:02d}",
                "name": name, "address": f"{city} · {name}", "city": city, "city_code": "",
                "lng": lng, "lat": lat, "category": category,
                "indoor": True if category in INDOOR else (None if category == "文博建筑群" else False),
                "evidence": "fixture",
                **amap_source(name, lng, lat), "source_name": "高德地图（示例点位）",
                "pricing": sample_admission({"city": city, "name": name, "category": category, "lng": lng, "lat": lat}),
            }
            for index, (name, lng, lat, category) in enumerate(PLACES.get(city, ()), 1)
        ]

    def search_poi(self, city: str, query: str) -> list[dict]:
        candidates = self._places(city)
        if query in {"景点", "旅游景点", "展览 景点", "热门景点"}:
            return candidates
        exact = [place for place in candidates if place["name"] == query]
        if exact:
            return exact
        return [place for place in candidates if poi_matches(place, query)]

    def search_hotels(self, city: str, *, request: TripRequest | None = None) -> list[dict]:
        places = self._places(city)
        if not places or (request is not None and not lodging_scope(request)[0]):
            return []
        anchor = places[0]
        city_slug = city_key(city)
        central_hotel = {
            "poi_id": f"{anchor['poi_id']}-hotel", "name": f"{city_slug}市中心酒店（演示候选）",
            "address": f"{city_slug}市中心（演示地点）", "city": city_slug, "city_code": "",
            "lng": round(anchor["lng"] + 0.0021, 6), "lat": round(anchor["lat"] + 0.0013, 6),
            "category": "酒店", "indoor": True, "evidence": "fixture",
        }
        if request is None:
            return [central_hotel]
        value_hotel = {
            **central_hotel,
            "poi_id": f"{anchor['poi_id']}-hotel-value", "name": f"{city_slug}经济住宿（演示候选）",
            "lng": round(anchor["lng"] - 0.0011, 6), "lat": round(anchor["lat"] + 0.0028, 6),
        }
        link = lodging_search(request)
        return [{**hotel, **link, "pricing": sample_lodging(request, room_rate)}
                for hotel, room_rate in ((central_hotel, 320), (value_hotel, 180))]

    def quote_attractions(self, visits: list[tuple[dict, str]]) -> dict[str, dict]:
        return {place["poi_id"]: price for place, visit_date in visits if (price := official_admission(place, visit_date))}

    def get_weather(self, city: str, dates: list[str]) -> list[dict]:
        def forecast(day: str) -> dict:
            digest = hashlib.sha256(f"{city_key(city)}:{day}".encode()).digest()
            center = (8, 10, 15, 19, 22, 26, 30, 29, 24, 18, 13, 9)[date.fromisoformat(day).month - 1]
            return {
                "date": day, "summary": ("晴", "多云", "小雨", "阴")[digest[0] % 4],
                "low_c": center - 4, "high_c": center + 4, "evidence": "fixture",
            }

        return [forecast(day) for day in dates] if city_key(city) in PLACES else []

    def get_route(self, city: str, transport: str, origin: dict, destination: dict) -> dict:
        length = math.ceil(distance_m(origin, destination) * 1.22)
        speed, overhead = {"walking": (80, 0), "driving": (470, 8), "transit": (380, 13)}[transport]
        return {
            "distance_m": length, "duration_minutes": max(1, math.ceil(length / speed) + overhead),
            "polyline": [[origin["lng"], origin["lat"]], [destination["lng"], destination["lat"]]],
            "evidence": "fixture",
        }


def _env(*names: str) -> str:
    return next((os.environ[name].strip() for name in names if os.environ.get(name, "").strip()), "")


def _fetch(service: str, action: Any) -> dict:
    try:
        response = action()
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise ProviderError(f"{service}_timeout", f"{service} 接口超时", retryable=True) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise ProviderError(f"{service}_http_{status}", f"{service} 返回 HTTP {status}", retryable=status in (408, 425, 429) or status >= 500) from exc
    except httpx.RequestError as exc:
        raise ProviderError(f"{service}_unavailable", f"无法连接 {service} 服务", retryable=True) from exc
    except ValueError as exc:
        raise ProviderError(f"{service}_payload", f"{service} 返回无法解析的数据", retryable=True) from exc
    if not isinstance(payload, dict):
        raise ProviderError(f"{service}_payload", f"{service} 返回缺少数据对象", retryable=True)
    return payload


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _location(value: object) -> list[float] | None:
    if isinstance(value, str):
        try:
            lng, lat = map(float, value.split(","))
            if -180 <= lng <= 180 and -90 <= lat <= 90:
                return [lng, lat]
        except ValueError:
            pass
    return None


def _integer(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _fare(value: object) -> float | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (ValueError, OverflowError):
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


class AMapProvider:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def _get(self, endpoint: str, params: dict) -> dict:
        key = _env("AMAP_WEB_KEY", "AMAP_API_KEY", "AMAP_KEY")
        if not key:
            raise ProviderError("amap_config", "请配置高德 Web 服务密钥 AMAP_WEB_KEY")
        payload = _fetch("AMap", lambda: self.client.get(f"{AMAP_URL}{endpoint}", params={**params, "key": key}))
        if str(payload.get("status")) != "1":
            code = str(payload.get("infocode", "unknown"))
            raise ProviderError(f"amap_{code}", f"高德服务返回错误 {code}", retryable=code in AMAP_RETRYABLE)
        return payload

    def _places(self, city: str, query: str, *, hotels: bool = False) -> list[dict]:
        params = {"region": city, "city_limit": "true", "page_size": 12}
        if hotels or query == "旅游景点":
            params["types"] = "100000" if hotels else "110000"
        else:
            params["keywords"] = query
        payload = self._get("/v5/place/text", params)
        accepted = []
        for item in _items(payload.get("pois")):
            location = _location(item.get("location"))
            poi_id, name, provider_city = map(_text, (item.get("id"), item.get("name"), item.get("cityname")))
            if not (location and poi_id and name) or (provider_city and city_key(provider_city) != city_key(city)):
                continue
            type_name = _text(item.get("type")).split(";")[-1]
            indoor = None
            if not any(hint in name for hint in ("故宫", "故居", "遗址", "园区")) and any(hint in name or hint in type_name for hint in INDOOR):
                indoor = True
            elif any(hint in name or hint in type_name for hint in OUTDOOR_HINTS):
                indoor = False
            website = _text(item.get("website"))
            accepted.append({
                "poi_id": poi_id, "name": name, "address": _text(item.get("address")), "city": provider_city or city,
                "city_code": _text(item.get("citycode")), "lng": location[0], "lat": location[1],
                "category": "酒店" if hotels else type_name or "景点", "indoor": True if hotels else indoor,
                "evidence": "amap",
                **amap_source(name, *location),
                **({"official_website": website} if not hotels and website.startswith("https://") else {}),
                "pricing": {"status": "unknown"},
            })
        return accepted

    def search_poi(self, city: str, query: str) -> list[dict]:
        return self._places(city, query)

    def search_hotels(self, city: str, *, request: TripRequest | None = None) -> list[dict]:
        if request is None:
            return self._places(city, "酒店", hotels=True)
        nights, rooms = lodging_scope(request)
        if not nights:
            return []
        nearby = self._places(city, "酒店", hotels=True)
        if not nearby:
            return []
        fallback = lodging_search(request)
        for hotel in nearby:
            hotel["pricing"] = {"status": "unknown", "nights": nights, "rooms": rooms, **fallback}
        if not booking_available():
            return nearby
        try:
            quoted = BookingPricing(self.client).hotels(request, nearby[0])
            return self._locate_booking_hotels(quoted, city) + nearby
        except ProviderError:
            return nearby

    def _locate_booking_hotels(self, quoted: list[dict], city: str) -> list[dict]:
        if not quoted:
            return []
        raw_points = "|".join(f"{hotel['lng']:.6f},{hotel['lat']:.6f}" for hotel in quoted)
        converted = self._get("/v3/assistant/coordinate/convert", {"locations": raw_points, "coordsys": "gps"})
        points = _text(converted.get("locations")).split(";")
        if len(points) != len(quoted):
            return []
        results = []
        for hotel, point in zip(quoted, points, strict=True):
            location = _location(point)
            if not location:
                continue
            area = self._get("/v3/geocode/regeo", {"location": point, "extensions": "base"}).get("regeocode")
            component = area.get("addressComponent") if isinstance(area, dict) else None
            if not isinstance(component, dict):
                continue
            provider_city = _text(component.get("city")) or _text(component.get("province"))
            if not provider_city or city_key(provider_city) != city_key(city):
                continue
            results.append({**hotel, "lng": location[0], "lat": location[1], "city": provider_city,
                            "city_code": _text(component.get("citycode"))})
        return results

    def quote_attractions(self, visits: list[tuple[dict, str]]) -> dict[str, dict]:
        return BookingPricing(self.client).attractions(visits)

    def get_weather(self, city: str, dates: list[str]) -> list[dict]:
        district = self._get("/v3/config/district", {"keywords": city, "subdistrict": 0})
        cities = [row for row in _items(district.get("districts")) if city_key(_text(row.get("name"))) == city_key(city)]
        if not cities or not _text(cities[0].get("adcode")):
            return []
        payload = self._get("/v3/weather/weatherInfo", {"city": cities[0]["adcode"], "extensions": "all"})
        result = []
        for forecast in _items(payload.get("forecasts")):
            for day in _items(forecast.get("casts")):
                if day.get("date") not in dates:
                    continue
                result.append({
                    "date": day["date"], "summary": _text(day.get("dayweather")) or "天气未知",
                    "low_c": _integer(day.get("nighttemp")), "high_c": _integer(day.get("daytemp")),
                    "as_of": _text(forecast.get("reporttime")), "evidence": "amap",
                })
        return result

    def get_route(self, city: str, transport: str, origin: dict, destination: dict) -> dict | None:
        params = {
            "origin": f"{origin['lng']:.6f},{origin['lat']:.6f}",
            "destination": f"{destination['lng']:.6f},{destination['lat']:.6f}",
            "show_fields": "cost,polyline",
        }
        path = f"/v5/direction/{transport}"
        if transport == "transit":
            path += "/integrated"
            if not (origin.get("city_code") and destination.get("city_code")):
                return None
            params.update(city1=origin["city_code"], city2=destination["city_code"], AlternativeRoute=1)
        route = self._get(path, params).get("route")
        choices = _items(route.get("transits" if transport == "transit" else "paths")) if isinstance(route, dict) else []
        if not choices:
            return None
        route = choices[0]
        cost = route.get("cost") if isinstance(route.get("cost"), dict) else {}
        duration = _integer(cost.get("duration") or route.get("duration"))
        if duration is None:
            return None
        points = _items(route.get("steps"))
        for segment in _items(route.get("segments")):
            walking, bus = segment.get("walking"), segment.get("bus")
            points.extend(_items(walking.get("steps")) if isinstance(walking, dict) else [])
            points.extend(_items(bus.get("buslines"))[:1] if isinstance(bus, dict) else [])
        polyline = [point for step in points for encoded in _text(step.get("polyline")).split(";") if (point := _location(encoded))]
        if len(polyline) > 1_000:
            last = len(polyline) - 1
            polyline = [polyline[round(index * last / 999)] for index in range(1_000)]
        fare = {}
        if transport == "transit":
            fare_cny = _fare(cost.get("transit_fee"))
            if fare_cny is not None:
                fare["fare_cny"] = fare_cny
        return {
            "distance_m": _integer(route.get("distance")), "duration_minutes": math.ceil(duration / 60),
            "polyline": polyline, "evidence": "amap",
            **fare,
        }


class ChatCompletionsModel:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def complete(self, request: dict) -> dict:
        base_url = _env("TRAVEL_MODEL_BASE_URL", "CHAT_BASE_URL", "LLM_BASE_URL", "OPENAI_BASE_URL").rstrip("/")
        api_key = _env("TRAVEL_MODEL_API_KEY", "CHAT_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY")
        model = _env("TRAVEL_MODEL_NAME", "CHAT_MODEL", "LLM_MODEL", "OPENAI_MODEL")
        if not (base_url.startswith(("https://", "http://")) and api_key and model):
            raise ProviderError("model_config", "请配置模型服务地址、名称和密钥")
        endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        data = _fetch("Model", lambda: self.client.post(endpoint, json={**request, "model": model}, headers={"Authorization": f"Bearer {api_key}"}))
        choices = _items(data.get("choices"))
        message = choices[0].get("message") if choices else None
        if not isinstance(message, dict):
            raise ProviderError("model_payload", "模型未返回消息", retryable=True)
        calls, content = _items(message.get("tool_calls")), message.get("content")
        if not calls and not isinstance(content, str):
            raise ProviderError("model_payload", "模型未返回文本或工具调用", retryable=True)
        return {"role": "assistant", "content": content if isinstance(content, str) else None, **({"tool_calls": calls} if calls else {})}
