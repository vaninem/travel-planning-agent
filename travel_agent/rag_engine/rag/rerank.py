"""华东师范大学文本重排适配器。"""

import math
import os
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from ..storage.env_file import read_env_file
from ..types import SearchHit
from .recall import index_text
from .types import TEXT_MODEL_CHAR_LIMIT


ECNU_BASE_URL = "https://chat.ecnu.edu.cn/open/api/v1"
DOCUMENT_CHAR_LIMIT = TEXT_MODEL_CHAR_LIMIT


class RerankRequestError(RuntimeError):
    """已脱敏的重排接口错误。"""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class RerankConfig:
    base_url: str = ECNU_BASE_URL
    model: str = "ecnu-rerank"
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("RERANK_BASE_URL 必须是 HTTP API 基础地址")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("RERANK_MODEL 必须为非空字符串")
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("重排超时必须是有限正数")

    @classmethod
    def from_env(
        cls, env_file: Path | None = None, *, environ: Mapping[str, str] | None = None,
    ) -> "RerankConfig":
        """显式读取 RERANK_*，进程环境优先。"""
        path = Path(env_file) if env_file is not None else Path(__file__).resolve().parents[2] / ".env"
        values = {**read_env_file(path), **(os.environ if environ is None else environ)}
        return cls(
            base_url=values.get("RERANK_BASE_URL", ECNU_BASE_URL).strip().rstrip("/"),
            model=values.get("RERANK_MODEL", "ecnu-rerank").strip(),
            api_key=values.get("RERANK_API_KEY", "").strip(),
            timeout_seconds=float(values.get("RERANK_TIMEOUT_SECONDS", "15")),
        )


class ECNUReranker:
    def __init__(self, config: RerankConfig, transport: Any = None):
        self.config = config
        self.transport = transport
        self.endpoint = config.base_url.rstrip("/") + "/rerank"

    def rerank(self, query: str, hits: list[SearchHit], limit: int) -> list[SearchHit]:
        """请求文本重排，用返回下标映射回本地原始命中。"""
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or limit <= 0:
            raise ValueError("重排需要非空查询和正整数 limit")
        if not hits:
            return []
        if not self.config.api_key:
            raise RerankRequestError("请显式配置 RERANK_API_KEY")
        import httpx

        top_n = min(limit, len(hits))
        body = {
            "model": self.config.model, "query": query,
            "documents": [index_text(hit.chunk)[:DOCUMENT_CHAR_LIMIT] for hit in hits],
            "top_n": top_n, "return_documents": False,
        }
        context = nullcontext(self.transport) if self.transport is not None else httpx.Client()
        try:
            with context as client:
                response = client.post(
                    self.endpoint, headers={"Authorization": f"Bearer {self.config.api_key}"},
                    json=body, timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise RerankRequestError(f"重排接口请求失败（HTTP {code}）",
                                     retryable=code in (408, 429) or 500 <= code < 600) from None
        except httpx.TimeoutException:
            raise RerankRequestError("重排接口请求超时", retryable=True) from None
        except httpx.RequestError:
            raise RerankRequestError("重排接口连接失败", retryable=True) from None
        except ValueError:
            raise RerankRequestError("重排接口响应不是有效 JSON") from None

        return [
            replace(hits[index], score=score, rerank_score=score)
            for index, score in self._parse_response(payload, len(hits), top_n)
        ]

    @staticmethod
    def _parse_response(payload: Any, candidate_count: int, top_n: int) -> list[tuple[int, float]]:
        """核对响应数量、下标、分数，再生成稳定排名。"""
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != top_n:
            raise RerankRequestError("重排结果数量与 top_n 不匹配")
        ranked = {}
        for item in results:
            if not isinstance(item, dict):
                raise RerankRequestError("重排结果项必须为对象")
            index, score = item.get("index"), item.get("relevance_score")
            if type(index) is not int or not 0 <= index < candidate_count or index in ranked:
                raise RerankRequestError("重排结果下标越界或重复")
            try:
                if type(score) not in (int, float) or not math.isfinite(score):
                    raise ValueError
                ranked[index] = float(score)
            except (OverflowError, ValueError):
                raise RerankRequestError("重排分数必须是有限数值") from None
        return sorted(ranked.items(), key=lambda hit: (-hit[1], hit[0]))
