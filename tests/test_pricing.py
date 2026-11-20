import json
from datetime import datetime

import httpx
import pytest

from travel_agent.domain import TripRequest
from travel_agent.pricing import BookingPricing


@pytest.fixture
def booking(monkeypatch):
    monkeypatch.setenv("BOOKING_API_TOKEN", "mock-token")
    monkeypatch.setenv("BOOKING_AFFILIATE_ID", "1234")
    monkeypatch.setattr(BookingPricing, "clock", staticmethod(lambda: datetime.fromisoformat("2026-11-16T10:00:00+08:00")))


def test_hotel_quote_preserves_stay_total_and_separates_derived_nightly_average(booking):
    url = "https://www.booking.com/hotel/cn/example.html?checkin=2026-11-20&checkout=2026-11-22"

    def respond(request):
        assert request.headers["Authorization"] == "Bearer mock-token"
        arguments = json.loads(request.content)
        if request.url.path.endswith("/search"):
            assert arguments["checkin"] == "2026-11-20" and arguments["checkout"] == "2026-11-22"
            assert arguments["guests"] == {"number_of_adults": 3, "number_of_rooms": 2}
            return httpx.Response(200, json={"data": [
                {"id": 2, "currency": {"booker": "CNY"}, "price": {"total": {"booker_currency": 829}}, "url": url},
                {"id": 6, "currency": "CNY", "price": {"total": 300}, "url": "https://booking.com.invalid/hotel/unsafe"},
            ]})
        assert arguments["accommodations"] == [2]
        return httpx.Response(200, json={"data": [{"id": 2, "name": {"zh-cn": "报价酒店"}, "location": {
            "country": "cn", "coordinates": {"longitude": 116.4, "latitude": 39.9},
        }}]})

    args = TripRequest(city="北京", days=3, travelers=3, start_date="2026-11-20")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        place, = BookingPricing(client).hotels(args, {"lng": 116.4, "lat": 39.9})
    quote = place["pricing"]
    assert (quote["status"], quote["total"], quote["nightly"], quote["rooms"], quote["nights"]) == ("quoted", 829, 414.5, 2, 2)
    assert quote["source_url"] == url and "per_night" not in quote


def test_seattle_hotel_uses_local_checkin_date_and_keeps_only_nearby_matches(booking, monkeypatch):
    monkeypatch.setattr(BookingPricing, "clock", staticmethod(lambda: datetime.fromisoformat("2026-11-17T09:00:00+08:00")))
    url = "https://www.booking.com/hotel/us/seattle-example.html"

    def respond(request):
        arguments = json.loads(request.content)
        if request.url.path.endswith("/search"):
            assert arguments["checkin"] == "2026-11-16"
            assert arguments["checkout"] == "2026-11-18"
            assert arguments["currency"] == "CNY"
            return httpx.Response(200, json={"data": [
                {"id": identifier, "currency": {"booker": "CNY", "accommodation": "USD"},
                 "price": {"total": {"booker_currency": 630, "accommodation_currency": 87}}, "url": url}
                for identifier in (1, 2)
            ]})
        return httpx.Response(200, json={"data": [
            {"id": identifier, "name": {"en-gb": "Seattle Hotel"}, "location": {
                "country": "us", "coordinates": {"longitude": lng, "latitude": lat},
            }} for identifier, lng, lat in ((1, -122.33, 47.61), (2, -122.20, 47.50))
        ]})

    args = TripRequest(city="西雅图", days=3, travelers=2, start_date="2026-11-16")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        place, = BookingPricing(client).hotels(args, {"lng": -122.33, "lat": 47.61}, country="us")
    assert place["poi_id"] == "booking-1"
    assert (place["pricing"]["total"], place["pricing"]["nightly"], place["pricing"]["currency"]) == (630, 315, "CNY")


def test_attraction_quote_requires_admission_and_matched_location(booking):
    ticket = "https://www.booking.com/attractions/cn/ticket.html"
    tour = "https://www.booking.com/attractions/cn/tour.html"
    position = {"country": "cn", "type": "attraction", "coordinates": {"longitude": 116.41, "latitude": 39.88}}

    def respond(request):
        arguments = json.loads(request.content)
        if request.url.path.endswith("/search"):
            assert arguments["dates"] == {"start_date": "2026-11-20", "end_date": "2026-11-20"}
            return httpx.Response(200, json={"data": [
                {"id": identifier, "price": {"total": amount, "currency": "CNY"}, "urls": {"web": {"detail": url}}}
                for identifier, amount, url in [("tour", 15, tour), ("ticket", 38, ticket)]
            ]})
        return httpx.Response(200, json={"data": [
            {"id": "tour", "name": {"zh-cn": "天坛公园导览和午餐"}, "locations": [position]},
            {"id": "ticket", "name": {"zh-cn": "天坛公园成人门票"}, "locations": [position]},
        ]})

    place = {"poi_id": "tiantan", "name": "天坛公园", "city": "北京", "lng": 116.41, "lat": 39.88}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        quote = BookingPricing(client).attractions([(place, "2026-11-20")])["tiantan"]
    assert (quote["amount"], quote["source_url"], quote["visit_date"]) == (38, ticket, "2026-11-20")
