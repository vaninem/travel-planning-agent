import logging
import os
from threading import Event
from typing import Protocol


logger = logging.getLogger(__name__)


class QueueSignal(Protocol):
    def publish(self, job_id: str) -> None: ...

    def wait(self, stop: Event, interval: float) -> str | None: ...


class SQLiteSignal:
    def publish(self, job_id: str) -> None:
        pass

    def wait(self, stop: Event, interval: float) -> None:
        stop.wait(interval)


class RedisSignal:
    """Ready-job notifications backed by the durable SQLite job ledger."""

    def __init__(self, url: str, key: str) -> None:
        try:
            from redis import Redis
            from redis.exceptions import RedisError
        except ImportError as exc:
            raise RuntimeError("Install travel-planning-agent[redis] to enable Redis") from exc
        self.client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=1, socket_timeout=4)
        self.redis_error = RedisError
        self.key = key

    def publish(self, job_id: str) -> None:
        try:
            with self.client.pipeline() as pipe:
                pipe.lpush(self.key, job_id).ltrim(self.key, 0, 9999).execute()
        except self.redis_error:
            logger.warning("Redis notification failed. The job remains available in SQLite.")

    def wait(self, stop: Event, interval: float) -> str | None:
        try:
            message = self.client.brpop(self.key, timeout=max(0.1, min(interval, 2)))
            return message[1] if message else None
        except self.redis_error:
            stop.wait(interval)
            return None


def queue_signal() -> QueueSignal:
    backend = os.getenv("TRAVEL_QUEUE", "sqlite").lower()
    if backend == "sqlite":
        return SQLiteSignal()
    if backend == "redis":
        return RedisSignal(
            os.getenv("TRAVEL_REDIS_URL", "redis://127.0.0.1:6379/0"),
            os.getenv("TRAVEL_REDIS_QUEUE", "travel-planning-agent:ready"),
        )
    raise ValueError("TRAVEL_QUEUE must be sqlite or redis")
