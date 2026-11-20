import json

import httpx
import pytest

from travel_agent.ticket_verification import _quote, _seasonal_table_quote, verify_web_tickets


STOP = {"poi_id": "museum", "name": "测试美术博物馆", "city": "测试市"}
DATE = "2026-11-17"
URL = "https://museum.gov.cn/tickets"
VALIDITY = "自2025年5月12日起执行，有效期2年。"
PRICE = "普通成人票85元"


def _claim(**updates):
    return {"item": 0, "source": 0, "amount": 85, "currency": "CNY", "venue_evidence": STOP["name"],
            "price_evidence": PRICE, "date_evidence": VALIDITY, **updates}


def test_search_follows_public_result_and_verifies_body(monkeypatch):
    monkeypatch.setenv("TRAVEL_TICKET_WEB", "1")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    def respond(request):
        if request.url.host == "www.so.com":
            return httpx.Response(200, text='<h3><a data-mdurl="https://example.org/blog">游客攻略</a></h3>')
        if request.url.host == "www.bing.com":
            feed = f"<rss><channel><item><link>{URL.replace('https://', 'http://')}</link></item></channel></rss>"
            return httpx.Response(200, text=feed, headers={"Content-Type": "text/xml"})
        assert str(request.url) == URL
        html = f"<h1>{STOP['name']}</h1><p>门票 {PRICE}</p><p>{VALIDITY}</p>"
        return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})

    class Extractor:
        def complete(self, request):
            assert URL in request["messages"][1]["content"]
            return {"content": json.dumps({"quotes": [_claim()]})}

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        quote = verify_web_tickets(client, Extractor(), [(STOP, DATE)])["museum"]
    assert (quote["amount"], quote["visit_date"], quote["source_url"]) == (85, DATE, URL)
    assert PRICE in quote["notes"][0]


def test_checks_visits_after_the_first_model_batch(monkeypatch):
    monkeypatch.setenv("TRAVEL_TICKET_WEB", "1")
    monkeypatch.setattr("travel_agent.ticket_verification._discover", lambda client, stop: [{
        "url": URL, "kind": "official", "excerpts": [PRICE], "text": f"{stop['name']} {PRICE} {VALIDITY}",
    }])

    class Extractor:
        def complete(self, request):
            items = json.loads(request["messages"][1]["content"])
            assert len(items) <= 4
            return {"content": json.dumps({"quotes": [_claim(item=item["item"], venue_evidence=item["venue"]) for item in items]})}

    visits = [({**STOP, "poi_id": str(index), "name": f"{STOP['name']}{index}"}, DATE) for index in range(6)]
    with httpx.Client(trust_env=False) as client:
        assert set(verify_web_tickets(client, Extractor(), visits)) == set(str(index) for index in range(6))


def test_generic_seasonal_table_uses_the_requested_month_and_exact_venue():
    page = {"url": URL, "kind": "official", "tables": [[
        ["序号", "景点名称", "旺季门票（月）", "淡季门票（月）"],
        ["4、5、7、8、9、10", "1、2、3、6、11、12"],
        ["1", "测试其他馆", "130 元", "120"],
        ["2", STOP["name"], "80 元", "70"],
    ]]}
    assert _seasonal_table_quote(STOP, DATE, page)["amount"] == 70
    assert _seasonal_table_quote(STOP, "2026-05-17", page)["amount"] == 80
    assert _seasonal_table_quote({**STOP, "name": "测试美术博物馆分馆"}, DATE, page) is None


@pytest.mark.parametrize(("price", "validity", "amount", "day", "kind", "expected"), [
    (PRICE, VALIDITY, 85, DATE, "official", True),
    (PRICE, VALIDITY, 85, "2027-05-12", "official", False),
    (PRICE, VALIDITY, 1, DATE, "official", False),
    ("普通成人票85元起", VALIDITY, 85, DATE, "official", False),
    ("儿童门票85元", VALIDITY, 85, DATE, "official", False),
    (PRICE, "可订今日", 85, DATE, "seller", False),
    (PRICE, "可订2026-11-17", 85, DATE, "seller", True),
    ("每年11月1日至次年3月31日为淡季，大门票40元/人", "每年11月1日至次年3月31日", 40, DATE, "official", True),
    ("每年4月1日至10月31日为旺季，大门票60元/人", "每年4月1日至10月31日", 60, DATE, "official", False),
    ("大门票60元/人", "每年11月1日至次年3月31日", 60, DATE, "official", False),
])
def test_only_accepts_matching_adult_price_and_applicable_date(price, validity, amount, day, kind, expected):
    page = {"url": URL, "kind": kind, "text": f"{STOP['name']} 门票 {price} {validity}"}
    claim = _claim(price_evidence=price, date_evidence=validity, amount=amount)
    assert bool(_quote(claim, STOP, day, page)) is expected
