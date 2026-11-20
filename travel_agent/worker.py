"""Independent process that resumes jobs from durable planning checkpoints."""

import argparse
import logging
import random
import signal
import sqlite3
from threading import Event, Thread

from .domain import PlanningError, ProviderError, TripRequest
from .guides import prepare_guides
from .planner import advance
from .queue import QueueSignal, SQLiteSignal, queue_signal
from .settings import db_path, load_settings
from .store import Lease, LeaseLost, Store


logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self, store: Store, *, ttl: float = 30, poll: float = 0.5, queue: QueueSignal | None = None,
        advance_stage=advance, retry_base: float = 1,
    ) -> None:
        if ttl <= 0 or poll <= 0 or retry_base < 0:
            raise ValueError("Lease and poll intervals must be positive and retry base nonnegative")
        self.store = store
        self.ttl = ttl
        self.poll = poll
        self.queue = queue if queue is not None else SQLiteSignal()
        self.advance_stage = advance_stage
        self.retry_base = retry_base

    def _heartbeat(self, lease: Lease, stop: Event, lost: Event) -> None:
        while not stop.wait(self.ttl / 3):
            try:
                if not self.store.renew(lease, self.ttl):
                    lost.set()
                    return
            except sqlite3.Error:
                logger.warning("Lease renewal temporarily failed for %s", lease.job_id)

    def run_one(self, preferred_job: str | None = None) -> bool:
        lease = self.store.claim(self.ttl, preferred_job)
        if lease is None:
            return False
        logger.info("Claimed %s at %s (attempt %s)", lease.job_id, lease.stage, lease.attempts)
        stop, lost = Event(), Event()
        heartbeat = Thread(target=self._heartbeat, args=(lease, stop, lost), daemon=True)
        heartbeat.start()
        try:
            request = TripRequest.model_validate(lease.request)
            stage, checkpoint = lease.stage, lease.checkpoint
            while stage != "complete":
                if lost.is_set():
                    raise LeaseLost(lease.job_id)
                checkpoint = self.advance_stage(stage, request, checkpoint, mode=lease.mode, strategy=lease.strategy)
                if not self.store.renew(lease, self.ttl):
                    raise LeaseLost(lease.job_id)
                stage = self.store.complete_stage(lease, stage, checkpoint)
        except LeaseLost:
            logger.info("Worker no longer owns %s", lease.job_id)
        except (ProviderError, PlanningError) as error:
            delay = min(60, self.retry_base * 2 ** min(lease.attempts - 1, 6))
            delay *= random.uniform(0.8, 1.2)
            self._fail(lease, str(error), error.code, delay if error.retryable else None)
        except sqlite3.Error:
            logger.warning("Temporary database write failure for %s; another worker can recover after lease expiry.", lease.job_id)
        except Exception as error:
            logger.error("Job %s failed with %s", lease.job_id, type(error).__name__)
            self._fail(lease, "规划过程中发生内部错误。", "internal_error", None)
        finally:
            stop.set()
            heartbeat.join(timeout=self.ttl)
        return True

    def _fail(self, lease: Lease, message: str, code: str, retry_after: float | None) -> None:
        try:
            if self.store.renew(lease, self.ttl):
                self.store.fail(lease, error=message[:300], code=code, retry_after=retry_after)
        except LeaseLost:
            logger.info("Worker no longer owns %s", lease.job_id)
        except sqlite3.Error:
            logger.warning("Failed to save error for %s; the lease will expire.", lease.job_id)

    def run(self, stop: Event) -> None:
        preferred = None
        while not stop.is_set():
            try:
                took_job = self.run_one(preferred)
            except sqlite3.OperationalError:
                logger.warning("Job storage is busy. Retrying the next poll.")
                took_job = False
            preferred = None if took_job else self.queue.wait(stop, self.poll)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one immediately available job.")
    parser.add_argument("--lease-seconds", type=float, default=30)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_settings()
    store = Store(db_path())
    store.initialize()
    try:
        prepare_guides()
    except (ProviderError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("攻略准备失败（%s），继续处理规划任务", type(exc).__name__)
    worker = Worker(store, ttl=args.lease_seconds, poll=args.poll_seconds, queue=queue_signal())
    if args.once:
        worker.run_one()
        return
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker.run(stop)


if __name__ == "__main__":
    main()
