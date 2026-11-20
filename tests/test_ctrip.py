import json

import httpx
import pytest

from travel_agent.ctrip import CtripWendao, ctrip_available
from travel_agent.domain import ProviderError, TripRequest


def trip(**fields):
    return TripRequest(city="北京", start_date="2026-11-20", days=3, travelers=2, **fields)


def test_wendao_keeps_provider_detail_links_and_ignores_generated_prices(monkeypatch):
    monkeypatch.setenv("WENDAO_API_KEY", "test-token")
    hotel = "https://www.trip.com/hotels/beijing-hotel-detail-347414/hilton-beijing/"
    scenic = "https://you.ctrip.com/destinationsite/sight/beijing1/229-sight.html"

    def handle(request):
        if request.method == "HEAD":
            assert str(request.url) in {hotel, scenic}
            assert "Authorization" not in request.headers and "Cookie" not in request.headers
            assert not request.content
            return httpx.Response(200)
        assert request.url == "https://externalcallback.ctrip.com/skills/api/crew/workbuddy/searchInfo"
        inputs = json.loads(request.content)["inputs"]
        assert inputs["token"] == "test-token"
        assert all(word in inputs["query"] for word in ("北京", "2026-11-20", "2026-11-22"))
        assert "private-note" not in inputs["query"]
        return httpx.Response(200, json={
            "result": {"content": (
                f"[酒店 ¥99/晚]({hotel}) [景点]({scenic}) [重复]({hotel})\n"
                "[钓鱼](https://you.ctrip.com.evil.test/sight/beijing1/229.html)\n"
                "[跳转](https://you.ctrip.com/sight/beijing1/229.html?redirectUrl=https://example.com)"
            )},
            "state": {"result": "https://you.ctrip.com/sight/beijing1/133020.html"},
        })

    with httpx.Client(transport=httpx.MockTransport(handle), headers={"Authorization": "test-header"}, cookies={"session": "private"}) as client:
        assert ctrip_available()
        links = CtripWendao(client).search_links(trip(notes="private-note"))
    assert [(link["kind"], link["source_url"]) for link in links] == [("hotel", hotel), ("attraction", scenic)]
    assert all(link["source_name"] == "携程问道" and "99" not in link["label"] for link in links)


def test_wendao_skips_network_without_token_and_filters_hotels_when_disabled(monkeypatch):
    monkeypatch.delenv("WENDAO_API_KEY", raising=False)

    def unexpected(_):
        pytest.fail("No request should be sent without credentials")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        assert not ctrip_available()
        assert CtripWendao(client).search_links(trip()) == []

    monkeypatch.setenv("WENDAO_API_KEY", "test-token")
    body = {"result": "https://hotels.ctrip.com/hotels/387238.html"}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        assert CtripWendao(client).search_links(trip(hotel=False)) == []


def test_wendao_errors_do_not_leak_secret(monkeypatch):
    monkeypatch.setenv("WENDAO_API_KEY", "test-token")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, text="test-token"))) as client:
        with pytest.raises(ProviderError) as error:
            CtripWendao(client).search_links(trip())
    assert error.value.retryable and "test-token" not in str(error.value)


def test_wendao_keeps_only_reachable_details(monkeypatch):
    monkeypatch.setenv("WENDAO_API_KEY", "test-token")
    moved = "https://you.ctrip.com/sight/beijing1/229.html"
    moved_to = "https://you.ctrip.com/destinationsite/sight/beijing1/229-sight.html"
    dead = "https://you.ctrip.com/sight/beijing1/123.html"
    unsafe_redirect = "https://hotels.ctrip.com/hotels/387238.html"
    calls = []

    def handle(request):
        url = str(request.url)
        if request.method == "POST":
            return httpx.Response(200, json={"result": " ".join((moved, dead, unsafe_redirect))})
        calls.append((request.method, url))
        if url == moved:
            return httpx.Response(302, headers={"Location": moved_to})
        if url == moved_to:
            return httpx.Response(405 if request.method == "HEAD" else 200)
        if url == dead:
            return httpx.Response(404)
        if url == unsafe_redirect:
            return httpx.Response(302, headers={"Location": "https://example.invalid/bad"})
        pytest.fail("An out-of-scope redirect should never be followed")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        links = CtripWendao(client).search_links(trip())
    assert [item["source_url"] for item in links] == [moved_to]
    assert calls == [("HEAD", moved), ("HEAD", moved_to), ("GET", moved_to), ("HEAD", dead), ("HEAD", unsafe_redirect)]
