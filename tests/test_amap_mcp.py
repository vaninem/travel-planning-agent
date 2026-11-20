import json

import httpx
import pytest

from travel_agent.amap_mcp import AMAP_MCP_URL, AMapMCPProvider, StreamableMCP, amap_mcp_available
from travel_agent.domain import ProviderError


def _reply(request: dict, result: dict, *, session: str | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": request["id"], "result": result},
        headers={"Mcp-Session-Id": session} if session else {},
    )


def test_mcp_is_opted_in_with_a_dedicated_key_or_a_transport_override(monkeypatch):
    for var in ("AMAP_TRANSPORT", "AMAP_MCP_KEY", "AMAP_MAPS_API_KEY", "AMAP_API_KEY", "AMAP_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AMAP_WEB_KEY", "stored-rest-key")
    assert not amap_mcp_available()
    monkeypatch.setenv("AMAP_MCP_KEY", "mock-key")
    assert amap_mcp_available()
    monkeypatch.setenv("AMAP_TRANSPORT", "rest")
    assert not amap_mcp_available()
    monkeypatch.delenv("AMAP_MCP_KEY")
    monkeypatch.setenv("AMAP_TRANSPORT", "mcp")
    assert amap_mcp_available()


def test_supported_amap_tools_use_the_real_mcp_protocol_and_keep_only_verified_city(monkeypatch):
    monkeypatch.setenv("AMAP_MCP_KEY", "mock-key")
    calls = []

    def handle(request):
        assert request.url.host == "mcp.amap.com" and request.url.path == "/mcp"
        assert request.url.params["key"] == "mock-key"
        assert "text/event-stream" in request.headers["Accept"]
        message = json.loads(request.content)
        if message["method"] == "initialize":
            assert message["params"]["protocolVersion"] == "2025-06-18"
            return _reply(message, {"protocolVersion": "2025-03-26"}, session="local-session")
        assert request.headers["Mcp-Session-Id"] == "local-session"
        assert request.headers["MCP-Protocol-Version"] == "2025-03-26"
        if message["method"] == "notifications/initialized":
            assert "id" not in message
            return httpx.Response(202)
        assert message["method"] == "tools/call"
        name, args = (message["params"][key] for key in ("name", "arguments"))
        calls.append((name, args))
        if name == "maps_text_search":
            result = {"pois": [
                {"id": "expired", "name": "已失效的地点"},
                {"id": "museum", "name": "云南省博物馆", "typecode": "140100"},
                {"id": "wrong", "name": "大理景点", "location": "100,26", "cityname": "大理市"},
                {"id": "museum", "name": "重复结果"},
            ]}
        elif name == "maps_search_detail":
            if args["id"] == "expired":
                return _reply(message, {"isError": True, "content": [{"type": "text", "text": "地点详情查询失败"}]})
            result = {"id": "museum", "name": "云南省博物馆", "city": "昆明市",
                      "location": "102.76,24.96", "type": "博物馆"}
        elif name == "maps_weather":
            result = {"city": "昆明", "forecasts": [{"date": "2026-11-17", "dayweather": "晴",
                                                   "nighttemp": "12", "daytemp": "20"}]}
        else:
            assert name == "maps_direction_transit_integrated"
            result = {"route": {"transits": [{"duration": "732", "distance": "4500", "cost": "3"}]}}
        response = {"content": [{"type": "text", "text": json.dumps(result)}]}
        if name == "maps_weather":
            frame = {"id": message["id"], "jsonrpc": "2.0", "result": response}
            event_stream = ": heartbeat\n\nevent: message\ndata: " + json.dumps(frame) + "\n\n"
            return httpx.Response(200, text=event_stream, headers={"Content-Type": "text/event-stream; charset=utf-8"})
        return _reply(message, response)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        provider = AMapMCPProvider(client)
        poi, = provider.search_poi("昆明", "旅游景点")
        assert (poi["poi_id"], poi["city"], poi["lng"], poi["evidence"]) == ("museum", "昆明市", 102.76, "amap_mcp")
        assert poi["source_url"].startswith("https://uri.amap.com/marker?")
        day, = provider.get_weather("昆明", ["2026-11-17", "2026-11-18"])
        assert (day["date"], day["summary"], day["high_c"], day["evidence"]) == ("2026-11-17", "晴", 20, "amap_mcp")
        route = provider.get_route("昆明", "transit", poi, {**poi, "lng": 102.78})
        assert route == {"distance_m": 4500, "duration_minutes": 13, "polyline": [], "evidence": "amap_mcp", "fare_cny": 3}
        assert calls == [
            ("maps_text_search", {"city": "昆明", "keywords": "旅游景点"}),
            ("maps_search_detail", {"id": "expired"}),
            ("maps_search_detail", {"id": "museum"}),
            ("maps_weather", {"city": "昆明"}),
            ("maps_direction_transit_integrated", {"origin": "102.760000,24.960000",
                                                   "destination": "102.780000,24.960000",
                                                   "city": "昆明市", "cityd": "昆明市"}),
        ]


def test_mcp_reopens_expired_session_and_redacts_tool_errors():
    initialized = 0

    def handle(request):
        nonlocal initialized
        message = json.loads(request.content)
        if message["method"] == "initialize":
            initialized += 1
            assert "Mcp-Session-Id" not in request.headers
            return _reply(message, {"protocolVersion": "2025-06-18"}, session=f"session-{initialized}")
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        if initialized == 1:
            return httpx.Response(404)
        return _reply(message, {"isError": True, "content": [{"type": "text", "text": "10003 fake-private-token"}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = StreamableMCP(client, AMAP_MCP_URL, service="高德")
        with pytest.raises(ProviderError) as error:
            transport.call_tool("maps_weather", {"city": "北京"})
    assert initialized == 2
    assert error.value.retryable and "fake-private-token" not in str(error.value)


def test_weather_falls_back_to_the_verified_city_adcode(monkeypatch):
    monkeypatch.setenv("AMAP_MCP_KEY", "mock-key")

    def handle(request):
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return _reply(message, {"protocolVersion": "2025-06-18"})
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        tool = message["params"]
        if tool["name"] == "maps_geo":
            data = {"return": [
                {"city": "苏州市", "adcode": "320500"},
                {"city": [], "province": "天津市", "adcode": "120000"},
            ]}
        elif tool["arguments"] == {"city": "天津"}:
            return _reply(message, {"isError": True, "content": [{"type": "text", "text": "INVALID_PARAMS"}]})
        else:
            assert tool["arguments"] == {"city": "120000"}
            data = {"forecasts": [{"city": "天津市", "casts": [
                {"date": "2026-11-21", "dayweather": "晴", "daytemp": "8", "nighttemp": "-1"},
            ]}]}
        return _reply(message, {"structuredContent": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        day, = AMapMCPProvider(client).get_weather("天津", ["2026-11-21"])
    assert day["date"] == "2026-11-21" and day["low_c"] == -1
