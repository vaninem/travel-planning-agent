"""Read-only Ctrip Wendao search. Its generated answers supply links, not price evidence."""

import json
import os
import re
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import httpx

from .domain import ProviderError, TripRequest


WENDAO_URL = "https://externalcallback.ctrip.com/skills/api/crew/workbuddy/searchInfo"
URL_PATTERN = re.compile(r"https://[^\s<>\[\]()（）【】、，。！？\"']+")
REDIRECT_PARAMETERS = {"redirect", "redirecturl", "url", "returnurl", "backurl", "next", "continue", "target"}
LINK_CHECK_LIMIT = 8
LINK_TIMEOUT = httpx.Timeout(2.0, connect=1.0)


def ctrip_available() -> bool:
    return bool(os.getenv("WENDAO_API_KEY", "").strip())


def _link_kind(url: str) -> str | None:
    if len(url) > 2000 or "\\" in url:
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    if any(re.sub(r"[^a-z]", "", key.lower()) in REDIRECT_PARAMETERS for key, _ in parse_qsl(parsed.query)):
        return None

    path = parsed.path.lower()
    if host == "ctrip.com" or host.endswith(".ctrip.com"):
        if (host == "hotels.ctrip.com" and re.fullmatch(r"/hotels/\d+\.html", path)) or (
            host in ("hotels.ctrip.com", "m.ctrip.com") and re.search(r"/(?:hotel|hotels)/detail(?:/|$)", path)
        ):
            return "hotel"
        if host == "you.ctrip.com" and re.fullmatch(r"/(?:destinationsite/)?sight/[^/]+/[^/]+\.html", path):
            return "attraction"
    if host == "trip.com" or host.endswith(".trip.com"):
        if path.startswith("/hotels/") and "-hotel-detail-" in path:
            return "hotel"
        if path.startswith(("/travel-guide/attraction/", "/things-to-do/detail/")) and len(path.rstrip("/").split("/")) >= 4:
            return "attraction"
    return None


class CtripWendao:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def _verify_link(self, url: str, kind: str, token: str) -> str | None:
        method = "HEAD"
        redirects = 0
        for _ in range(3):
            if token in unquote(url) or _link_kind(url) != kind:
                return None
            try:
                request = httpx.Request(method, url, extensions={"timeout": LINK_TIMEOUT.as_dict()})
                response = self.client.send(request, stream=True, auth=None, follow_redirects=False)
            except (httpx.RequestError, httpx.InvalidURL):
                return None
            try:
                status, location = response.status_code, response.headers.get("location")
            finally:
                response.close()
            if 200 <= status < 300:
                return url
            if status in (301, 302, 303, 307, 308) and location and redirects == 0:
                try:
                    url = urljoin(url, location)
                except ValueError:
                    return None
                redirects += 1
            elif status in (403, 405) and method == "HEAD":
                method = "GET"
            else:
                return None
        return None

    def search_links(self, request: TripRequest) -> list[dict[str, str]]:
        token = os.getenv("WENDAO_API_KEY", "").strip()
        if not token:
            return []
        with_lodging = request.hotel and request.days > 1
        parameters = {
            "city": request.city,
            "dates": request.dates(),
            "travelers": request.travelers,
            "request_hotels": with_lodging,
            "total_budget_cny": request.budget,
        }
        query = (
            "请按下面的旅行条件查询可查看的景点门票页面。仅当需要住宿时，也返回酒店详情页。"
            "给出实际检索到的携程或 Trip.com HTTPS 直达链接，每类最多四条。仅查询，勿下单或预订。\n"
            + json.dumps(parameters, ensure_ascii=False)
        )
        try:
            result = self.client.post(
                WENDAO_URL, json={"inputs": {"token": token, "query": query}}, follow_redirects=False,
            )
            result.raise_for_status()
            payload = result.json()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            raise ProviderError("ctrip_http", f"携程问道查询返回 HTTP {status}", retryable=status in (408, 429) or status >= 500) from None
        except httpx.RequestError:
            raise ProviderError("ctrip_unavailable", "携程问道暂无法连接", retryable=True) from None
        except ValueError:
            raise ProviderError("ctrip_payload", "携程问道返回的数据无法读取") from None

        if not isinstance(payload, dict) or payload.get("error"):
            raise ProviderError("ctrip_payload", "携程问道未返回可用查询结果")
        content = payload.get("result")
        if isinstance(content, dict):
            content = content.get("content")
        if not isinstance(content, str):
            return []

        links: list[dict[str, str]] = []
        counts = {"hotel": 0, "attraction": 0}
        seen = set()
        checked = set()
        for raw in URL_PATTERN.findall(content[:100_000]):
            url = raw.rstrip(".,;:!?")
            kind = _link_kind(url)
            if kind is None or (kind == "hotel" and not with_lodging) or token in unquote(url) or url in checked or counts[kind] >= 4:
                continue
            if len(checked) >= LINK_CHECK_LIMIT:
                break
            checked.add(url)
            verified = self._verify_link(url, kind, token)
            if verified is None or verified in seen:
                continue
            seen.add(verified)
            counts[kind] += 1
            label = "酒店详情" if kind == "hotel" else "景点与票务"
            links.append({"label": label, "source_name": "携程问道", "source_url": verified, "kind": kind})
        return links
