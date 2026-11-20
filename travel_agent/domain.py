"""公共请求结构与规划失败类型。"""

from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Transport = Literal["walking", "driving", "transit"]
Strategy = Literal["fixed", "tools", "verified"]
Mode = Literal["fixture", "live"]
DEFAULT_MEALS_PER_PERSON_DAY = 120.0


class TripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    city: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    start_date: date
    days: int = Field(ge=1, le=14)
    travelers: int = Field(default=1, ge=1, le=30)
    interests: list[ShortText] = Field(default_factory=list, max_length=12)
    budget: float | None = Field(default=None, gt=0)
    meals_per_person_day: float | None = Field(default=None, ge=0, le=1_000_000, multiple_of=0.01)
    transport: Transport = "transit"
    hotel: bool = True
    notes: str = Field(default="", max_length=1_000)
    pace: Literal["relaxed", "balanced", "packed"] = "balanced"
    required_places: list[ShortText] = Field(default_factory=list, max_length=12)
    avoid_outdoors_in_rain: bool = True

    @model_validator(mode="after")
    def calendar_can_fit(self) -> "TripRequest":
        try:
            self.start_date + timedelta(days=self.days - 1)
        except OverflowError as exc:
            raise ValueError("旅行日期超出日历范围") from exc
        return self

    def dates(self) -> list[str]:
        return [(self.start_date + timedelta(days=i)).isoformat() for i in range(self.days)]

    def payload(self) -> dict:
        fields = {"meals_per_person_day"} if self.meals_per_person_day is None else None
        return self.model_dump(mode="json", exclude=fields)


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class PlanningError(ProviderError):
    """可以作为任务失败原因保存的业务异常。"""
