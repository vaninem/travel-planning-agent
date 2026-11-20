"""E11：批量嵌入 API 适配器。"""

from contextlib import nullcontext
import math
from typing import Any, Protocol, cast

import httpx

from .config import EmbeddingConfig
from .errors import EmbeddingNotConfigured, EmbeddingRequestError


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HTTPEmbeddingClient:
    def __init__(self, config: EmbeddingConfig, transport: Any = None):
        """只保存依赖；transport 为具有 post 方法的 HTTP 客户端，由调用方管理。

        可传入使用 httpx.MockTransport 的 httpx.Client，测试无需真实请求。
        """
        self.config = config
        self.transport = transport
        self.endpoint = config.base_url.rstrip("/") + "/embeddings"
        self.calls = 0
        self.dimensions = config.expected_dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        """请求 /embeddings，返回与输入顺序一致的向量列表。

        1. model/input/encoding_format=float；Authorization 只进入请求头。
        2. 按 config.batch_size 分批，不把单批大小当作接口全部限制。
        3. 用 _parse_response 检查 index，不能默认服务返回顺序等于输入顺序。
        4. 区分未配置、401、429、超时、响应损坏；日志不含密钥/鉴权头。
        5. 空输入返回 []；失败时不返回部分结果或伪造向量。
        HTTPX 为可选依赖，若使用 SOCKS 代理需安装 httpx[socks]。
        """
        if not isinstance(texts, list) or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("texts 必须是非空字符串组成的列表")
        if not texts:
            return []
        if not self.config.enabled:
            raise EmbeddingNotConfigured("请配置嵌入服务的地址、模型和 API Key")
        batch_size = self.config.batch_size
        result: list[list[float]] = []
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        # 默认客户端复用批次间的连接；注入的客户端仍归调用方管理。
        context = nullcontext(self.transport) if self.transport is not None else httpx.Client()
        try:
            with context as client:
                for start in range(0, len(texts), batch_size):
                    batch = texts[start:start + batch_size]
                    body = {
                        "model": self.config.model,
                        "input": batch,
                        "encoding_format": "float",
                    }
                    self.calls += 1  # 统计实际发起的请求次数，包括失败请求。
                    response = client.post(
                        self.endpoint,
                        headers=headers,
                        json=body,
                        timeout=self.config.timeout_seconds,
                    )
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except ValueError:
                        raise EmbeddingRequestError("嵌入接口响应不是有效 JSON", error_code="invalid_json") from None
                    vectors = self._parse_response(payload, len(batch))
                    result.extend(vectors)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            reason = {401: "鉴权失败", 403: "无访问权限", 429: "请求限流或配额不足"}.get(code, "HTTP 请求失败")
            raise EmbeddingRequestError(
                f"嵌入接口{reason}（HTTP {code}）", error_code=f"http_{code}",
                retryable=code in (408, 429) or 500 <= code < 600,
            ) from None
        except httpx.TimeoutException:
            raise EmbeddingRequestError("嵌入接口请求超时", error_code="timeout", retryable=True) from None
        except httpx.RequestError:
            raise EmbeddingRequestError("嵌入接口网络连接失败", error_code="connection_error", retryable=True) from None
        return result

    def _parse_response(self, payload: dict, expected_count: int) -> list[list[float]]:
        """解析响应，校验数量、唯一完整 index、非空有限数值、维度一致。

        按 index 恢复输入顺序，拒绝重复/缺失 index、零范数和跨批维度漂移；
        已配置 expected_dimensions 时核对，不给所有模型写死 1024。
        """
        if type(expected_count) is not int or expected_count < 0:
            raise ValueError("expected_count 必须是非负整数")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != expected_count:
            raise EmbeddingRequestError("嵌入响应 data 必须为与输入数量一致的列表")

        result: list[list[float] | None] = [None] * expected_count
        dimensions = self.dimensions
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingRequestError("嵌入响应项必须为对象")
            index, embedding = item.get("index"), item.get("embedding")
            if type(index) is not int or not 0 <= index < expected_count:
                raise EmbeddingRequestError("嵌入响应 index 越界或不是整数")
            if result[index] is not None:
                raise EmbeddingRequestError("嵌入响应 index 重复")
            if not isinstance(embedding, list) or not embedding:
                raise EmbeddingRequestError("嵌入向量必须为非空数值列表")
            if any(type(value) not in (int, float) for value in embedding):
                raise EmbeddingRequestError("嵌入向量包含非数值元素")
            try:
                vector = [float(value) for value in embedding]
            except (OverflowError, ValueError):
                raise EmbeddingRequestError("嵌入向量数值超出可表示范围") from None
            if not all(math.isfinite(value) for value in vector):
                raise EmbeddingRequestError("嵌入向量包含非有限数值")
            if not any(value != 0.0 for value in vector):
                raise EmbeddingRequestError("嵌入向量不能为零向量")
            if dimensions is None:
                dimensions = len(vector)
            if len(vector) != dimensions:
                raise EmbeddingRequestError("嵌入向量维度与配置或之前的响应不一致")
            result[index] = vector

        # 全部通过后才记录维度，失败响应不改变客户端状态。
        self.dimensions = dimensions
        return cast(list[list[float]], result)
