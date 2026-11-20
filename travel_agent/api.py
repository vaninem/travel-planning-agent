"""FastAPI endpoints for job submission, progress streaming and itinerary edits."""

import asyncio
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .budget import build_budget_summary
from .domain import DEFAULT_MEALS_PER_PERSON_DAY, Mode, ProviderError, Strategy, TripRequest
from .edits import change_day, verify_edit_version
from .forecast import fetch_daily_forecast
from .pricing import lodging_search, official_admission, public_transit_reference
from .providers import PLACES, city_key
from .queue import QueueSignal, queue_signal
from .settings import ROOT, db_path, live_available, load_settings
from .store import TERMINAL, JobConflict, JobMissing, Store, encode


class JobSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: TripRequest
    mode: Mode = "fixture"
    strategy: Strategy = "verified"


class ItineraryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    day_index: int = Field(ge=0)
    poi_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(max_length=12)


IdempotencyHeader = Annotated[str | None, Header(min_length=1, max_length=128)]
JobState = Literal["queued", "running", "retrying", "succeeded", "dead"]


@lru_cache(maxsize=32)
def _forecast(lng: float, lat: float, dates: tuple[str, ...], _window: int) -> list[dict]:
    with httpx.Client(trust_env=False) as client:
        return fetch_daily_forecast(client, lng=lng, lat=lat, dates=list(dates))


def _job_view(job: dict) -> dict:
    itinerary = job.get("itinerary")
    if job["mode"] != "live" or not itinerary:
        return job
    request = TripRequest.model_validate(job["request"])
    for day in itinerary["days"]:
        for stop in day["stops"]:
            price = stop.get("pricing") or {}
            if price.get("status") in (None, "unknown"):
                if published := official_admission(stop, day["date"]):
                    stop["pricing"] = published
    if request.budget is not None:
        itinerary["budget_summary"] = build_budget_summary(request, itinerary["days"], itinerary.get("hotel"), mode="live")
    if request.hotel and request.days > 1:
        itinerary.setdefault("lodging_search", lodging_search(request))
    if request.transport == "transit" and (references := public_transit_reference(request.city)):
        itinerary["transit_fare_reference"] = references
    return job


def create_app(*, database: Path | None = None, queue: QueueSignal | None = None) -> FastAPI:
    load_settings()
    store = Store(database if database is not None else db_path())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(store.initialize)
        app.state.queue = queue if queue is not None else queue_signal()
        yield

    app = FastAPI(title="旅序 · 旅行规划助手", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(JobMissing)
    async def job_not_found(_request, _error):
        return JSONResponse({"detail": "找不到该任务。"}, status_code=404)

    @app.exception_handler(JobConflict)
    async def job_conflict(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/api/config")
    def configuration():
        from .amap_mcp import amap_mcp_available
        from .ctrip import ctrip_available
        from .pricing import booking_available
        from .rollinggo import rollinggo_available

        booking_configured = booking_available()
        return {
            "live_available": live_available(),
            "fixture_cities": list(PLACES),
            "booking_available": booking_configured,
            "hotel_pricing_available": booking_configured or rollinggo_available(),
            "default_meals_per_person_day": DEFAULT_MEALS_PER_PERSON_DAY,
            "amap_mcp_available": amap_mcp_available(),
            "ctrip_available": ctrip_available(),
        }

    @app.post("/api/jobs", status_code=202)
    def submit(submission: JobSubmission, response: Response, idempotency_key: IdempotencyHeader = None):
        if submission.mode == "live" and not live_available():
            raise HTTPException(503, "实时数据需要配置高德 MCP 或 Web 服务密钥，以及模型服务。")
        if submission.mode == "fixture" and city_key(submission.request.city) not in PLACES:
            raise HTTPException(422, "示例数据暂未收录该城市。请选择页面上的示例城市，或配置实时数据后查询。")
        request = submission.request.payload()
        legacy_request = request if "meals_per_person_day" not in request else None
        if legacy_request is not None:
            request = {**request, "meals_per_person_day": DEFAULT_MEALS_PER_PERSON_DAY}
        job, created = store.submit(request, submission.mode, submission.strategy, idempotency_key, legacy_request=legacy_request)
        if created:
            app.state.queue.publish(job["id"])
        else:
            response.status_code = 200
        response.headers["Location"] = f"/api/jobs/{job['id']}"
        return job

    @app.get("/api/jobs")
    def history(limit: Annotated[int, Query(ge=1, le=200)] = 50, status: JobState | None = None):
        return {"jobs": store.list_jobs(limit, status=status)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return _job_view(store.get(job_id))

    @app.get("/api/jobs/{job_id}/forecast")
    def get_forecast(job_id: str):
        job = store.get(job_id)
        itinerary = job.get("itinerary") or {}
        days = itinerary.get("days") or []
        first = next((stop for day in days for stop in day.get("stops", [])), None)
        if job["mode"] != "live" or not first:
            return {"forecasts": []}
        try:
            forecasts = _forecast(round(first["lng"], 3), round(first["lat"], 3),
                                  tuple(day["date"] for day in days), int(time.time()) // 600)
        except ProviderError:
            forecasts = []
        return {"forecasts": forecasts}

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str):
        job = store.retry_dead(job_id)
        app.state.queue.publish(job_id)
        return job

    @app.patch("/api/jobs/{job_id}/itinerary")
    def edit(job_id: str, patch: ItineraryEdit):
        job = _job_view(store.get(job_id))
        verify_edit_version(job, patch.revision)
        try:
            changed = change_day(job, patch.day_index, patch.poi_ids)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if changed == job["itinerary"]:
            return _job_view(job)
        return _job_view(store.replace_itinerary(job_id, patch.revision, changed, day_index=patch.day_index, poi_ids=patch.poi_ids))

    @app.get("/api/jobs/{job_id}/events")
    def events(job_id: str, after: Annotated[int, Query(ge=0, le=2**63-1)] = 0):
        return {"events": store.events(job_id, after, limit=500)}

    @app.get("/api/jobs/{job_id}/stream")
    async def stream(
        job_id: str,
        after: Annotated[int, Query(ge=0, le=2**63-1)] = 0,
        last_event_id: Annotated[int | None, Header(ge=0, le=2**63-1)] = None,
    ):
        cursor = max(after, last_event_id or 0)
        job = await asyncio.to_thread(store.get, job_id)
        if job["status"] in TERMINAL and not await asyncio.to_thread(store.events, job_id, cursor, 1):
            return Response(status_code=204)

        async def generate():
            nonlocal cursor
            yield "retry: 1200\n\n"
            last_frame = time.monotonic()
            while True:
                current = await asyncio.to_thread(store.get, job_id)
                pending = await asyncio.to_thread(store.events, job_id, cursor)
                for event in pending:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {encode(event)}\n\n"
                if pending:
                    last_frame = time.monotonic()
                    continue
                if current["status"] in TERMINAL:
                    return
                if time.monotonic() - last_frame >= 12:
                    yield ": keep-alive\n\n"
                    last_frame = time.monotonic()
                await asyncio.sleep(0.25)

        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    frontend = ROOT / "frontend" / "dist"
    if (frontend / "index.html").exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")

    return app


app = create_app()
