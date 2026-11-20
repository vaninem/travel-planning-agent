"""Use AMap's Streamable HTTP MCP transport with the planner's existing result shapes."""

from itertools import count
import json
import re

import httpx

from .domain import ProviderError
from .providers import (
    AMAP_RETRYABLE,
    AMAP_URL,
    AMapProvider,
    _env,
    _fetch,
    _integer,
    _items,
    _location,
    _text,
    city_key,
)


AMAP_MCP_URL = "https://mcp.amap.com/mcp"
SUPPORTED_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}


def _mcp_key() -> str:
    key = _env("AMAP_MCP_KEY", "AMAP_MAPS_API_KEY")
    if not key and _env("AMAP_TRANSPORT").lower() == "mcp":
        key = _env("AMAP_WEB_KEY", "AMAP_API_KEY", "AMAP_KEY")
    return key


def amap_mcp_available() -> bool:
    return _env("AMAP_TRANSPORT").lower() != "rest" and bool(_mcp_key())


class _SessionExpired(Exception):
    pass


class StreamableMCP:
    """Small synchronous MCP client for request/response calls to read-only tools."""

    def __init__(
        self, client: httpx.Client, url: str, *, service: str, params: dict | None = None,
        headers: dict | None = None,
    ) -> None:
        self.client = client
        self.timeout: httpx.Timeout | float = client.timeout
        self.url = url
        self.params = params
        self.service = service
        self.headers = {
            **(headers or {}), "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
        }
        self.session_id: str | None = None
        self.version: str | None = None
        self.request_ids = count(1)

    def _error(self, kind: str, message: str, *, retryable: bool = False) -> ProviderError:
        return ProviderError(f"{self.service}_mcp_{kind}", f"{self.service} MCP {message}", retryable=retryable)

    @staticmethod
    def _stream_response(response: httpx.Response, request_id: int) -> dict:
        lines = []
        for line in response.iter_lines():
            if line.startswith("data:"):
                lines.append(line[5:].removeprefix(" "))
            elif not line and lines:
                message = json.loads("\n".join(lines))
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
                lines.clear()
        if lines:
            message = json.loads("\n".join(lines))
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
        return {}

    def _request(self, method: str, params: dict | None = None, *, notification: bool = False) -> dict:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        request_id = None if notification else next(self.request_ids)
        if request_id is not None:
            message["id"] = request_id
        headers = dict(self.headers)
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        if self.version is not None:
            headers["MCP-Protocol-Version"] = self.version
        try:
            with self.client.stream("POST", self.url, json=message, params=self.params, headers=headers, timeout=self.timeout) as response:
                status = response.status_code
                if status == 404 and self.session_id is not None:
                    raise _SessionExpired
                if not response.is_success:
                    raise self._error(f"http_{status}", f"返回 HTTP {status}", retryable=status in (408, 425, 429) or status >= 500)
                if notification:
                    return {}
                if method == "initialize":
                    self.session_id = response.headers.get("Mcp-Session-Id")
                media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if media_type == "text/event-stream":
                    reply = self._stream_response(response, request_id)
                else:
                    response.read()
                    reply = response.json()
        except httpx.TimeoutException:
            raise self._error("timeout", "请求超时", retryable=True) from None
        except httpx.RequestError:
            raise self._error("connection", "连接失败", retryable=True) from None
        except (UnicodeError, ValueError):
            raise self._error("payload", "返回内容无法解析") from None
        if not isinstance(reply, dict) or reply.get("id") != request_id or reply.get("jsonrpc") != "2.0":
            raise self._error("protocol", "未返回对应的 JSON-RPC 响应")
        if "error" in reply:
            raise self._error("rpc", f"{method} 调用失败")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise self._error("payload", "返回结果格式不受支持")
        return result

    def _initialize(self) -> None:
        result = self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "travel-planning-agent", "version": "0.1.0"},
        })
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in SUPPORTED_VERSIONS:
            raise self._error("version", "协议版本不受支持")
        self.version = version
        try:
            self._request("notifications/initialized", notification=True)
        except (ProviderError, _SessionExpired):
            self.session_id = self.version = None
            raise

    def call_tool(self, name: str, arguments: dict) -> dict:
        for attempt in range(2):
            try:
                if self.version is None:
                    self._initialize()
                result = self._request("tools/call", {"name": name, "arguments": arguments})
                break
            except _SessionExpired:
                self.session_id = self.version = None
                if attempt == 1:
                    raise self._error("session", "会话失效", retryable=True) from None
        if result.get("isError"):
            content = " ".join(_text(item.get("text")) for item in _items(result.get("content")))
            codes = set(re.findall(r"\b\d{5}\b", content))
            transient = bool(codes & AMAP_RETRYABLE) or any(
                term in content for term in ("OVER_LIMIT", "TIMEOUT", "SERVICE_UNAVAILABLE", "服务器繁忙")
            )
            raise self._error("tool", f"{name} 查询失败", retryable=transient)
        value = result.get("structuredContent")
        if isinstance(value, dict):
            return value
        for item in _items(result.get("content")):
            if item.get("type") == "text":
                try:
                    value = json.loads(_text(item.get("text")))
                except ValueError:
                    continue
                if isinstance(value, dict):
                    return value
        raise self._error("payload", f"{name} 缺少结构化结果")


