import httpx

from travel_agent.api import _job_view
from travel_agent.budget import build_budget_summary
from travel_agent.domain import TripRequest
from travel_agent.edits import change_day
from travel_agent.planner import PlanCheck, PlanningSession, advance
from travel_agent.pricing import official_admission


def plan(**changes):
    request = TripRequest.model_validate({
        "city": "上海", "start_date": "2030-10-01", "days": 1, "travelers": 1,
        "transport": "walking", "avoid_outdoors_in_rain": False, **changes,
    })
    state = {}
    for stage in ("research", "draft", "validate"):
        state = advance(stage, request, state)
    return state["itinerary"]


def test_tight_budget_prefers_lower_cost_without_dropping_required_stop():
    preferences = {"city": "北京", "interests": ["科技馆", "园林"], "required_places": ["故宫博物院"]}
    unrestricted = plan(**preferences)
    inexpensive = plan(**preferences, budget=125)
    assert inexpensive["hotel"] is None
    assert "budget_summary" not in unrestricted
    assert "lodging" not in {item["key"] for item in inexpensive["budget_summary"]["items"]}
    stops = inexpensive["days"][0]["stops"]
    assert any(stop["name"] == "故宫博物院" for stop in stops)
    assert inexpensive["budget_summary"]["status"] == "within"
    assert sum(stop["pricing"].get("amount", 0) for stop in stops) < sum(
        stop["pricing"].get("amount", 0) for stop in unrestricted["days"][0]["stops"]
    )


def test_live_budget_counts_unverified_tickets_as_provisional_zero():
    request = TripRequest.model_validate({
        "city": "北京", "start_date": "2030-10-01", "days": 2,
        "travelers": 2, "transport": "transit", "budget": 500,
    })
    source = "https://example.test/ticket"
    first = {"poi_id": "first", "pricing": {"status": "quoted", "amount": 60, "source_url": source}}
    second = {"poi_id": "second", "pricing": {"status": "unknown"}}
    route = {"from_poi_id": "first", "to_poi_id": "second", "fare_cny": 5, "evidence": "amap"}
    lodging = {"pricing": {"status": "quoted", "total": 200, "nightly": 200, "source_url": source}}
    days = [{"stops": [first, second], "legs": [route]}]
    budget = build_budget_summary(request, days, lodging, mode="live")
    assert budget["estimated_total"] == 570
    assert budget["estimated_subtotal"] == 570
    assert (budget["status"], budget["remaining"]) == ("over", -70)
    tickets = next(item for item in budget["items"] if item["key"] == "tickets")
    assert (tickets["amount"], tickets["known_amount"], tickets["unknown_count"]) == (120, 120, 0)
    assert budget["provisional_tickets"] == 1

    request = request.model_copy(update={"budget": 700})
    budget = build_budget_summary(request, days, lodging, mode="live")
    assert budget["status"] == "within"
    assert budget["remaining"] == 130

    budget = build_budget_summary(request, days, None, mode="live")
    assert budget["status"] == "partial" and budget["estimated_total"] is None


def test_historical_nanjing_plan_displays_verified_fares_without_double_charging_the_wall():
    request = TripRequest(city="南京", start_date="2026-11-17", days=2, travelers=2, budget=2000, hotel=False, transport="walking")
    names = ["南京博物院", "红山森林动物园", "环洲", "古鸡鸣寺", "南京城墙景区(神策门至太平门)", "南京城墙台城景区"]
    stops = [{"poi_id": name, "name": name, "city": "南京市", "pricing": {"status": "unknown"}} for name in names]
    stops[2]["address"] = "玄武巷1号玄武湖公园内"
    trip = {"days": [{"date": date, "stops": day} for date, day in zip(request.dates(), (stops[:3], stops[3:]), strict=True)]}
    resolved = _job_view({"request": request.model_dump(mode="json"), "mode": "live", "itinerary": trip})["itinerary"]
    assert [stop["pricing"]["amount"] for stop in stops] == [0, 70, 0, 10, 30, 30]
    assert all(stop["pricing"]["source_url"].startswith("https://") for stop in stops)
    budget = resolved["budget_summary"]
    assert budget["provisional_tickets"] == 0
    assert next(line for line in budget["items"] if line["key"] == "tickets")["known_amount"] == 220


