"""检索范围与供词法、向量和重排共用的索引文本。"""

from pathlib import PurePosixPath

from ..types import DocumentChunk
from .types import RAGSearchOptions


def index_text(chunk: DocumentChunk) -> str:
    """给检索模型补文件、标题及符号，原文块单独保留。"""
    labels = (chunk.source, chunk.heading_path, chunk.metadata.get("qualified_symbol") or chunk.metadata.get("symbol"))
    header = "\n".join(value for value in labels if isinstance(value, str) and value)
    header = header[:chunk.metadata.get("index_header_max_chars")]
    return f"{header}\n{chunk.content}" if header else chunk.content


def filter_chunks(chunks: list[DocumentChunk], options: RAGSearchOptions) -> list[DocumentChunk]:
    """用一致的文件、路径和语言条件限制各路召回。"""
    prefix = PurePosixPath(options.path_prefix) if options.path_prefix is not None else None
    return [
        chunk for chunk in chunks
        if (options.source is None or chunk.source == options.source)
        and (prefix is None or PurePosixPath(chunk.source).is_relative_to(prefix))
        and (options.language is None or chunk.metadata.get("language") == options.language)
    ]
