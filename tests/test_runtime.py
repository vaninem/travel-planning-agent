import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from travel_agent.api import create_app
from travel_agent.domain import ProviderError, TripRequest
from travel_agent.edits import change_day
from travel_agent.queue import RedisSignal
from travel_agent.store import LeaseLost, Store
from travel_agent.worker import Worker


TRIP = {
    "city": "上海", "start_date": "2026-11-20", "days": 2, "travelers": 2,
    "interests": ["博物馆", "历史"], "transport": "transit", "hotel": True,
}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "travel.db")
    value.initialize()
    return value


def submit(store, key=None, max_attempts=4):
    return store.submit(TRIP, "fixture", "verified", key, max_attempts=max_attempts)[0]


def test_concurrent_idempotency_and_leases(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda _: submit(store, "same-user-action"), range(8)))
        claims = list(pool.map(lambda _: store.claim(10), range(8)))
    assert len({job["id"] for job in jobs}) == 1
    assert len([claim for claim in claims if claim]) == 1
    assert [event["type"] for event in store.events(jobs[0]["id"], 0)] == ["queued", "claimed"]


def test_meal_allowance_defaults_for_new_plans_and_preserves_legacy_retries(store):
    request = {**TRIP, "hotel": False, "budget": 2000}
    previous, _ = store.submit(TripRequest.model_validate(request).payload(), "fixture", "verified", "legacy-meal")
    app = create_app(database=store.path)
    with TestClient(app) as client:
        assert client.get("/api/config").json()["default_meals_per_person_day"] == 120
        legacy = client.post("/api/jobs", json={"request": request}, headers={"Idempotency-Key": "legacy-meal"})
        assert legacy.status_code == 200 and legacy.json()["id"] == previous["id"]
        assert "meals_per_person_day" not in legacy.json()["request"]

        jobs = [previous]
        for rate in (None, 150.25, 0):
            custom = {**request, "meals_per_person_day": rate} if rate is not None else request
            response = client.post("/api/jobs", json={"request": custom})
            assert response.status_code == 202
            assert response.json()["request"]["meals_per_person_day"] == (120 if rate is None else rate)
            jobs.append(response.json())
        assert client.post("/api/jobs", json={"request": {**request, "meals_per_person_day": -1}}).status_code == 422
        for job, total in zip(jobs, (240, 480, 601, 0), strict=True):
            assert Worker(store).run_one(job["id"])
            budget = client.get(f"/api/jobs/{job['id']}").json()["itinerary"]["budget_summary"]
            meals = next(item for item in budget["items"] if item["key"] == "meals")
            assert meals["amount"] == total


def test_expired_worker_cannot_write_after_reclaim(store, monkeypatch):
    now = time.time()
    monkeypatch.setattr("travel_agent.store.time.time", lambda: now)
    job = submit(store)
    old = store.claim(1)
    store.complete_stage(old, "research", {"persisted": True})
    now += 2
    replacement = store.claim(10)
    assert replacement.token != old.token
    assert replacement.stage == "draft" and replacement.checkpoint == {"persisted": True}
    assert not store.renew(old, 10)
    with pytest.raises(LeaseLost):
        store.complete_stage(old, "draft", {"late": True})
    assert store.get(job["id"])["stage"] == "draft"


