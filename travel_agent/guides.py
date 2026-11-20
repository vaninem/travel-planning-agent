"""旅游知识库的 Markdown 入库和中文检索入口。"""

import argparse
import hashlib
import logging
import os
from functools import lru_cache
from pathlib import Path

from .domain import ProviderError
from .rag_engine.rag.types import RAGSearchOptions
from .rag_engine.tools.rag import RAGTool
from .settings import ROOT, load_settings


logger = logging.getLogger(__name__)
MODES = ("auto", "lexical", "vector", "hybrid")


def _configured_mode() -> str:
    mode = os.environ.get("TRAVEL_GUIDE_RETRIEVAL", "auto").strip().lower()
    if mode not in MODES:
        logger.warning("无效的攻略检索配置，使用 BM25")
        return "lexical"
    if mode != "auto":
        return mode
    required = ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY", "DATABASE_URL")
    return "hybrid" if all(os.getenv(key, "").strip() for key in required) else "lexical"


def _new_embedding_index():
    from .rag_engine.embeddings import EmbeddingConfig, HTTPEmbeddingClient, IncrementalEmbeddingIndex

    # 在项目根目录显式读取配置；不依赖引擎所在包的路径。
    environment = {"EMBEDDING_DB_SCHEMA": "travel_guide_embeddings", **os.environ}
    config = EmbeddingConfig.from_env(ROOT / ".env", environ=environment)
    if not config.enabled or not config.database_configured:
        raise RuntimeError("攻略向量检索尚未配置嵌入服务或数据库")
    client = None
    if os.getenv("TRAVEL_GUIDE_EMBEDDING_DIRECT", "0").strip() == "1":
        import httpx

        class DirectTransport:
            def post(self, *args, **kwargs):
                with httpx.Client(trust_env=False) as transport:
                    return transport.post(*args, **kwargs)

        client = HTTPEmbeddingClient(config, transport=DirectTransport())
    index = IncrementalEmbeddingIndex(config, client=client)
    index.initialize()
    return index


@lru_cache(maxsize=3)
def _index(mode: str = "lexical") -> RAGTool:
    guide_path = Path(os.environ.get("TRAVEL_GUIDE_DIR") or Path(__file__).with_name("guide_data"))
    guide_path = guide_path.expanduser().resolve()
    documents = sorted(guide_path.rglob("*.md"))
    if not documents:
        raise ProviderError("rag_unavailable", "攻略知识库没有 Markdown 文档")
    # 文件清单变更后使用新的命名空间；相同内容仍可复用持久化向量。
    names = "\n".join(document.relative_to(guide_path).as_posix() for document in documents)
    revision = hashlib.sha256(names.encode("utf-8")).hexdigest()
    try:
        options = RAGSearchOptions(mode=mode)
        embedding = _new_embedding_index() if mode != "lexical" else None
        index = RAGTool(
            guide_path, chunk_size=200, chunk_overlap=24,
            repo_id=f"travel-guides:{guide_path}", revision=revision,
            embedding_index=embedding, search_options=options,
        )
        for document in documents:
            index.add_document(document)
        if embedding is not None:
            status = index.prepare_index()
            if status.pending or status.running or status.failed or status.ready != len(index.chunks):
                logger.warning("攻略向量状态：ready=%s pending=%s running=%s failed=%s",
                               status.ready, status.pending, status.running, status.failed)
                raise RuntimeError("攻略向量索引尚未就绪")
        logger.info("攻略索引已就绪：mode=%s chunks=%s", mode, len(index.chunks))
        return index
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        if mode == "lexical":
            raise
        logger.warning("攻略 %s 索引不可用（%s），使用 BM25", mode, type(exc).__name__)
        return _index("lexical")


def prepare_guides() -> RAGTool:
    """在 Worker 启动阶段完成索引，查询时只进行召回与查询编码。"""
    return _index(_configured_mode())


def search_guides(query: str, limit: int = 4, *, mode: str | None = None) -> list[dict]:
    try:
        index = _index(mode or _configured_mode())
        try:
            hits = index.search(query, limit=limit)
        except (OSError, RuntimeError) as exc:
            if index.search_options.mode == "lexical":
                raise
            logger.warning("攻略向量查询失败（%s），本次使用 BM25", type(exc).__name__)
            hits = index.search(query, limit=limit, options=RAGSearchOptions(mode="lexical"))
    except (ImportError, OSError, UnicodeError) as exc:
        raise ProviderError("rag_unavailable", "无法读取或加载攻略知识库") from exc
    return [
        {
            "content": hit.chunk.content,
            "source": hit.chunk.source,
            "heading": hit.chunk.heading_path,
            "start_line": hit.chunk.metadata.get("start_line"),
            "end_line": hit.chunk.metadata.get("end_line"),
        }
        for hit in hits
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="同步并准备攻略检索索引")
    parser.add_argument("--require-mode", choices=("lexical", "vector", "hybrid"), help="若实际模式不匹配则返回失败")
    args = parser.parse_args()
    load_settings()
    logging.basicConfig(level=logging.INFO)
    try:
        index = prepare_guides()
    except (ProviderError, OSError, RuntimeError, ValueError) as exc:
        logger.error("攻略索引准备失败（%s）", type(exc).__name__)
        return 1
    print(f"攻略索引: {index.search_options.mode}; {len(index.chunks)} 个块")
    return int(args.require_mode is not None and index.search_options.mode != args.require_mode)


if __name__ == "__main__":
    raise SystemExit(main())
