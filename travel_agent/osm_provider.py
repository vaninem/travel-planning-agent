"""Look up verified international attractions in OpenStreetMap."""

from dataclasses import dataclass
import json
import os
import unicodedata
from collections.abc import Iterable

import httpx

from .domain import ProviderError, TripRequest
from .providers import _fetch, distance_m, poi_matches


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
ALIASES = {"西雅图": "Seattle", "美国西雅图": "Seattle, US", "紐約": "New York", "纽约": "New York", "洛杉矶": "Los Angeles",
           "旧金山": "San Francisco", "伦敦": "London", "巴黎": "Paris", "東京": "Tokyo", "东京": "Tokyo"}
TOURISM = {"museum": ("博物馆", True), "gallery": ("美术馆", True), "attraction": ("地标", None),
           "viewpoint": ("观景点", False), "zoo": ("动物园", False), "aquarium": ("水族馆", True),
           "theme_park": ("主题公园", False), "hotel": ("酒店", True), "guest_house": ("民宿", True)}
GENERIC = {"景点", "旅游景点", "热门景点", "经典地标", "著名景点", "attractions", "sightseeing"}
QUERY_GROUPS = {
    "公园": {"公园", "主题公园", "园林"}, "自然": {"公园", "园林", "观景点", "动物园"},
    "亲子": {"动物园", "博物馆", "水族馆", "主题公园", "公园"},
    "室内": {"博物馆", "美术馆", "水族馆"}, "museum": {"博物馆", "美术馆"}, "park": {"公园", "园林"},
    "美食": {"市场"},
}
HOTEL_SEARCH = {"酒店", "住宿", "hotel", "hotels"}
SEATTLE_ALIASES = {"西雅图", "美国西雅图", "seattle", "seattlewa", "seattlewashington", "seattleus", "seattleusa"}
ATTRACTIONS = (
    'nwr(area.city)["tourism"~"^(attraction|museum|gallery|viewpoint|zoo|aquarium|theme_park)$"]["name"];'
    'nwr(area.city)["leisure"~"^(garden|park)$"]["name"]["wikidata"];'
    'nwr(area.city)["amenity"="marketplace"]["name"]["wikidata"];'
)
HOTELS = 'nwr(area.city)["tourism"~"^(hotel|guest_house)$"]["name"];'

# Published map listings with venue links, used if Overpass cannot serve Seattle.
DOWNTOWN_SOURCE = "https://en.wikivoyage.org/wiki/Seattle/Downtown"
QUEEN_ANNE_SOURCE = "https://en.wikivoyage.org/wiki/Seattle/Queen_Anne-South_Lake_Union"
SEATTLE_PLACES = (
    ("space-needle", "太空针塔 · Space Needle", 47.6204, -122.3491, "400 Broad St", "地标", True, "https://www.spaceneedle.com/", QUEEN_ANNE_SOURCE),
    ("pike-place", "派克市场 · Pike Place Market", 47.6094, -122.342, "1501 Pike Pl", "市场", True, "https://www.pikeplacemarket.org/", DOWNTOWN_SOURCE),
    ("mopop", "流行文化博物馆 · Museum of Pop Culture", 47.6215, -122.3486, "325 5th Ave N", "博物馆", True, "https://www.mopop.org/", QUEEN_ANNE_SOURCE),
    ("chihuly", "奇胡利玻璃艺术园 · Chihuly Garden and Glass", 47.6206, -122.35, "305 Harrison St", "美术馆", True, "https://www.chihulygardenandglass.com/", QUEEN_ANNE_SOURCE),
    ("aquarium", "西雅图水族馆 · Seattle Aquarium", 47.6077, -122.343, "1483 Alaskan Way", "水族馆", True, "https://www.seattleaquarium.org/", DOWNTOWN_SOURCE),
    ("seattle-art-museum", "西雅图艺术博物馆 · Seattle Art Museum", 47.6072, -122.338, "100 University St", "博物馆", True, "https://www.seattleartmuseum.org/", DOWNTOWN_SOURCE),
    ("olympic-sculpture-park", "奥林匹克雕塑公园 · Olympic Sculpture Park", 47.61676, -122.3553, "2901 Western Ave", "公园", False, "https://www.seattle.gov/parks/recreation/events-and-attractions/museums-and-attractions", DOWNTOWN_SOURCE),
    ("kerry-park", "凯瑞公园 · Kerry Park", 47.6296, -122.3594, "211 W Highland Dr", "观景点", False, "https://www.seattle.gov/parks/parks/kerry-park", QUEEN_ANNE_SOURCE),
    ("discovery-park", "探索公园 · Discovery Park", 47.66053, -122.41572, "3801 W Government Way", "公园", False, "https://www.seattle.gov/parks/parks/discovery-park", QUEEN_ANNE_SOURCE),
)
SEATTLE_HOTELS = (
    ("marqueen-hotel", "MarQueen Hotel", 47.62475, -122.35654, "600 Queen Anne Ave N", "酒店", True, "https://www.marqueen.com/", QUEEN_ANNE_SOURCE),
    ("maxwell-hotel", "The Maxwell Hotel", 47.62557, -122.3513, "300 Roy St", "酒店", True, "https://www.themaxwellhotel.com/", QUEEN_ANNE_SOURCE),
)