def test_live_optimizer_checks_actual_ticket_before_replacing_a_stop(monkeypatch):
    request = TripRequest(city="苏州", start_date="2026-11-17", days=1, budget=110, hotel=False, transport="walking")
    stop = {"poi_id": "original", "name": "原景点", "city": "苏州", "lng": 120.6, "lat": 31.3,
            "pricing": {"status": "quoted", "amount": 100}, "start_time": "09:00", "end_time": "11:00"}
    expensive = {**stop, "poi_id": "expensive", "name": "较贵景点", "pricing": {"status": "unknown"}}
    affordable = {**stop, "poi_id": "affordable", "name": "可负担景点", "pricing": {"status": "unknown"}, "lng": 120.61}

    def verify(client, model, visits):
        amounts = {"expensive": 160, "affordable": 40}
        return {place["poi_id"]: {"status": "quoted", "amount": amounts[place["poi_id"]]} for place, _ in visits}

    monkeypatch.setattr("travel_agent.planner.verify_web_tickets", verify)
    with httpx.Client(trust_env=False) as client:
        session = PlanningSession(request, {"places": {p["poi_id"]: p for p in (expensive, affordable)}},
                                  mode="live", strategy="verified", client=client, exchange=None)
        monkeypatch.setattr(session.provider, "quote_attractions", lambda visits: {})
        result = session.fit_budget(PlanCheck([{"date": request.dates()[0], "stops": [stop], "legs": []}], None, []))
    selected = result.days[0]["stops"][0]
    assert (selected["poi_id"], selected["pricing"]["amount"]) == ("affordable", 40)
    assert build_budget_summary(request, result.days, None, mode="live")["estimated_subtotal"] == 100


def test_manual_live_replacement_checks_new_ticket_before_saving(monkeypatch):
    request = TripRequest(city="苏州", start_date="2026-11-17", days=1, budget=200, hotel=False, transport="walking")
    stop = {"poi_id": "original", "name": "原景点", "city": "苏州", "start_time": "09:00", "end_time": "11:00"}
    replacement = {"poi_id": "new", "name": "新景点", "city": "苏州"}
    job = {"mode": "live", "request": request.model_dump(mode="json"), "itinerary": {"candidates": [replacement],
           "days": [{"date": request.dates()[0], "stops": [stop], "legs": []}]}}

    def quote(request, day):
        assert day["stops"][0]["poi_id"] == "new"
        day["stops"][0]["pricing"] = {"status": "quoted", "amount": 70, "visit_date": day["date"]}

    monkeypatch.setattr("travel_agent.planner.quote_edited_day", quote)
    result = change_day(job, 0, ["new"])
    assert result["days"][0]["stops"][0]["pricing"]["amount"] == 70
    assert result["budget_summary"]["estimated_subtotal"] == 130


def test_budget_uses_verified_upgrade_and_room_under_cap():
    request = TripRequest(city="上海", start_date="2026-11-17", days=2, travelers=2, budget=2000,
                          transport="walking", required_places=["西侧公园"])
    park = {"poi_id": "park", "city": "上海", "name": "西侧公园", "lng": 121.48, "lat": 31.23}
    promenade = {**park, "poi_id": "promenade", "name": "滨江步道"}
    pearl = {**park, "poi_id": "pearl", "name": "东方明珠广播电视塔"}
    small = {"poi_id": "small", "pricing": {"status": "quoted", "total": 1200}}
    large = {"poi_id": "large", "pricing": {"status": "quoted", "total": 1560}}
    state = {"places": {place["poi_id"]: place for place in (park, promenade, pearl)},
             "hotels": {hotel["poi_id"]: hotel for hotel in (small, large)}}
    session = PlanningSession(request, state, mode="live", strategy="fixed", client=None, exchange=None)
    days = [{"date": day, "legs": [], "stops": [{**place, "start_time": "10:00", "end_time": "11:00"}]}
            for day, place in zip(request.dates(), (park, promenade), strict=True)]
    result = session.fit_budget(PlanCheck(days, large, []))
    assert result.hotel["poi_id"] == "small"
    assert [day["stops"][0]["poi_id"] for day in result.days] == ["park", "pearl"]
    budget = build_budget_summary(request, result.days, result.hotel, mode="live")
    assert (budget["estimated_total"], budget["remaining"]) == (1838, 162)


def test_fixture_group_lodging_lists_each_night_and_total():
    itinerary = plan(days=3, travelers=3, budget=3000)
    pricing = itinerary["hotel"]["pricing"]
    assert pricing["status"] == "sample"
    assert (pricing["nights"], pricing["rooms"]) == (2, 2)
    assert pricing["total"] == sum(quote["amount"] for quote in pricing["per_night"])
    budget_lodging = next(item for item in itinerary["budget_summary"]["items"] if item["key"] == "lodging")
    assert budget_lodging["amount"] == pricing["total"]


def test_official_ticket_price_uses_museum_location_and_visit_dates():
    itinerary = plan(start_date="2026-11-20", required_places=["上海博物馆"])
    stop = next(stop for stop in itinerary["days"][0]["stops"] if stop["name"] == "上海博物馆")
    assert stop["pricing"]["amount"] == 148
    assert stop["pricing"]["source_url"].startswith("https://whlyj.sh.gov.cn/")


def test_xian_official_tickets_distinguish_free_and_temporary_admission():
    place = {"city": "西安市", "name": "陕西历史博物馆"}
    assert official_admission(place, "2026-11-17")["status"] == "free"
    clock_tower = {**place, "name": "西安钟楼"}
    assert official_admission(clock_tower, "2026-11-14")["amount"] == 15
    assert official_admission(clock_tower, "2026-11-17")["amount"] == 30
    museum = {**place, "name": "西安碑林博物馆"}
    assert official_admission(museum, "2026-11-17")["amount"] == 85
    assert official_admission(museum, "2027-05-12")["status"] == "unknown"
