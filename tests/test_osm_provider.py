import httpx

from travel_agent.osm_provider import OSMProvider


def test_osm_requires_a_matched_city_and_keeps_real_osm_identity():
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host.startswith("geocoding-api."):
            assert request.url.params["name"] == "Seattle"
            return httpx.Response(200, json={"results": [
                {"name": "Seattle", "latitude": 47.61, "longitude": -122.33, "feature_code": "PPL"},
            ]})
        assert 'area.cover["boundary"="administrative"]["name"="Seattle"]' in request.url.params["data"]
        return httpx.Response(200, json={"elements": [
            {"type": "way", "id": 123, "center": {"lat": 47.621, "lon": -122.349},
             "tags": {"name": "A Museum", "name:zh": "博物馆", "tourism": "museum", "wikidata": "Q1"}},
            {"type": "way", "id": 124, "center": {"lat": 40.0, "lon": -122.0},
             "tags": {"name": "Outside Seattle", "tourism": "museum"}},
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = OSMProvider(client)
        place, = provider.search_poi("西雅图", "博物馆|美术馆")
        assert provider.search_poi("西雅图", "A Museum") == [place]
    assert len(calls) == 2
    assert place["city"] == "西雅图"
    assert place["source_url"] == "https://www.openstreetmap.org/way/123"
    assert place["source_name"] == "© OpenStreetMap contributors"
    assert place["pricing"]["status"] == "unknown"


def test_seattle_overpass_outage_keeps_verified_places_and_real_hotel_without_price():
    def respond(request):
        if request.url.host.startswith("geocoding-api."):
            return httpx.Response(200, json={"results": [
                {"name": "Seattle", "latitude": 47.61, "longitude": -122.33, "feature_code": "PPL"},
            ]})
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = OSMProvider(client)
        places = provider.search_poi("西雅图", "旅游景点")
        food, = provider.search_poi("西雅图", "美食")
        hotels = provider.search_hotels("西雅图")
    assert len(places) >= 8
    assert all(place["city"] == "西雅图" and place["evidence"] == "wikivoyage" for place in places)
    assert food["source_url"] == "https://www.pikeplacemarket.org/"
    assert len(hotels) and all(place["pricing"]["status"] == "unknown" for place in hotels)
