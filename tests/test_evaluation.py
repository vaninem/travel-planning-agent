from collections import Counter
from datetime import date
import json

import httpx
import pytest

from evals.cases import TravelCase, build_cases
from evals.exchanges import Recorder, ReplayMismatch, Replayer, ToolSnapshot, snapshot
from evals.metrics import assess
from travel_agent.domain import ProviderError, TripRequest
from travel_agent.providers import AMapProvider, ChatCompletionsModel


def test_corpus_has_hundred_unique_valid_cases():
    cases = build_cases()
    assert len(cases) == len({case.case_id for case in cases}) == 100
    assert Counter(Counter(case.scenario for case in cases).values()) == {10: 10}
    assert build_cases() == cases
    assert all(TripRequest.model_validate(case.request).days >= 1 for case in cases)
    assert build_cases(date(2031, 1, 1))[0].request["start_date"] == "2031-01-01"


def test_tool_snapshot_fault_and_replay_use_no_provider():
    tools = ToolSnapshot()
    recorder = Recorder("timeout", tools)
    calls = []

    def provider():
        calls.append("provider")
        return [{"poi_id": "p1", "lng": 121.4, "lat": 31.2}]

    arguments = {"city": "上海"}
    with pytest.raises(ProviderError, match="地点查询超时") as first_error:
        recorder("search_poi", arguments, provider)
    assert first_error.value.retryable
    result = recorder("search_poi", arguments, provider)
    result[0]["lng"] = 0
    assert recorder("search_poi", arguments, provider)[0]["lng"] == 121.4
    assert calls == ["provider"]
    assert [op["source"] for op in recorder.operations] == ["injected", "provider", "snapshot"]

    def forbidden():
        pytest.fail("Replay called the provider")

    replay = Replayer(snapshot(recorder.operations))
    with pytest.raises(ProviderError, match="地点查询超时"):
        replay("search_poi", arguments, forbidden)
    assert replay("search_poi", arguments, forbidden)[0]["lng"] == 121.4
    assert replay("search_poi", arguments, forbidden)[0]["lng"] == 121.4
    replay.finish()

    with pytest.raises(ReplayMismatch, match="different arguments"):
        Replayer(recorder.operations)("search_poi", {"city": "苏州"}, forbidden)
    strict = ToolSnapshot(tools.entries(), strict=True)
    assert strict.resolve("search_poi", arguments, forbidden)[1] == "snapshot"
    with pytest.raises(ReplayMismatch, match="Missing search_poi"):
        strict.resolve("search_poi", {"city": "苏州"}, forbidden)


def test_metrics_compare_coordinates_and_schedule_to_evidence():
    request = {
        "city": "上海", "start_date": "2030-10-01", "days": 1,
        "hotel": False, "required_places": ["上海博物馆"],
    }
    point = {"poi_id": "p1", "name": "上海博物馆", "lng": 121.4, "lat": 31.2, "indoor": True}
    operations = [
        {"operation": "search_poi", "result": [point]},
        {"operation": "get_weather", "result": [{"date": "2030-10-01", "summary": "小雨"}]},
    ]
    itinerary = {
        "city": "上海市", "hotel": None, "days": [{"date": "2030-10-01", "stops": [
            {**point, "start_time": "09:00", "end_time": "11:00"},
        ]}],
    }
    score = assess(request, itinerary, operations)
    assert score["grounded_plan"] and score["constraints_pass"]
    assert score["rain_indoor_stops"] == score["rain_evaluable_stops"] == 1
    itinerary["days"][0]["stops"][0].update(lng=121.5, end_time="08:00")
    score = assess(request, itinerary, operations)
    assert score["grounded_stops"] == 0
    assert score["checks"]["schedule"] is False