@dataclass(frozen=True)
class City:
    name: str
    lat: float
    lng: float
    country: str = ""


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: object, bound: float) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if -bound <= result <= bound else None


def _search_key(value: str) -> str:
    chars = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in chars if char.isalnum())


def _matching_category(place: dict, query: str) -> bool:
    return poi_matches(place, query) or any(
        word in query.casefold() and place["category"] in categories for word, categories in QUERY_GROUPS.items()
    )


def _seattle_places(city: str, hotels: bool) -> list[dict]:
    return [{
        "poi_id": f"seattle-{slug}", "name": name, "city": city, "city_code": "", "lng": lng, "lat": lat,
        "address": address, "category": category, "indoor": indoor, "evidence": "wikivoyage", "country": "us",
        "source_url": url, "source_name": "场馆官网 · 坐标 Wikivoyage", "coordinate_url": coordinates,
        "pricing": {"status": "unknown"},
    } for slug, name, lat, lng, address, category, indoor, url, coordinates in (SEATTLE_HOTELS if hotels else SEATTLE_PLACES)]


def _tagged_places(rows: Iterable, label: str, city: City) -> list[dict]:
    ranked = []
    center = {"lat": city.lat, "lng": city.lng}
    for item in rows:
        if not isinstance(item, dict):
            continue
        kind, identifier, tags = item.get("type"), item.get("id"), item.get("tags")
        if kind not in ("node", "way", "relation") or type(identifier) is not int or identifier <= 0 or not isinstance(tags, dict):
            continue
        point = item.get("center", item)
        if not isinstance(point, dict):
            continue
        lat, lng = _number(point.get("lat"), 90), _number(point.get("lon"), 180)
        if lat is None or lng is None or distance_m(center, {"lat": lat, "lng": lng}) > 35_000:
            continue
        name = _text(tags.get("name"))
        if not name or _text(tags.get("access")) in {"private", "no"}:
            continue
        localized = _text(tags.get("name:zh-Hans")) or _text(tags.get("name:zh"))
        category, indoor = TOURISM.get(_text(tags.get("tourism")), ("地标", None))
        if tags.get("leisure") in {"park", "garden"}:
            category, indoor = ("公园" if tags["leisure"] == "park" else "园林"), False
        if tags.get("amenity") == "marketplace":
            category = "市场"
        quality = 3 * bool(tags.get("wikidata")) + 2 * bool(tags.get("wikipedia")) + bool(tags.get("website"))
        street = " ".join(filter(None, (_text(tags.get("addr:housenumber")), _text(tags.get("addr:street")))))
        website = _text(tags.get("website")) or _text(tags.get("contact:website"))
        ranked.append((quality, name, {
            "poi_id": f"osm-{kind}-{identifier}",
            "name": f"{localized} · {name}" if localized and localized != name else name,
            "address": street or city.name,
            "city": label, "city_code": "", "lng": lng, "lat": lat,
            "category": category, "indoor": indoor, "evidence": "osm", "country": city.country,
            "source_url": f"https://www.openstreetmap.org/{kind}/{identifier}",
            "source_name": "© OpenStreetMap contributors", "pricing": {"status": "unknown"},
            **({"official_website": website} if website.startswith("https://") else {}),
        }))
    deduplicated = {}
    for _, name, place in sorted(ranked, key=lambda row: (-row[0], distance_m(center, row[2]), row[1])):
        deduplicated.setdefault(_search_key(name), place)
    return list(deduplicated.values())


