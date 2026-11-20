"""Offline tests for RollingGo's read-only hotel price adapter."""

from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

import httpx
import pytest

from travel_agent.domain import TripRequest
from travel_agent.rollinggo import RollingGoPricing, rollinggo_available


MOCK_KEY = "mcp_mock_rollinggo_key"
HOTEL_ID = 12999
HOTEL_LINK = "https://rollinggo.cn/pages/hotel/detail/index?id=12999"
ANCHOR = {"lng": 121.4895, "lat": 31.2390}


@pytest.fixture
def rollinggo_key(monkeypatch):
    monkeypatch.setenv("ROLLINGGO_API_KEY", MOCK_KEY)


def _trip(travelers, *, city="上海"):
    arrival = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=7)
    return TripRequest(city=city, start_date=arrival, days=3, travelers=travelers)


def _search_row(booking_url=HOTEL_LINK):
    return {
        "hotelId": HOTEL_ID, "name": "外滩报价测试酒店", "address": "黄浦区南京东路20号", "areaCode": "CN",
        "latitude": ANCHOR["lat"], "longitude": ANCHOR["lng"], "bookingUrl": booking_url,
        "price": {"hasPrice": True, "currency": "CNY", "lowestPrice": 39},
    }


def _detail(arguments, *, booking_url=HOTEL_LINK):
    occupancy = arguments["occupancyParam"]
    average = {1: 160.25, 2: 212.25}[occupancy["adultCount"]]
    return {
        "success": True, "hotelId": arguments["hotelId"], "name": "报价测试酒店", "bookingUrl": booking_url,
        "checkIn": arguments["dateParam"]["checkInDate"], "checkOut": arguments["dateParam"]["checkOutDate"],
        "roomRatePlans": [
            {"roomNameCn": "基础房", "averagePrice": average, "totalPrice": 0, "currency": "CNY", "isOnRequest": False},
        ],
    }


def _transport(tool_result):
    def handle(request):
        assert (request.url.scheme, request.url.host, request.url.path) == ("https", "mcp.rollinggo.cn", "/mcp")
        assert request.headers["Authorization"] == f"Bearer {MOCK_KEY}"
        assert "text/event-stream" in request.headers["Accept"]
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-06-18"},
            })
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert message["method"] == "tools/call"
        name, arguments = message["params"]["name"], message["params"]["arguments"]
        payload = tool_result(name, arguments)
        if isinstance(payload, httpx.Response):
            return payload
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        }}
        if name == "searchHotels":
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps(reply)}\n\n",
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=reply)

    return httpx.MockTransport(handle)


def test_rollinggo_does_not_request_without_a_key(monkeypatch):
    monkeypatch.delenv("ROLLINGGO_API_KEY", raising=False)

    def handle(_):
        pytest.fail("RollingGo should not make a request without an API key")

    assert not rollinggo_available()
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert RollingGoPricing(client).hotels(_trip(2), anchor=ANCHOR) == []


@pytest.mark.parametrize(("travelers", "occupancies", "nightly"), [
    (2, [2], 212.25),
    (3, [1, 2], 372.50),
])
def test_quote_uses_each_room_occupancy_and_labels_the_derived_total(rollinggo_key, travelers, occupancies, nightly):
    trip = _trip(travelers)
    checkin, checkout = trip.dates()[0], trip.dates()[-1]
    room_calls = []

    def answer(name, arguments):
        if name == "searchHotels":
            assert arguments["place"] == "上海"
            assert arguments["checkInParam"]["checkInDate"] == checkin
            assert arguments["checkInParam"]["stayNights"] == 2
            return {"success": True, "hotelInformationList": [_search_row()]}
        assert name == "getHotelDetail"
        assert arguments["hotelId"] == HOTEL_ID
        assert arguments["dateParam"] == {"checkInDate": checkin, "checkOutDate": checkout}
        assert arguments["occupancyParam"]["roomCount"] == 1
        room_calls.append(arguments["occupancyParam"]["adultCount"])
        return _detail(arguments)

    assert rollinggo_available()
    with httpx.Client(transport=_transport(answer)) as client:
        hotel, = RollingGoPricing(client).hotels(trip, anchor=ANCHOR)
    assert sorted(room_calls) == occupancies
    assert (hotel["lng"], hotel["lat"]) == (ANCHOR["lng"], ANCHOR["lat"])
    price = hotel["pricing"]
    assert price["currency"] == "CNY"
    assert (price["nightly"], price["total"], price["nights"], price["rooms"]) == (nightly, nightly * 2, 2, len(occupancies))
    assert price["total_estimated"] is True
    assert price["source_url"] == HOTEL_LINK
    assert not price.get("per_night")