def test_live_adapters_accept_native_calls_and_normalize_grounded_points(monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "amap-test-key")
    monkeypatch.setenv("TRAVEL_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("TRAVEL_MODEL_API_KEY", "model-test-key")
    monkeypatch.setenv("TRAVEL_MODEL_NAME", "fixture-chat")
    tool_call = {
        "id": "call-1", "type": "function",
        "function": {"name": "search_poi", "arguments": '{"query":"博物馆"}'},
    }

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.invalid":
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer model-test-key"
            body = json.loads(request.content)
            assert body["model"] == "fixture-chat"
            assert body["tools"][0]["function"]["name"] == "search_poi"
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [tool_call]}}]})
        assert request.url.path == "/v5/place/text"
        assert request.url.params["key"] == "amap-test-key"
        assert request.url.params["city_limit"] == "true"
        return httpx.Response(200, json={"status": "1", "pois": [
            {"id": "correct", "name": "上海博物馆", "cityname": "上海市", "location": "121.475,31.230"},
            {"id": "wrong_city", "name": "其他城市博物馆", "cityname": "苏州市", "location": "120.6,31.3"},
            {"id": "bad_location", "name": "地图位置为空", "cityname": "上海市", "location": []},
        ]})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatCompletionsModel(client)
        message = model.complete({
            "messages": [{"role": "user", "content": "查博物馆"}],
            "tools": [{"type": "function", "function": {"name": "search_poi"}}],
        })
        assert message["tool_calls"] == [tool_call]
        poi, = AMapProvider(client).search_poi("上海", "博物馆")
        assert (poi["poi_id"], poi["lng"], poi["lat"], poi["evidence"]) == ("correct", 121.475, 31.230, "amap")


@pytest.mark.parametrize("strategy", ["fixed", "tools", "verified"])
def test_fixture_executes_recovers_and_replays(strategy):
    from evals.run import benchmark_case, replay_trace

    clean, clean_trace = benchmark_case(build_cases()[0], mode="fixture", strategy=strategy, profile="clean", tool_snapshot=ToolSnapshot())
    metrics, trace = benchmark_case(build_cases()[0], mode="fixture", strategy=strategy, profile="timeout", tool_snapshot=ToolSnapshot())
    assert clean["outcome"] == metrics["outcome"] == "succeeded"
    assert clean["injected_faults"] == 0
    assert metrics["injected_faults"] == 1
    assert metrics["replay_matches"]
    assert metrics["live_model_invocations"] == 0
    assert metrics["quality"]["grounded_plan"]
    assert metrics["quality"]["constraints_pass"]
    assert replay_trace(trace)["itinerary_sha256"] == clean_trace["result"]["itinerary_sha256"]
    trace["result"]["itinerary"]["city"] = "错城"
    with pytest.raises(ReplayMismatch, match="Final itinerary"):
        replay_trace(trace)


