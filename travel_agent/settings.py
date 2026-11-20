import logging
import os
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def load_settings() -> None:
    # AMap authenticates via a URL parameter. Keep full request URLs out of service logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv(ROOT / ".env", override=False)


def db_path() -> Path:
    path = Path(os.getenv("TRAVEL_DB", ".runtime/travel.sqlite3")).expanduser()
    return path if path.is_absolute() else ROOT / path


def live_available() -> bool:
    from .amap_mcp import amap_mcp_available

    map_ready = amap_mcp_available() or any(os.getenv(name, "").strip() for name in ("AMAP_WEB_KEY", "AMAP_API_KEY", "AMAP_KEY"))
    credential_groups = (
        ("TRAVEL_MODEL_BASE_URL", "CHAT_BASE_URL", "LLM_BASE_URL", "OPENAI_BASE_URL"),
        ("TRAVEL_MODEL_API_KEY", "CHAT_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"),
        ("TRAVEL_MODEL_NAME", "CHAT_MODEL", "LLM_MODEL", "OPENAI_MODEL"),
    )
    return map_ready and all(any(os.getenv(name, "").strip() for name in group) for group in credential_groups)
