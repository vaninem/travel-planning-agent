"""Durable job, stage and event storage shared by the API and workers."""

import json
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGES = ("research", "draft", "validate", "complete")
TERMINAL = {"succeeded", "dead"}


class JobMissing(LookupError):
    pass


class JobConflict(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    job_id: str
    token: str
    request: dict[str, Any]
    mode: str
    strategy: str
    stage: str
    checkpoint: dict[str, Any]
    attempts: int


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def public_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "status": row["status"], "stage": row["stage"],
        "attempts": row["attempts"], "max_attempts": row["max_attempts"],
        "request": json.loads(row["request"]), "mode": row["mode"], "strategy": row["strategy"],
        "created_at": iso_time(row["created_at"]), "updated_at": iso_time(row["updated_at"]),
        "error": row["error"], "itinerary": json.loads(row["itinerary"]) if row["itinerary"] else None,
        "revision": row["revision"],
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, request TEXT NOT NULL,
    mode TEXT NOT NULL, strategy TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
    checkpoint TEXT NOT NULL DEFAULT '{}', itinerary TEXT, revision INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL, available_at REAL NOT NULL,
    lease_token TEXT, lease_until REAL, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, available_at, lease_until);
CREATE INDEX IF NOT EXISTS jobs_history ON jobs(created_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
    type TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL, data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS job_events_cursor ON job_events(job_id, id);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)

    @staticmethod
    def _find(db: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobMissing(job_id)
        return row

    @staticmethod
    def _event(
        db: sqlite3.Connection, row: sqlite3.Row, kind: str, message: str, now: float,
        **extra: Any,
    ) -> None:
        data = {"status": row["status"], "attempts": row["attempts"], **extra}
        db.execute(
            "INSERT INTO job_events(job_id,type,stage,message,data,created_at) VALUES(?,?,?,?,?,?)",
            (row["id"], kind, row["stage"], message, encode(data), now),
        )

    def submit(
        self, request: dict[str, Any], mode: str, strategy: str, key: str | None, *, max_attempts: int = 4,
        legacy_request: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        serialized = encode(request)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
            if old is not None:
                same_request = old["request"] == serialized or (legacy_request is not None and old["request"] == encode(legacy_request))
                if not same_request or (old["mode"], old["strategy"]) != (mode, strategy):
                    raise JobConflict("幂等键已用于另一份旅行需求。")
                return public_job(old), False
            job_id = uuid.uuid4().hex
            now = time.time()
            db.execute(
                """INSERT INTO jobs(id,idempotency_key,request,mode,strategy,status,stage,
                                    max_attempts,available_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,'queued','research',?,?,?,?)""",
                (job_id, key, serialized, mode, strategy, max_attempts, now, now, now),
            )
            row = self._find(db, job_id)
            self._event(db, row, "queued", "旅行规划任务已提交。", now)
            return public_job(row), True

    def get(self, job_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            return public_job(self._find(db, job_id))

    def list_jobs(self, limit: int, *, status: str | None = None) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            if status is not None:
                rows = db.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ?", (status, limit),
                )
            else:
                rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
            return [public_job(row) for row in rows]

    def events(self, job_id: str, after: int, limit: int = 200) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            self._find(db, job_id)
            rows = db.execute(
                "SELECT * FROM job_events WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?", (job_id, after, limit),
            )
            return [{
                "id": row["id"], "type": row["type"], "stage": row["stage"], "message": row["message"],
                "created_at": iso_time(row["created_at"]), "data": json.loads(row["data"]),
            } for row in rows]

    def claim(self, ttl: float, preferred_job: str | None = None) -> Lease | None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            expired = db.execute(
                "SELECT id FROM jobs WHERE status = 'running' AND lease_until <= ? AND attempts >= max_attempts",
                (now,),
            ).fetchall()
            for item in expired:
                self._fail_locked(db, item["id"], "任务租约过期且重试次数已耗尽。", "lease_expired", now, None)
            selected = db.execute(
                """SELECT * FROM jobs
                   WHERE attempts < max_attempts AND (
                     (status IN ('queued','retrying') AND available_at <= ?) OR
                     (status = 'running' AND lease_until <= ?)
                   ) ORDER BY (status = 'queued'), (id IS NOT ?), available_at, created_at LIMIT 1""",
                (now, now, preferred_job),
            ).fetchone()
            if selected is None:
                return None
            token = secrets.token_hex(24)
            db.execute(
                """UPDATE jobs SET status='running', attempts=attempts+1, lease_token=?, lease_until=?,
                                   updated_at=?, error=NULL WHERE id=?""",
                (token, now + ttl, now, selected["id"]),
            )
            row = self._find(db, selected["id"])
            resumed = row["attempts"] > 1
            message = "已接手任务，从上一个检查点继续。" if resumed else "Worker 已领取任务。"
            self._event(db, row, "claimed", message, now, resumed=resumed, expired_lease=selected["status"] == "running")
            return Lease(
                row["id"], token, json.loads(row["request"]), row["mode"], row["strategy"],
                row["stage"], json.loads(row["checkpoint"]), row["attempts"],
            )

    def renew(self, lease: Lease, ttl: float) -> bool:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            # After a host resumes, only the current token can win the race with a new claim.
            result = db.execute(
                "UPDATE jobs SET lease_until=? WHERE id=? AND status='running' AND lease_token=?",
                (now + ttl, lease.job_id, lease.token),
            )
            return result.rowcount == 1

    def _owned(self, db: sqlite3.Connection, lease: Lease, now: float) -> sqlite3.Row:
        row = self._find(db, lease.job_id)
        if row["status"] != "running" or row["lease_token"] != lease.token or row["lease_until"] <= now:
            raise LeaseLost(lease.job_id)
        return row

    def complete_stage(self, lease: Lease, stage: str, checkpoint: dict[str, Any]) -> str:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = self._owned(db, lease, now)
            if row["stage"] != stage or stage not in STAGES[:-1]:
                raise LeaseLost(lease.job_id)
            next_stage = STAGES[STAGES.index(stage) + 1]
            if next_stage == "complete":
                itinerary = checkpoint.get("itinerary")
                if not isinstance(itinerary, dict):
                    raise ValueError("Validation must produce an itinerary")
                db.execute(
                    """UPDATE jobs SET stage=?, checkpoint=?, itinerary=?, revision=1, status='succeeded',
                                       lease_token=NULL, lease_until=NULL, updated_at=? WHERE id=?""",
                    (next_stage, encode(checkpoint), encode(itinerary), now, lease.job_id),
                )
                row = self._find(db, lease.job_id)
                self._event(db, row, "succeeded", "行程校验完成，可以查看每日安排。", now, completed_stage=stage)
                return next_stage
            db.execute(
                "UPDATE jobs SET stage=?, checkpoint=?, updated_at=? WHERE id=?",
                (next_stage, encode(checkpoint), now, lease.job_id),
            )
            row = self._find(db, lease.job_id)
            message = {"research": "已收集景点和旅行资料。", "draft": "已生成行程草案，正在检查安排。"}[stage]
            self._event(db, row, "stage_completed", message, now, completed_stage=stage)
            return next_stage

    def fail(self, lease: Lease, *, error: str, code: str, retry_after: float | None) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = self._owned(db, lease, now)
            available = now + retry_after if retry_after is not None and row["attempts"] < row["max_attempts"] else None
            self._fail_locked(db, lease.job_id, error, code, now, available)

    def _fail_locked(
        self, db: sqlite3.Connection, job_id: str, error: str, code: str, now: float, available: float | None,
    ) -> None:
        status = "retrying" if available is not None else "dead"
        db.execute(
            """UPDATE jobs SET status=?, error=?, available_at=?, lease_token=NULL, lease_until=NULL, updated_at=?
               WHERE id=?""",
            (status, error, available if available is not None else now, now, job_id),
        )
        row = self._find(db, job_id)
        kind, message = ("retry_scheduled", "发生临时错误，任务将自动重试。") if available else ("dead", "行程未能完成。")
        self._event(db, row, kind, message, now, error=error, code=code, retry_at=iso_time(available) if available else None)

    def retry_dead(self, job_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = self._find(db, job_id)
            if row["status"] != "dead":
                raise JobConflict("只有已失败的任务可以手动重试。")
            db.execute(
                """UPDATE jobs SET status='retrying', error=NULL, max_attempts=attempts+4,
                                   available_at=?, updated_at=? WHERE id=?""",
                (now, now, job_id),
            )
            row = self._find(db, job_id)
            self._event(db, row, "retried", "任务已重新进入队列。", now)
            return public_job(row)

    def replace_itinerary(
        self, job_id: str, revision: int, itinerary: dict[str, Any], *, day_index: int, poi_ids: list[str],
    ) -> dict[str, Any]:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = self._find(db, job_id)
            if row["status"] != "succeeded" or row["revision"] != revision:
                raise JobConflict("行程版本已变化，请刷新后重试。")
            db.execute(
                "UPDATE jobs SET itinerary=?, revision=revision+1, updated_at=? WHERE id=?",
                (encode(itinerary), now, job_id),
            )
            row = self._find(db, job_id)
            self._event(
                db, row, "itinerary_updated", "已保存景点调整。", now,
                revision=row["revision"], day_index=day_index, poi_ids=poi_ids,
            )
            return public_job(row)