class AMapMCPProvider(AMapProvider):
    def __init__(self, client: httpx.Client) -> None:
        super().__init__(client)
        self.key = _mcp_key()
        self.mcp = StreamableMCP(client, AMAP_MCP_URL, service="高德", params={"key": self.key})

    def _call(self, tool: str, args: dict) -> dict:
        if not self.key:
            raise ProviderError("amap_mcp_config", "请配置高德 MCP 密钥 AMAP_MCP_KEY")
        result = self.mcp.call_tool(tool, args)
        if str(result.get("status")) == "0":
            code = _text(result.get("infocode"))
            raise ProviderError("amap_mcp_tool", "高德 MCP 查询失败", retryable=code in AMAP_RETRYABLE)
        return result

    def _poi_rows(self, params: dict) -> dict:
        city = params["region"]
        default_query = "酒店" if params.get("types") == "100000" else "旅游景点"
        query = params.get("keywords") or default_query
        result = self._call("maps_text_search", {"city": city, "keywords": query})
        accepted, seen = [], set()
        detail_error = None
        for row in _items(result.get("pois"))[:12]:
            poi_id = _text(row.get("id"))
            if not poi_id or poi_id in seen:
                continue
            seen.add(poi_id)
            provider_city = _text(row.get("cityname")) or _text(row.get("city"))
            if not provider_city or _location(row.get("location")) is None:
                try:
                    detail = self._call("maps_search_detail", {"id": poi_id})
                except ProviderError as error:
                    if detail_error is None or error.retryable:
                        detail_error = error
                    continue
                candidates = _items(detail.get("pois"))
                detail = candidates[0] if candidates else detail
                if _text(detail.get("id")) != poi_id:
                    continue
                row = {**row, **detail}
                provider_city = _text(row.get("cityname")) or _text(row.get("city"))
            if provider_city and city_key(provider_city) == city_key(city):
                accepted.append({**row, "cityname": provider_city})
        if not accepted and detail_error is not None:
            raise detail_error
        return {"pois": accepted}

    def _get(self, endpoint: str, params: dict) -> dict:
        if endpoint == "/v5/place/text":
            return self._poi_rows(params)
        if endpoint.startswith("/v5/direction/"):
            args = {key: params[key] for key in ("origin", "destination")}
            suffix = endpoint.removeprefix("/v5/direction/")
            if suffix == "transit/integrated":
                suffix = "transit_integrated"
                args.update(city=params["city1"], cityd=params["city2"])
            data = self._call(f"maps_direction_{suffix}", args)
            route = data.get("route", data)
            if not isinstance(route, dict):
                return {}
            if suffix == "transit_integrated":
                transits = []
                for trip in _items(route.get("transits")):
                    cost = trip.get("cost")
                    cost = dict(cost) if isinstance(cost, dict) else {"transit_fee": cost}
                    transits.append({**trip, "cost": cost})
                route = {**route, "transits": transits}
            return {"route": route}
        # Booking property verification can require coordinate APIs absent from the MCP surface.
        if _env("AMAP_WEB_KEY", "AMAP_API_KEY", "AMAP_KEY"):
            return super()._get(endpoint, params)
        if endpoint not in {"/v3/assistant/coordinate/convert", "/v3/geocode/regeo"}:
            raise ProviderError("amap_mcp_tool", "高德 MCP 未提供此查询")
        data = _fetch("AMap", lambda: self.client.get(f"{AMAP_URL}{endpoint}", params={**params, "key": self.key}))
        if str(data.get("status")) != "1":
            raise ProviderError("amap_mcp_coordinates", "高德坐标核验失败")
        return data

    def _places(self, city: str, query: str, *, hotels: bool = False) -> list[dict]:
        rows = super()._places(city, query, hotels=hotels)
        return [{**row, "evidence": "amap_mcp"} for row in rows]

    def get_weather(self, city: str, dates: list[str]) -> list[dict]:
        try:
            payload = self._call("maps_weather", {"city": city})
        except ProviderError as error:
            if error.retryable or not error.code.endswith("_mcp_tool"):
                raise
            payload = {}
        if not _items(payload.get("forecasts")):
            geocodes = self._call("maps_geo", {"address": city, "city": city})
            for row in _items(geocodes.get("geocodes") or geocodes.get("return")):
                name = _text(row.get("city")) or _text(row.get("province"))
                code = _text(row.get("adcode"))
                if city_key(name) == city_key(city) and code.isdecimal():
                    payload = self._call("maps_weather", {"city": code})
                    break
        provider_city = _text(payload.get("city"))
        if provider_city and city_key(provider_city) != city_key(city):
            return []
        result = {}
        for row in _items(payload.get("forecasts")):
            if _text(row.get("city")) and city_key(row["city"]) != city_key(city):
                continue
            for forecast in _items(row.get("casts")) or [row]:
                if forecast.get("date") in dates:
                    result[forecast["date"]] = self._forecast(forecast, _text(row.get("reporttime")))
        return [result[day] for day in dates if day in result]

    @staticmethod
    def _forecast(row: dict, report_time: str) -> dict:
        return {
            "date": row["date"], "summary": _text(row.get("dayweather")) or "天气未知",
            "low_c": _integer(row.get("nighttemp")), "high_c": _integer(row.get("daytemp")),
            "as_of": report_time, "evidence": "amap_mcp",
        }

    def get_route(self, city: str, transport: str, origin: dict, destination: dict) -> dict | None:
        if transport == "transit":
            origin = {**origin, "city_code": origin.get("city") or city}
            destination = {**destination, "city_code": destination.get("city") or city}
        route = super().get_route(city, transport, origin, destination)
        return {**route, "evidence": "amap_mcp"} if route is not None else None