def test_worker_heartbeats_during_external_call(store):
    started, release = Event(), Event()

    def slow(stage, request, checkpoint, **_):
        if stage == "research":
            started.set()
            assert release.wait(3)
        return {"itinerary": {"test": True}}

    job = submit(store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(Worker(store, ttl=0.4, advance_stage=slow).run_one)
        assert started.wait(3)
        try:
            time.sleep(0.75)
            assert store.claim(5) is None
        finally:
            release.set()
        assert worker.result(timeout=4)
    assert store.get(job["id"])["status"] == "succeeded"


def test_worker_resumes_after_host_sleep_if_lease_was_not_reclaimed(store, monkeypatch):
    now = time.time()
    monkeypatch.setattr("travel_agent.store.time.time", lambda: now)

    def advance(stage, request, checkpoint, **_):
        nonlocal now
        now += 3600
        return {"itinerary": {"test": True}}

    job = submit(store, max_attempts=1)
    assert Worker(store, advance_stage=advance).run_one()
    finished = store.get(job["id"])
    assert (finished["status"], finished["attempts"]) == ("succeeded", 1)


def test_retry_exhaustion_dead_letter_and_manual_retry(store):
    def timeout(*args, **kwargs):
        raise ProviderError("timeout", "工具超时", retryable=True)

    job = submit(store, max_attempts=2)
    failing = Worker(store, advance_stage=timeout, retry_base=0)
    assert failing.run_one()
    assert store.get(job["id"])["status"] == "retrying"
    assert failing.run_one()
    assert store.list_jobs(3, status="dead")[0]["id"] == job["id"]
    assert store.retry_dead(job["id"])["status"] == "retrying"
    assert Worker(store).run_one()
    state = store.get(job["id"])
    assert (state["attempts"], state["status"]) == (3, "succeeded")
    assert [event["type"] for event in store.events(job["id"], 0)].count("succeeded") == 1


def wait_for(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    pytest.fail("condition did not become true before timeout")


def test_sigkill_after_checkpoint_and_another_worker_finishes(store, tmp_path):
    marker = tmp_path / "in-draft"
    child_code = """
import os, sys, time
from pathlib import Path
from travel_agent.store import Store
from travel_agent.planner import advance
from travel_agent.worker import Worker
def pause(stage, request, checkpoint, **kw):
    if stage == 'draft':
        Path(sys.argv[2]).touch()
        time.sleep(60)
    return advance(stage, request, checkpoint, **kw)
Worker(Store(Path(sys.argv[1])), ttl=0.6, advance_stage=pause).run_one()
"""
    job = submit(store)
    env = {**os.environ, "TRAVEL_DB": str(store.path), "TRAVEL_QUEUE": "sqlite"}
    child = subprocess.Popen([sys.executable, "-c", child_code, str(store.path), str(marker)], cwd=ROOT, env=env)
    try:
        wait_for(marker.exists)
        assert store.get(job["id"])["stage"] == "draft"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
    assert child.returncode == -9
    time.sleep(0.7)
    subprocess.run([sys.executable, "-m", "travel_agent.worker", "--once"], cwd=ROOT, env=env, check=True, timeout=15)
    state = store.get(job["id"])
    assert (state["status"], state["attempts"], state["revision"]) == ("succeeded", 2, 1)
    progress = store.events(job["id"], 0)
    assert [event["data"].get("completed_stage") for event in progress].count("research") == 1
    assert [event["type"] for event in progress].count("succeeded") == 1
    assert [event for event in progress if event["type"] == "claimed"][-1]["data"]["expired_lease"] is True


def test_api_submission_resume_and_optimistic_edit(store):
    with TestClient(create_app(database=store.path)) as client:
        headers = {"Idempotency-Key": "trip-test"}
        body = {"request": TRIP, "strategy": "verified"}
        initial = client.post("/api/jobs", json=body, headers=headers)
        assert initial.status_code == 202
        job_id = initial.json()["id"]
        assert client.post("/api/jobs", json=body, headers=headers).json()["id"] == job_id
        assert client.post("/api/jobs", json={**body, "strategy": "fixed"}, headers=headers).status_code == 409
        assert client.get("/api/jobs/unknown/stream").status_code == 404
        assert Worker(store).run_one()

        path = f"/api/jobs/{job_id}"
        events = client.get(f"{path}/events").json()["events"]
        cursor = events[1]["id"]
        response = client.get(f"{path}/stream", headers={"Last-Event-ID": str(cursor)})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = [int(line.removeprefix("id: ")) for line in response.text.splitlines() if line.startswith("id: ")]
        assert frames == [event["id"] for event in events[2:]]
        assert client.get(f"{path}/stream?after={frames[-1]}").status_code == 204

        job = client.get(path).json()
        stops = job["itinerary"]["days"][0]["stops"]
        assert len(stops) >= 2
        order = [stop["poi_id"] for stop in reversed(stops)]
        patch = {"revision": job["revision"], "day_index": 0, "poi_ids": order}
        invalid = {**patch, "poi_ids": ["unknown", *order[1:]]}
        assert client.patch(f"{path}/itinerary", json=invalid).status_code == 422
        edited = client.patch(f"{path}/itinerary", json=patch)
        assert edited.status_code == 200, edited.text
        assert edited.json()["revision"] == 2
        assert [stop["poi_id"] for stop in edited.json()["itinerary"]["days"][0]["stops"]] == order
        assert client.patch(f"{path}/itinerary", json=patch).status_code == 409
        assert client.get(f"{path}/events?after={frames[-1]}").json()["events"][0]["type"] == "itinerary_updated"


def test_demo_city_catalog_and_submitting_a_nondefault_city(store):
    with TestClient(create_app(database=store.path)) as client:
        cities = client.get("/api/config").json()["fixture_cities"]
        assert len(cities) == len(set(cities)) == 10
        assert {"上海", "北京", "杭州", "成都"}.issubset(cities)
        unknown = client.post("/api/jobs", json={"request": {**TRIP, "city": "昆明"}})
        assert unknown.status_code == 422
        assert client.get("/api/jobs").json() == {"jobs": []}

        submission = client.post("/api/jobs", json={"request": {**TRIP, "city": "北京市", "budget": 1000}})
        assert submission.status_code == 202
        assert Worker(store).run_one()
        job = client.get(f"/api/jobs/{submission.json()['id']}").json()
    assert job["status"] == "succeeded"
    itinerary = job["itinerary"]
    assert len(itinerary["days"]) == 2 and itinerary["city"].removesuffix("市") == "北京"
    assert all(stop["city"] == "北京" for day in itinerary["days"] for stop in day["stops"])
    assert itinerary["budget_summary"]["limit"] == 1000


def test_edit_with_missing_route_duration_can_be_saved(store, monkeypatch):
    job_id = submit(store)["id"]
    assert Worker(store).run_one()
    job = store.get(job_id)
    day = job["itinerary"]["days"][0]
    ids = [stop["poi_id"] for stop in reversed(day["stops"])]
    day["legs"] = [{"from_poi_id": ids[0], "to_poi_id": ids[1], "duration_minutes": None}]
    monkeypatch.setattr("travel_agent.planner.estimate_leg", lambda *args, **kwargs: None)
    changed = change_day(job, 0, ids)
    assert [stop["poi_id"] for stop in changed["days"][0]["stops"]] == ids
    assert changed["days"][0]["legs"] == []
    assert any("暂无通勤估算" in message for message in changed["warnings"])


@pytest.mark.skipif(not os.getenv("TRAVEL_TEST_REDIS_URL"), reason="set TRAVEL_TEST_REDIS_URL to test Redis")
def test_real_redis_delivery_and_sqlite_reconciliation(store):
    signal = RedisSignal(os.environ["TRAVEL_TEST_REDIS_URL"], f"travel-test:{time.time_ns()}")
    try:
        with TestClient(create_app(database=store.path, queue=signal)) as client:
            result = client.post("/api/jobs", json={"request": TRIP})
        assert result.status_code == 202
        job_id = result.json()["id"]
        assert signal.wait(Event(), 1) == job_id
        assert Worker(store, queue=signal).run_one(job_id)
        assert store.get(job_id)["status"] == "succeeded"
        orphan = submit(store)
        assert Worker(store, queue=signal).run_one()
        assert store.get(orphan["id"])["status"] == "succeeded"
    finally:
        signal.client.delete(signal.key)
