"""Read-only hotel pricing from RollingGo's redirect-mode MCP tools."""

from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import os
from time import monotonic
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

from .amap_mcp import StreamableMCP
from .domain import ProviderError, TripRequest
from .pricing import CNY, _amount, _coordinates, _dict, _distance_km, _items, _text, lodging_scope


ROLLINGGO_URL = "https://mcp.rollinggo.cn/mcp"
ROLLINGGO_NAME = "RollingGo"
STOP_ERRORS = {"http_401", "http_403", "http_429", "version", "protocol", "connection", "deadline"}


def rollinggo_available() -> bool:
    return bool(os.getenv("ROLLINGGO_API_KEY", "").strip())


class RollingGoPricing:
    def __init__(self, client: httpx.Client):
        self.key = os.getenv("ROLLINGGO_API_KEY", "").strip()
        self.mcp = StreamableMCP(client, ROLLINGGO_URL, service=ROLLINGGO_NAME,
                                 headers={"Authorization": f"Bearer {self.key}"})

    def _booking_url(self, value: object, hotel_id: int, checkin: str, checkout: str) -> str:
        if not isinstance(value, str) or any(ch.isspace() or ch == "\\" for ch in value):
            return ""
        if self.key in unquote(value):
            return ""
        try:
            link = urlsplit(value)
        except ValueError:
            return ""
        if (link.scheme != "https" or link.netloc.lower() not in {"rollinggo.cn", "www.rollinggo.cn"}
                or link.path.rstrip("/") != "/pages/hotel/detail/index"):
            return ""
        expected = {"id": str(hotel_id), "hotelid": str(hotel_id), "checkin": checkin, "checkindate": checkin,
                    "checkout": checkout, "checkoutdate": checkout}
        for name, parameter in parse_qsl(link.query):
            normalized = name.lower().replace("_", "").replace("-", "")
            if normalized in expected and parameter != expected[normalized]:
                return ""
        return value

    def _call(self, name: str, arguments: dict, deadline: float) -> dict:
        for attempt in range(2):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ProviderError("RollingGo_mcp_deadline", "酒店报价查询超时")
            self.mcp.timeout = min(6.0, remaining)
            try:
                return self.mcp.call_tool(name, arguments)
            except ProviderError as error:
                if attempt or not error.retryable or error.code.endswith("_http_429"):
                    raise

    def hotels(self, request: TripRequest, anchor: dict | None = None) -> list[dict]:
        nights, rooms = lodging_scope(request)
        if not self.key or not 1 <= nights <= 28:
            return []
        checkin, checkout = request.dates()[0], request.dates()[-1]
        adults = min(request.travelers, 2)
        deadline = monotonic() + 25
        search = {
            "originQuery": f"查询{request.city}从{checkin}开始入住{nights}晚的酒店",
            "place": request.city, "placeType": "城市", "size": 10 if request.budget is not None else 5,
            "checkInParam": {"checkInDate": checkin, "stayNights": nights, "adultCount": adults},
        }
        candidate_response = self._call("searchHotels", search, deadline)
        if candidate_response.get("success") is False:
            return []
        candidates = _items(candidate_response.get("hotelInformationList"))[:search["size"]]
        quote_limit = 3
        if request.budget is not None:
            quote_limit = 6
            try:
                higher_tier = self._call("searchHotels", {**search, "filterOptions": {"starRatings": [4.0, 5.0]}}, deadline)
            except ProviderError:
                higher_tier = {}
            premium = _items(higher_tier.get("hotelInformationList"))[:search["size"]]
            target = request.budget / (nights * rooms)

            def price_distance(row: dict) -> tuple[bool, bool, float]:
                price = _dict(row.get("price"))
                amount = _amount(price.get("lowestPrice")) if price.get("currency") == CNY else None
                if not amount:
                    return True, False, 0
                return False, amount > target, abs(target - amount)

            # Keep options from the original results, then choose other hotels by their indicative rates.
            candidates = candidates[:2] + sorted(premium + candidates[2:], key=price_distance)
        results, unpriced = [], []
        seen = set()
        stop_quotes = False
        anchor_country = _text(anchor.get("country")).lower() if anchor else ""
        for row in candidates:
            identifier, name = row.get("hotelId"), _text(row.get("name") or row.get("nameEn"))
            point = _coordinates({"coordinates": row})
            country = _text(row.get("areaCode")).lower()
            if type(identifier) is not int or identifier <= 0 or identifier in seen or not name or not point:
                continue
            if anchor and (_distance_km(anchor, point) > 75 or (anchor_country and country != anchor_country)):
                continue
            seen.add(identifier)
            source_url = self._booking_url(row.get("bookingUrl"), identifier, checkin, checkout)
            hotel = {"poi_id": f"rollinggo-{identifier}", "name": name, "address": _text(row.get("address")),
                     "city": request.city, "city_code": "", "country": country, "lng": point[0], "lat": point[1],
                     "category": "酒店", "indoor": True, "evidence": "rollinggo", "source_name": ROLLINGGO_NAME}
            pricing = None
            try:
                if not stop_quotes:
                    pricing = self._quote(identifier, source_url, request, nights, rooms, checkin, checkout, deadline)
            except ProviderError as error:
                stop_quotes = error.code.removeprefix("RollingGo_mcp_") in STOP_ERRORS
            if pricing:
                results.append({**hotel, "source_url": pricing["source_url"], "pricing": pricing})
            elif source_url:
                unpriced.append({**hotel, "source_url": source_url,
                                 "pricing": {"status": "unknown", "nights": nights, "rooms": rooms}})
            if len(results) == quote_limit:
                break
        return sorted(results, key=lambda hotel: hotel["pricing"]["total"]) if results else unpriced

    def _quote(self, hotel_id: int, search_url: str, request: TripRequest, nights: int, rooms: int,
               checkin: str, checkout: str, deadline: float) -> dict | None:
        double_rooms, single_rooms = divmod(request.travelers, 2)
        nightly = Decimal(0)
        url = ""
        room_notes = []
        for adults, count in ((2, double_rooms), (1, single_rooms)):
            if not count:
                continue
            detail = self._call("getHotelDetail", {
                "hotelId": hotel_id, "dateParam": {"checkInDate": checkin, "checkOutDate": checkout},
                "occupancyParam": {"roomCount": 1, "adultCount": adults},
            }, deadline)
            if (detail.get("success") is not True or detail.get("hotelId") != hotel_id
                    or detail.get("checkIn") != checkin or detail.get("checkOut") != checkout):
                return None
            rates = []
            for rate in _items(detail.get("roomRatePlans")):
                if rate.get("currency") != CNY or rate.get("isOnRequest") is True or rate.get("inventoryCount") == 0:
                    continue
                capacity = _dict(rate.get("roomInfo")).get("maxOccupancy")
                if type(capacity) is int and capacity < adults:
                    continue
                amount = _amount(rate.get("averagePrice"))
                if amount is None and nights == 1:
                    amount = _amount(rate.get("totalPrice"))
                if amount is not None and 0 < amount <= 100_000_000:
                    rates.append((amount, _text(rate.get("roomNameCn") or rate.get("roomName"))))
            if not rates:
                return None
            amount, room_name = min(rates, key=lambda rate: rate[0])
            nightly += Decimal(str(amount)) * count
            if room_name:
                room_notes.append(f"{count} 间{room_name}，每间 {adults} 人")
            url = url or self._booking_url(detail.get("bookingUrl"), hotel_id, checkin, checkout)
        url = url or search_url
        if not url:
            return None
        cents = Decimal("0.01")
        notes = ["按查询到的平均每晚房价估算住宿合计，最终房价以预订页面为准。"]
        if room_notes:
            notes.insert(0, "、".join(room_notes))
        if rooms > 1:
            notes.append("多间房按对应人数的单间报价估算，请到预订页面核对多间库存。")
        return {"status": "quoted", "currency": CNY, "nightly": float(nightly.quantize(cents, rounding=ROUND_HALF_UP)),
                "total": float((nightly * nights).quantize(cents, rounding=ROUND_HALF_UP)), "total_estimated": True,
                "nights": nights, "rooms": rooms, "source_url": url, "source_name": ROLLINGGO_NAME,
                "observed_at": datetime.now(UTC).isoformat(timespec="seconds"), "notes": notes}
