"""Group trip estimates from provider prices and clearly labeled sample assumptions."""

import math

from .domain import TripRequest
from .pricing import lodging_search, sample_admission, sample_lodging


LEGACY_MEALS_PER_PERSON_DAY = 60
DEMO_ROOM_PER_NIGHT = 180


def _amount(value: object) -> float | None:
    if type(value) not in (int, float) or not (0 <= value):
        return None
    try:
        return round(value, 2) if math.isfinite(value) else None
    except OverflowError:
        return None


def ticketed_place(place: dict, *, mode: str) -> dict:
    if mode == "fixture" and not place.get("pricing"):
        return {**place, "pricing": sample_admission(place)}
    return place


def priced_hotel(request: TripRequest, hotel: dict | None, *, mode: str) -> dict | None:
    if hotel and mode == "fixture" and not hotel.get("pricing"):
        return {**hotel, **lodging_search(request), "pricing": sample_lodging(request, DEMO_ROOM_PER_NIGHT)}
    return hotel


def _quote_amount(quote: dict | None, field: str, *, mode: str) -> float | None:
    if not quote:
        return None
    status = quote.get("status")
    if status == "free":
        return 0
    if quote.get("currency", "CNY") != "CNY" or quote.get("price_kind") in {"range", "from"}:
        return None
    if status not in {"quoted", "free"} and not (status == "sample" and mode == "fixture"):
        return None
    return _amount(quote.get(field, 0 if status == "free" else None))


def ticket_cost(request: TripRequest, place: dict, *, mode: str) -> float | None:
    quote = ticketed_place(place, mode=mode).get("pricing") or {}
    if quote.get("status") in (None, "unknown"):
        return 0
    amount = _quote_amount(quote, "amount", mode=mode)
    if quote.get("status") == "quoted" and quote.get("currency") == "CNY" and quote.get("price_kind") == "range":
        amount = _amount(quote.get("maximum"))
    return round(amount * request.travelers, 2) if amount is not None else None


def lodging_cost(request: TripRequest, hotel: dict | None, *, mode: str) -> float | None:
    if request.days == 1 or not request.hotel:
        return 0
    lodging = priced_hotel(request, hotel, mode=mode)
    return _quote_amount(lodging.get("pricing"), "total", mode=mode) if lodging else None


def local_leg_cost(request: TripRequest, distance: float) -> float:
    if request.transport == "walking":
        return 0
    kilometers = distance / 1000
    if request.transport == "driving":
        return (6 + math.ceil(kilometers * 1.2)) * math.ceil(request.travelers / 4)
    return (2 + int(kilometers / 5)) * request.travelers


def _daily_meal_rate(request: TripRequest) -> float:
    return LEGACY_MEALS_PER_PERSON_DAY if request.meals_per_person_day is None else request.meals_per_person_day


def _meals_cost(request: TripRequest) -> float:
    return round(_daily_meal_rate(request) * request.travelers * request.days, 2)


def basic_cost(request: TripRequest, hotel: dict | None, *, mode: str) -> float:
    return _meals_cost(request) + (lodging_cost(request, hotel, mode=mode) or 0)


def _line(key: str, label: str, amounts: list[float | None]) -> dict:
    known = round(sum(amount for amount in amounts if amount is not None), 2)
    missing = amounts.count(None)
    return {"key": key, "label": label, "amount": None if missing else known, "known_amount": known, "unknown_count": missing}


def _foreign_tickets(stops: list[dict], travelers: int) -> list[dict]:
    costs = []
    for stop in stops:
        quote = stop.get("pricing") or {}
        currency = quote.get("currency")
        amount = _amount(quote.get("amount"))
        if quote.get("status") != "quoted" or not currency or currency == "CNY" or amount is None:
            continue
        maximum = _amount(quote.get("maximum"))
        costs.append({"label": stop["name"], "currency": currency, "amount": round(amount * travelers, 2),
                      "maximum": round(maximum * travelers, 2) if maximum is not None else None,
                      "price_kind": quote.get("price_kind")})
    return costs


def build_budget_summary(request: TripRequest, days: list[dict], hotel: dict | None, *, mode: str) -> dict | None:
    if request.budget is None:
        return None

    stops = [stop for day in days for stop in day["stops"]]
    tickets = []
    for day in days:
        previous_group = None
        for stop in day["stops"]:
            pricing = stop.get("pricing") or {}
            group = pricing.get("admission_group") if pricing.get("status") == "quoted" else None
            if not group or group != previous_group:
                tickets.append(ticket_cost(request, stop, mode=mode))
            previous_group = group
    provisional_tickets = sum((ticketed_place(stop, mode=mode).get("pricing") or {}).get("status") in (None, "unknown") for stop in stops)
    foreign_costs = _foreign_tickets(stops, request.travelers)
    transportation = []
    if request.transport != "walking":
        for day in days:
            legs = {(leg.get("from_poi_id"), leg.get("to_poi_id")): leg for leg in day.get("legs", [])}
            for origin, destination in zip(day["stops"], day["stops"][1:]):
                leg = legs.get((origin["poi_id"], destination["poi_id"]), {})
                distance = _amount(leg.get("distance_m"))
                fare = _amount(leg.get("fare_cny")) if leg.get("evidence") in {"amap", "amap_mcp"} and request.transport == "transit" else None
                if mode == "fixture" and distance is not None:
                    transportation.append(local_leg_cost(request, distance))
                else:
                    transportation.append(round(fare * request.travelers, 2) if fare is not None else None)

    items = [
        _line("tickets", "景点门票", tickets),
        _line("transport", "市内交通", transportation),
        _line("meals", "餐饮预留", [_meals_cost(request)]),
    ]
    if request.hotel and request.days > 1:
        items.append(_line("lodging", "住宿", [lodging_cost(request, hotel, mode=mode)]))

    subtotal = round(sum(item["known_amount"] for item in items), 2)
    complete = not any(item["unknown_count"] for item in items)
    difference = round(request.budget - subtotal, 2)
    status = "over" if subtotal > request.budget else ("within" if complete else "partial")
    notes = [
        f"全团人民币估算，餐饮按每人每天 ¥{_daily_meal_rate(request):g} 预留。",
        "市内交通按行程列出的景点之间估算，往返城市及未安排的路线另计。",
    ]
    if mode == "fixture":
        notes.append("交通和未附票价来源的示例金额用于演示规划，实际以预订或现场公示为准。")
    if request.hotel and request.days > 1:
        notes.append("住宿按每间最多两位、旅行天数减一天计算；请按实际房型核对。")
    if not complete:
        notes.append("待核实的费用未计入已知小计。")
    if provisional_tickets:
        notes.append(f"{provisional_tickets} 项待核实门票暂按 ¥0 计入，购票价以来源页面为准。")
    if foreign_costs:
        notes.append("外币票价按原币单列，人民币小计未包含这部分金额。")
    return {
        "currency": "CNY", "limit": request.budget, "estimated_total": subtotal if complete else None,
        "estimated_subtotal": subtotal, "remaining": difference if complete or difference < 0 else None,
        "status": status, "items": items, "notes": notes,
        "provisional_tickets": provisional_tickets,
        **({"foreign_costs": foreign_costs} if foreign_costs else {}),
    }