def test_fixture_preserves_indoor_candidates_for_multiple_rainy_days():
    from evals.run import benchmark_case

    case = next(case for case in build_cases() if case.case_id == "slow_pace-06")
    metrics, trace = benchmark_case(case, mode="fixture", strategy="verified", profile="clean", tool_snapshot=ToolSnapshot())
    assert metrics["quality"]["checks"]["nonempty_days"]
    dates = trace["result"]["itinerary"]["days"]
    assert sum("雨" in day["weather"]["summary"] for day in dates) == 2
    assert metrics["quality"]["rain_evaluable_stops"] == metrics["quality"]["rain_indoor_stops"]


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_live_trace_replays_without_credentials_or_requests(monkeypatch, transport):
    from evals.run import benchmark_case, replay_trace

    env = {
        "AMAP_WEB_KEY": "amap-test-key",
        "TRAVEL_MODEL_BASE_URL": "https://model.invalid/v1",
        "TRAVEL_MODEL_API_KEY": "model-test-key",
        "TRAVEL_MODEL_NAME": "test-chat",
        "AMAP_TRANSPORT": transport,
        "WENDAO_API_KEY": "read-only-test-token",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    allow_requests = True

    def handle(request: httpx.Request) -> httpx.Response:
        assert allow_requests, "Replay made a network request"
        match request.url.path:
            case "/mcp":
                payload = json.loads(request.content)
                if payload["method"] == "notifications/initialized":
                    return httpx.Response(202)
                if payload["method"] == "initialize":
                    result = {"protocolVersion": "2025-06-18"}
                else:
                    assert payload["method"] == "tools/call"
                    tool_name = payload["params"]["name"]
                    tool_data = {
                        "maps_text_search": {"pois": [{"id": "museum", "name": "上海博物馆", "cityname": "上海市", "location": "121.475,31.230"}]},
                        "maps_weather": {"forecasts": [{"date": "2030-10-01", "dayweather": "晴"}]},
                    }
                    result = {"content": [{"type": "text", "text": json.dumps(tool_data[tool_name])}]}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})
            case "/skills/api/crew/workbuddy/searchInfo":
                return httpx.Response(200, json={"result": "[景点](https://you.ctrip.com/sight/shanghai2/123.html)"})
            case "/sight/shanghai2/123.html":
                assert request.url.host == "you.ctrip.com"
                return httpx.Response(200, text="上海景点介绍")
            case "/mu/frontend/pg/service/visit-west":
                assert request.url.host == "www.shanghaimuseum.cn"
                return httpx.Response(200, text="上海博物馆参观信息", headers={"Content-Type": "text/html"})
            case "/search":
                assert request.url.host == "www.bing.com"
                return httpx.Response(200, content=b"<rss><channel/></rss>")
            case "/s":
                assert request.url.host == "www.so.com"
                return httpx.Response(200, text="<html></html>")
            case "/v3/config/district":
                return httpx.Response(200, json={"status": "1", "districts": [{"name": "上海市", "adcode": "310000"}]})
            case "/v3/weather/weatherInfo":
                return httpx.Response(200, json={"status": "1", "forecasts": [{"casts": [{"date": "2030-10-01", "dayweather": "晴"}]}]})
            case "/v5/place/text":
                return httpx.Response(200, json={"status": "1", "pois": [{
                    "id": "museum", "name": "上海博物馆", "cityname": "上海市", "location": "121.475,31.230",
                }]})
            case "/v1/chat/completions":
                itinerary = {"days": [{"date": "2030-10-01", "stops": [
                    {"poi_id": "museum", "start_time": "09:00", "end_time": "11:00"},
                ]}], "hotel_poi_id": None}
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(itinerary)}}]})
            case other:
                raise AssertionError(f"Unexpected request path {other}")

    client_class = httpx.Client

    def test_client(**kwargs):
        return client_class(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", test_client)
    case = TravelCase("live-http", "live", {"city": "上海", "start_date": "2030-10-01", "days": 1, "hotel": False})
    metrics, trace = benchmark_case(case, mode="live", strategy="fixed", profile="clean", tool_snapshot=ToolSnapshot())
    assert metrics["live_model_invocations"] == metrics["live_model_responses"] == 1
    assert metrics["replay_matches"] and metrics["quality"]["grounded_plan"]
    itinerary = trace["result"]["itinerary"]
    assert itinerary["days"][0]["stops"][0]["evidence"] == ("amap_mcp" if transport == "mcp" else "amap")
    assert itinerary["travel_links"][0]["source_url"] == "https://you.ctrip.com/sight/shanghai2/123.html"
    for name in env:
        monkeypatch.delenv(name)
    allow_requests = False
    assert replay_trace(trace)["itinerary_sha256"] == trace["result"]["itinerary_sha256"]


def test_unavailable_model_uses_only_grounded_live_places(monkeypatch):
    from travel_agent.planner import advance

    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **options: client_type(
        transport=httpx.MockTransport(lambda _request: pytest.fail("Replay should not use the network")), **options,
    ))
    request = TripRequest(city="西雅图", start_date="2026-11-16", days=2, hotel=False)
    places = [
        {"poi_id": "park", "name": "Kerry Park", "city": "西雅图", "lng": -122.3599, "lat": 47.6296,
         "category": "公园", "indoor": False, "evidence": "osm"},
        {"poi_id": "museum", "name": "Seattle Art Museum", "city": "西雅图", "lng": -122.3381, "lat": 47.6071,
         "category": "博物馆", "indoor": True, "evidence": "osm"},
    ]
    attempts = 0

    def exchange(operation, arguments, _perform):
        nonlocal attempts
        if operation == "model":
            attempts += 1
            raise ProviderError("Model_unavailable", "模型连接中断", retryable=True)
        return {"search_guides": [], "search_poi": places, "get_weather": [],
                "quote_attractions": {}, "ctrip_search": []}[operation]

    state = {}
    for stage in ("research", "draft", "validate"):
        state = advance(stage, request, state, mode="live", strategy="verified", exchange=exchange)
    itinerary = state["itinerary"]
    assert attempts == 1
    assert all(day["stops"] for day in itinerary["days"])
    assert {stop["poi_id"] for day in itinerary["days"] for stop in day["stops"]} == {"park", "museum"}
    assert any("模型服务暂时不可用" in warning for warning in itinerary["warnings"])
