"""Apply verified POI edits to a saved itinerary."""

from copy import deepcopy

from .domain import ProviderError, TripRequest
from .pricing import official_admission
from .store import JobConflict


def _minutes(time: str) -> int:
    hours, minutes = time.split(":")
    return int(hours) * 60 + int(minutes)


def _update_legs(request: TripRequest, mode: str, day: dict, known: dict) -> list[str]:
    from .planner import estimate_leg

    day["legs"] = []
    warnings = []
    for origin, destination in zip(day["stops"], day["stops"][1:]):
        leg = known.get((origin["poi_id"], destination["poi_id"]))
        try:
            if leg is None or leg.get("duration_minutes") is None:
                leg = estimate_leg(request, origin, destination, mode=mode)
        except ProviderError:
            leg = None
        description = f"{origin['name']} → {destination['name']}"
        duration = leg.get("duration_minutes") if leg is not None else None
        if duration is None:
            warnings.append(f"{description} 暂无通勤估算。")
            continue
        day["legs"].append(leg)
        available = _minutes(destination["start_time"]) - _minutes(origin["end_time"])
        if duration > available:
            warnings.append(f"{description} 预计需 {duration} 分钟，当前预留 {available} 分钟。")
    return warnings


def change_day(job: dict, day_index: int, poi_ids: list[str]) -> dict:
    from .budget import build_budget_summary

    itinerary = deepcopy(job["itinerary"])
    days = itinerary["days"]
    if day_index >= len(days):
        raise ValueError("找不到需要调整的日期。")
    day = days[day_index]
    slots = day["stops"]
    if len(poi_ids) != len(slots):
        raise ValueError("请选择与原行程相同数量的景点。")
    if len(set(poi_ids)) != len(poi_ids):
        raise ValueError("同一天不能重复安排同一个景点。")
    other_ids = {stop["poi_id"] for i, other in enumerate(days) if i != day_index for stop in other["stops"]}
    newly_duplicated = (set(poi_ids) - {stop["poi_id"] for stop in slots}) & other_ids
    if newly_duplicated:
        raise ValueError("新选的景点已经安排在另一天。")

    allowed = {item["poi_id"]: item for item in itinerary.get("candidates", [])}
    allowed.update({stop["poi_id"]: stop for stop in slots})
    if not set(poi_ids).issubset(allowed):
        raise ValueError("请选择已经核验过的候选景点。")
    if poi_ids == [stop["poi_id"] for stop in slots]:
        return itinerary

    request = TripRequest.model_validate(job["request"])
    day["stops"] = [
        {**allowed[poi_id], "start_time": slot["start_time"], "end_time": slot["end_time"]}
        for poi_id, slot in zip(poi_ids, slots, strict=True)
    ]
    for stop in day["stops"]:
        pricing = stop.get("pricing") or {}
        if pricing.get("visit_date") not in (None, day["date"]):
            stop["pricing"] = {"status": "unknown"}
        if pricing.get("visit_date") != day["date"] and (published := official_admission(stop, day["date"])):
            stop["pricing"] = published
    if job["mode"] == "live":
        from .planner import quote_edited_day

        quote_edited_day(request, day)
    known = {(leg["from_poi_id"], leg["to_poi_id"]): leg for leg in day.get("legs", [])}
    day["edited"] = True
    previous = set(day.get("edit_warnings", []))
    messages = [f"{day['date']} 调整后：{notice}" for notice in _update_legs(request, job["mode"], day, known)]
    day["edit_warnings"] = messages
    itinerary["warnings"] = list(dict.fromkeys(
        [warning for warning in itinerary.get("warnings", []) if warning not in previous] + messages,
    ))
    if request.budget is not None:
        budget = build_budget_summary(request, days, itinerary.get("hotel"), mode=job["mode"])
        if budget["status"] == "over":
            previous = build_budget_summary(request, job["itinerary"]["days"], itinerary.get("hotel"), mode=job["mode"])
            if budget["estimated_subtotal"] >= previous["estimated_subtotal"]:
                raise ValueError("调整后的费用估算超出预算，请选择其他景点。")
        itinerary["budget_summary"] = budget
    return itinerary


def verify_edit_version(job: dict, revision: int) -> None:
    if job["status"] != "succeeded":
        raise JobConflict("行程生成完成后才能调整。")
    if job["revision"] != revision:
        raise JobConflict("行程版本已变化，请刷新后重试。")
