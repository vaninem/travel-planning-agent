"""配置基础设施已提供；显式读取 .env，不发起请求，不打印密钥。"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ..storage.env_file import read_env_file


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    revision: str = "v1"
    database_url: str = field(default="", repr=False)
    database_schema: str = "agent_embeddings"
    database_connect_timeout: int = 10
    batch_size: int = 8
    timeout_seconds: float = 30.0
    lease_seconds: int = 120
    max_attempts: int = 3
    expected_dimensions: int | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", self.database_schema):
            raise ValueError("数据库 schema 必须为小写字母、数字、下划线组成的标识符")
        if self.database_connect_timeout <= 0:
            raise ValueError("数据库连接超时必须为正数")
        if self.batch_size <= 0 or self.timeout_seconds <= 0:
            raise ValueError("批大小和超时必须为正数")
        if self.lease_seconds <= 0 or self.max_attempts <= 0:
            raise ValueError("任务租约与最大尝试次数必须为正数")
        if self.expected_dimensions is not None and self.expected_dimensions <= 0:
            raise ValueError("向量维度必须为正数")

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    @property
    def database_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def profile_id(self) -> str:
        """区分服务、模型、人工版本和维度；换密钥不触发重建索引。"""
        identity = [self.base_url.rstrip("/"), self.model, self.revision, self.expected_dimensions]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    @classmethod
    def from_env(
        cls, env_file: Path | None = None, *, environ: Mapping[str, str] | None = None,
    ) -> "EmbeddingConfig":
        """显式加载文件，进程环境变量优先；不修改 os.environ。"""
        path = Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"
        values = {**read_env_file(path), **(os.environ if environ is None else environ)}
        get = lambda key, default="": str(values.get(key, default)).strip()
        dimensions = get("EMBEDDING_EXPECTED_DIMENSIONS")
        return cls(
            base_url=get("EMBEDDING_BASE_URL").rstrip("/"),
            model=get("EMBEDDING_MODEL"), api_key=get("EMBEDDING_API_KEY"),
            revision=get("EMBEDDING_REVISION", "v1"),
            database_url=get("DATABASE_URL"),
            database_schema=get("EMBEDDING_DB_SCHEMA", "agent_embeddings"),
            database_connect_timeout=int(get("DATABASE_CONNECT_TIMEOUT", "10")),
            batch_size=int(get("EMBEDDING_BATCH_SIZE", "8")),
            timeout_seconds=float(get("EMBEDDING_TIMEOUT_SECONDS", "30")),
            lease_seconds=int(get("EMBEDDING_LEASE_SECONDS", "120")),
            max_attempts=int(get("EMBEDDING_MAX_ATTEMPTS", "3")),
            expected_dimensions=int(dimensions) if dimensions else None,
        )
