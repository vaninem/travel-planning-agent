"""Run the API and supervise a separate worker process for local use."""

import argparse
import logging
import subprocess
import sys
from threading import Event, Thread

import uvicorn

from .settings import ROOT, load_settings


logger = logging.getLogger(__name__)


def supervise_worker(stop: Event) -> None:
    while not stop.is_set():
        process = subprocess.Popen([sys.executable, "-m", "travel_agent.worker"], cwd=ROOT)
        try:
            while not stop.wait(0.25) and process.poll() is None:
                pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if not stop.is_set():
            logger.warning("Worker exited with code %s; restarting.", process.returncode)
            stop.wait(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    load_settings()
    stop = Event()
    supervisor = Thread(target=supervise_worker, args=(stop,), daemon=True)
    supervisor.start()
    try:
        uvicorn.run("travel_agent.api:app", host=args.host, port=args.port, timeout_graceful_shutdown=3)
    finally:
        stop.set()
        supervisor.join(timeout=6)


if __name__ == "__main__":
    main()