class OSMProvider:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self._cities: dict[str, City | None] = {}
        self._places: dict[tuple[str, bool], list[dict]] = {}
        self._unavailable: set[str] = set()

    def _city(self, label: str) -> City | None:
        if label in self._cities:
            return self._cities[label]
        name = ALIASES.get(label, label)
        payload = _fetch("geocoding", lambda: self.client.get(
            GEOCODING_URL, params={"name": name, "count": 10, "language": "en"}, timeout=5,
        ))
        results = payload.get("results") or []
        primary = _search_key(name.split(",", 1)[0])
        for row in results:
            if not isinstance(row, dict) or not _text(row.get("feature_code")).startswith("PPL"):
                continue
            exact = _search_key(_text(row.get("name"))) == primary
            lat, lng = _number(row.get("latitude"), 90), _number(row.get("longitude"), 180)
            if exact and lat is not None and lng is not None:
                self._cities[label] = City(row["name"], lat, lng, _text(row.get("country_code")).lower())
                break
        return self._cities.setdefault(label, None)

    def _overpass(self, label: str, hotels: bool) -> list[dict]:
        if (label, hotels) in self._places:
            return self._places[label, hotels]
        city = self._city(label)
        if city is None:
            return []
        name = json.dumps(city.name, ensure_ascii=False)
        query = (f'[out:json][timeout:9];is_in({city.lat:.6f},{city.lng:.6f})->.cover;'
                 f'area.cover["boundary"="administrative"]["name"={name}]->.city;'
                 f'({HOTELS if hotels else ATTRACTIONS});out center 180;')
        url = os.environ.get("TRAVEL_OVERPASS_URL") or OVERPASS_URL
        payload = _fetch("OpenStreetMap", lambda: self.client.get(url, params={"data": query}, timeout=10,
            headers={"User-Agent": "TravelPlanningPreview/1.0 (personal itinerary planner)", "Accept": "application/json"},
        ))
        places = _tagged_places(payload.get("elements") or [], label, city)
        if not places and payload.get("remark"):
            raise ProviderError("osm_timeout", "公开地图服务响应不完整", retryable=True)
        self._places[label, hotels] = places
        return places

    def search_poi(self, city: str, query: str) -> list[dict]:
        city, query = city.strip(), query.strip()
        hotels = query.casefold() in HOTEL_SEARCH
        seattle = _search_key(city) in SEATTLE_ALIASES
        try:
            places = self._places.get((city, hotels), []) if city in self._unavailable else self._overpass(city, hotels)
        except ProviderError:
            if not seattle:
                raise
            self._unavailable.add(city)
            places = []
        if seattle and not places:
            places = _seattle_places(city, hotels)
            self._places[city, hotels] = places
        if hotels or query.casefold() in GENERIC:
            return places[:12]
        keyword = _search_key(query)
        matches = [place for place in places if keyword and keyword in _search_key(place["name"])]
        categories = [word.strip() for word in query.split("|") if word.strip()]
        return (matches or [place for place in places if any(_matching_category(place, term) for term in categories)])[:12]

    def search_hotels(self, city: str, *, request: TripRequest | None = None) -> list[dict]:
        return self.search_poi(city, "酒店") if request is None or (request.hotel and request.days > 1) else []
