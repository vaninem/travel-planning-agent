"""Versioned, deterministic benchmark inputs before provider data is applied."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


CORPUS_VERSION = "travel-cn-1"
CORPUS_START = date(2030, 10, 1)

CITIES = (
    "上海", "北京", "杭州", "成都", "西安", "广州", "深圳", "南京", "苏州", "重庆",
)

SCENARIOS: dict[str, dict[str, Any]] = {
    "city_break": {"days": 2, "interests": ["城市文化", "地标"], "notes": "首次来访，想看有代表性的景点"},
    "rain_ready": {"days": 2, "interests": ["博物馆", "室内"], "avoid_outdoors_in_rain": True},
    "slow_pace": {"days": 3, "interests": ["散步", "历史"], "pace": "relaxed"},
    "family": {"days": 2, "travelers": 4, "interests": ["亲子", "公园"], "pace": "relaxed"},
    "day_trip": {"days": 1, "interests": ["经典景点"], "hotel": False},
    "walking": {"days": 2, "transport": "walking", "interests": ["步行", "摄影"], "hotel": False},
    "transit": {"days": 2, "transport": "transit", "travelers": 2, "interests": ["博物馆", "建筑"]},
    "road_trip": {"days": 2, "transport": "driving", "travelers": 3, "interests": ["自然", "地标"]},
    "budget": {"days": 2, "budget": 800, "interests": ["公园", "文化"], "notes": "预算优先，价格不明请提示"},
    "must_see": {"days": 2, "interests": ["历史", "地标"], "notes": "请优先安排指定地点"},
}

# A single exact POI name per city makes the requirement independently testable.
REQUIRED_PLACES = {
    "上海": "上海博物馆",
    "北京": "故宫博物院",
    "杭州": "灵隐寺",
    "成都": "成都博物馆",
    "西安": "陕西历史博物馆",
    "广州": "广东省博物馆",
    "深圳": "深圳博物馆",
    "南京": "南京博物院",
    "苏州": "苏州博物馆",
    "重庆": "重庆中国三峡博物馆",
}

FIXTURE_RAIN_OFFSETS = (14, 1, 4, 4, 3, 1, 0, 1, 1, 1)


@dataclass(frozen=True)
class TravelCase:
    case_id: str
    scenario: str
    request: dict[str, Any]


def build_cases(start: date = CORPUS_START) -> list[TravelCase]:
    """Cross ten scenarios with ten cities in stable order."""
    cases = []
    for scenario, options in SCENARIOS.items():
        for city_index, city in enumerate(CITIES):
            date_offset = city_index % 3
            if scenario == "rain_ready" and start == CORPUS_START:
                date_offset = FIXTURE_RAIN_OFFSETS[city_index]
            request = {
                "city": city,
                "start_date": (start + timedelta(days=date_offset)).isoformat(),
                **deepcopy(options),
            }
            if scenario == "must_see":
                request["required_places"] = [REQUIRED_PLACES[city]]
            cases.append(TravelCase(f"{scenario}-{city_index + 1:02}", scenario, request))
    return cases
