"""Score published plans against recorded evidence and observable constraints."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, time, timedelta
from math import ceil, isclose, isfinite


def _coordinates(point: dict) -> tuple[float, float] | None:
    try:
        lng, lat = float(point["lng"]), float(point["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lng, lat) if -180 <= lng <= 180 and -90 <= lat <= 90 else None


def _grounded(point: dict, evidence: dict[str, set[tuple[float, float]]]) -> bool:
    coordinates = _coordinates(point)
    if coordinates is None:
        return False
    return any(
        isclose(coordinates[0], lng, rel_tol=0, abs_tol=1e-6)
        and isclose(coordinates[1], lat, rel_tol=0, abs_tol=1e-6)
        for lng, lat in evidence.get(point.get("poi_id", ""), ())
    )


def _providers(operations: Iterable[dict]) -> tuple[dict, dict, dict]:
    point_sets: dict[str, dict[str, set[tuple[float, float]]]] = {"search_poi": {}, "search_hotels": {}}
    weather: dict[str, str] = {}
    for operation in operations:
        op = operation["operation"]
        result = operation.get("result")
        if not isinstance(result, list):
            continue
        if op == "get_weather":
            for entry in result:
                if entry.get("date") and entry.get("summary"):
                    weather[entry["date"]] = entry["summary"]
        if op in point_sets:
            for point in result:
                coordinates = _coordinates(point)
                if point.get("poi_id") and coordinates is not None:
                    point_sets[op].setdefault(point["poi_id"], set()).add(coordinates)
    return point_sets["search_poi"], point_sets["search_hotels"], weather


def _schedule_ok(stops: list[dict]) -> bool:
    previous_end = time.min
    for stop in stops:
        try:
            start = time.fromisoformat(stop["start_time"])
            end = time.fromisoformat(stop["end_time"])
        except (KeyError, TypeError, ValueError):
            return False
        if start.tzinfo is not None or end.tzinfo is not None or not previous_end <= start < end:
            return False
        previous_end = end
    return True


def _budget_ok(limit: float, summary: object) -> bool:
    if not isinstance(summary, dict) or summary.get("limit") != limit or summary.get("currency") != "CNY":
        return False
    items = summary.get("items")
    if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
        return False
    known = [item.get("known_amount", item.get("amount") or 0) for item in items]
    if any(type(amount) not in (float, int) or not isfinite(amount) or amount < 0 for amount in known):
        return False
    if any(item.get("amount") is not None and item["amount"] != amount for item, amount in zip(items, known, strict=True)):
        return False
    subtotal = round(sum(known), 2)
    complete = all(item.get("amount") is not None for item in items)
    estimated = summary.get("estimated_total")
    if summary.get("estimated_subtotal") != subtotal or estimated != (subtotal if complete else None):
        return False
    status = "over" if round(limit - subtotal, 2) < 0 else ("within" if complete else "partial")
    return summary.get("status") == status


def assess(request: dict, itinerary: dict, operations: Iterable[dict]) -> dict:
    attractions, hotels, weather = _providers(operations)
    days = itinerary.get("days", [])
    stops = [stop for day in days for stop in day.get("stops", [])]
    hotel = itinerary.get("hotel")
    start = date.fromisoformat(request["start_date"])
    expected_dates = [(start + timedelta(days=offset)).isoformat() for offset in range(request["days"])]
    required = request.get("required_places", [])
    rain_days = [
        day for day in days if request.get("avoid_outdoors_in_rain", True)
        and "雨" in weather.get(day.get("date"), "")
    ]
    rain_stops = [
        stop for day in rain_days for stop in day.get("stops", [])
        if type(stop.get("indoor")) is bool
    ]
    city = request["city"].removesuffix("市")
    visit_names = {stop.get("name") for stop in stops}
    checks = {
        "city": itinerary.get("city", "").removesuffix("市") == city,
        "dates": [day.get("date") for day in days] == expected_dates,
        "nonempty_days": bool(days) and all(day.get("stops") for day in days),
        "schedule": all(_schedule_ok(day.get("stops", [])) for day in days),
        "unique_pois": len({stop.get("poi_id") for stop in stops}) == len(stops),
        "required_places": all(name in visit_names for name in required),
        "respects_no_hotel": request.get("hotel", True) or hotel is None,
    }
    if (limit := request.get("budget")) is not None:
        checks["budget_summary"] = _budget_ok(limit, itinerary.get("budget_summary"))
    grounded = sum(_grounded(stop, attractions) for stop in stops)
    hotel_grounded = _grounded(hotel, hotels) if hotel is not None else None
    return {
        "checks": checks,
        "constraints_pass": all(checks.values()),
        "stops": len(stops),
        "grounded_stops": grounded,
        "grounded_plan": bool(stops) and grounded == len(stops) and hotel_grounded is not False,
        "hotel_returned": hotel is not None,
        "hotel_grounded": hotel_grounded,
        "rain_evaluable_stops": len(rain_stops),
        "rain_indoor_stops": sum(stop["indoor"] for stop in rain_stops),
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    return round(sorted(values)[ceil(len(values) * percentile) - 1], 2) if values else None


def summarize(runs: list[dict]) -> dict:
    scores = [run["quality"] for run in runs if run["quality"] is not None]
    latencies = [run["latency_ms"] for run in runs]
    totals = {
        "executions": len(runs),
        "succeeded": len(scores),
        "constraints_passed": sum(score["constraints_pass"] for score in scores),
        "fully_grounded_plans": sum(score["grounded_plan"] for score in scores),
        "grounded_stops": sum(score["grounded_stops"] for score in scores),
        "stops": sum(score["stops"] for score in scores),
        "rain_indoor_stops": sum(score["rain_indoor_stops"] for score in scores),
        "rain_evaluable_stops": sum(score["rain_evaluable_stops"] for score in scores),
        "injected_faults": sum(run["injected_faults"] for run in runs),
        "recovered_injected_faults": sum(bool(run["injected_faults"]) and run["outcome"] == "succeeded" for run in runs),
        "stage_retries": sum(run["stage_retries"] for run in runs),
        "replay_matches": sum(run["replay_matches"] for run in runs),
        "live_model_invocations": sum(run["live_model_invocations"] for run in runs),
        "live_model_responses": sum(run["live_model_responses"] for run in runs),
        "fixture_model_invocations": sum(run["fixture_model_invocations"] for run in runs),
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
    }
    return totals
