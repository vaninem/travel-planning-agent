"""运行时错误契约已提供，TODO 实现时使用；不要用 [] 吞掉这些错误。"""


class EmbeddingNotConfigured(RuntimeError):
    """嵌入服务未配置；调用方可明确选择本地检索。"""


class EmbeddingRequestError(RuntimeError):
    """脱敏的接口错误；不要拼接请求鉴权头或原始敏感响应。"""

    def __init__(
        self, message: str, error_code: str = "invalid_response", retryable: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class IndexNotReady(RuntimeError):
    """索引有未完成/失败任务；调用方应提示状态或明确降级。"""


class EmbeddingStorageError(RuntimeError):
    """嵌入存储错误；消息不包含数据库口令或原始敏感异常。"""