def test_seattle_quote_preserves_wgs84_point_and_provider_booking_link(rollinggo_key):
    anchor = {"lat": 47.6062, "lng": -122.3321, "country": "us"}
    point = 47.6124, -122.3378
    trip = _trip(2, city="西雅图")

    def answer(name, arguments):
        if name == "searchHotels":
            assert arguments["place"] in {"西雅图", "Seattle"}
            assert arguments["checkInParam"]["checkInDate"] == trip.dates()[0]
            row = {**_search_row(), "name": "Seattle Mock Hotel", "areaCode": "US",
                   "address": "Pike Street, Seattle, WA", "latitude": point[0], "longitude": point[1]}
            return {"success": True, "hotelInformationList": [{**row, "hotelId": HOTEL_ID + 1, "areaCode": "CA"}, row]}
        assert name == "getHotelDetail" and arguments["hotelId"] == HOTEL_ID
        return _detail(arguments)

    with httpx.Client(transport=_transport(answer)) as client:
        hotel, = RollingGoPricing(client).hotels(trip, anchor=anchor)
    assert (hotel["name"], hotel["country"], hotel["lat"], hotel["lng"]) == ("Seattle Mock Hotel", "us", *point)
    assert hotel["pricing"]["source_url"] == HOTEL_LINK
    assert (hotel["pricing"]["currency"], hotel["pricing"]["total"]) == ("CNY", 424.50)


def test_budget_search_verifies_both_economy_and_higher_tier_hotel_quotes(rollinggo_key):
    trip = _trip(2, city="西安").model_copy(update={"budget": 3000})
    prices = {HOTEL_ID + index: price for index, price in enumerate((86, 119, 160, 536, 626))}

    def answer(name, arguments):
        if name == "searchHotels":
            assert arguments["size"] == 10
            higher_tier = bool(arguments.get("filterOptions"))
            if higher_tier:
                assert arguments["filterOptions"] == {"starRatings": [4.0, 5.0]}
            rows = []
            for identifier, price in prices.items():
                if (price >= 500) == higher_tier:
                    url = f"https://rollinggo.cn/pages/hotel/detail/index?id={identifier}"
                    row = _search_row(url)
                    rows.append({**row, "hotelId": identifier, "price": {**row["price"], "lowestPrice": price - 10}})
            return {"success": True, "hotelInformationList": rows}
        identifier = arguments["hotelId"]
        result = _detail(arguments, booking_url=f"https://rollinggo.cn/pages/hotel/detail/index?id={identifier}")
        result["roomRatePlans"][0]["averagePrice"] = prices[identifier]
        return result

    with httpx.Client(transport=_transport(answer)) as client:
        hotels = RollingGoPricing(client).hotels(trip)
    assert [hotel["pricing"]["nightly"] for hotel in hotels] == [86, 119, 160, 536, 626]


def test_hotel_detail_failure_does_not_discard_later_candidate(rollinggo_key):
    next_id = HOTEL_ID + 1
    next_url = f"https://rollinggo.cn/pages/hotel/detail/index?id={next_id}"
    detail_calls = []

    def answer(name, arguments):
        if name == "searchHotels":
            return {"success": True, "hotelInformationList": [
                _search_row(), {**_search_row(next_url), "hotelId": next_id, "name": "后续酒店"},
            ]}
        assert name == "getHotelDetail"
        detail_calls.append(arguments["hotelId"])
        if arguments["hotelId"] == HOTEL_ID:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return _detail(arguments, booking_url=next_url)

    with httpx.Client(transport=_transport(answer)) as client:
        hotel, = RollingGoPricing(client).hotels(_trip(2), anchor=ANCHOR)
    assert detail_calls[0] == HOTEL_ID and detail_calls[-1] == next_id
    assert hotel["poi_id"] == f"rollinggo-{next_id}"
    assert hotel["pricing"]["source_url"] == next_url


@pytest.mark.parametrize("mismatch", ["hotel", "dates", "link", "link-hotel", "currency"])
def test_invalid_details_cannot_become_a_stay_quote(rollinggo_key, mismatch):
    link = {
        "link": "https://rollinggo.cn.evil.invalid/pages/hotel/detail/index?id=12999",
        "link-hotel": "https://rollinggo.cn/pages/hotel/detail/index?hotelId=13000",
    }.get(mismatch, HOTEL_LINK)

    def answer(name, arguments):
        if name == "searchHotels":
            return {"success": True, "hotelInformationList": [_search_row(link)]}
        result = _detail(arguments, booking_url=link)
        if mismatch == "hotel":
            result["hotelId"] += 1
        elif mismatch == "dates":
            result["checkOut"] = result["checkIn"]
        elif mismatch == "currency":
            result["roomRatePlans"][0]["currency"] = "USD"
        return result

    with httpx.Client(transport=_transport(answer)) as client:
        hotels = RollingGoPricing(client).hotels(_trip(2), anchor=ANCHOR)
    for hotel in hotels:
        assert hotel["pricing"].get("total") is None
        assert "evil.invalid" not in str(hotel)
