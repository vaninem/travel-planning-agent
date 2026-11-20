"""保留原文位置的文档和代码库检索入口。"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, TYPE_CHECKING, TypedDict

from ..ports import ChatModel
from ..rag.chunking import split_python
from ..rag.expansion import build_symbol_index, expand_dependencies, expand_parents, retrieve_iteratively
from ..rag.query import plan_query
from ..rag.recall import filter_chunks, index_text
from ..rag.rerank import ECNUReranker
from ..rag.types import (
    CHUNK_CHARS_PER_TOKEN, DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_TOKENS, DEFAULT_INDEX_HEADER_CHARS,
    DEFAULT_PARENT_TOKENS, QueryPlan, RAGSearchOptions,
)
from ..retrieval import bm25_rank, fuse_rankings
from ..tokens import ApproxTokenCounter
from ..types import DocumentChunk, SearchHit, ToolResult
from .base import BaseTool


if TYPE_CHECKING:
    from ..embeddings import IncrementalEmbeddingIndex, IndexStatus


class _TextSection(TypedDict):
    heading_path: str | None
    start: int
    end: int
    paragraphs: list[tuple[str, int, int]]


class _RAGTokenCounter(ApproxTokenCounter):
    """近似计数加字符上限，避免长标识符或空白占满文本模型输入。"""

    def count(self, text: str) -> int:
        char_cost = (len(text) + CHUNK_CHARS_PER_TOKEN - 1) // CHUNK_CHARS_PER_TOKEN
        return max(super().count(text), char_cost)

    def truncate(self, text: str, max_tokens: int) -> str:
        return super().truncate(text[:max_tokens * CHUNK_CHARS_PER_TOKEN], max_tokens)


class RAGTool(BaseTool):
    """在显式加入的文件快照内执行可追溯检索。"""

    def __init__(
        self,
        knowledge_base_path: Path,
        chunk_size: int = DEFAULT_CHUNK_TOKENS,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        token_counter: Any | None = None,
        *,
        repo_id: str | None = None,
        revision: str = "working-tree",
        embedding_index: IncrementalEmbeddingIndex | None = None,
        reranker: ECNUReranker | None = None,
        query_model: ChatModel | None = None,
        search_options: RAGSearchOptions | None = None,
        parent_max_tokens: int | None = None,
    ):
        """保存目录与近似 token 预算；不自动读文件或建立外部索引。

        chunk_size/chunk_overlap 均以同一个 token_counter 计数，不是字符数。
        默认使用 ApproxTokenCounter；可注入 fake 计数器来隔离 E01 与 E04。
        """
        super().__init__("rag", "文档和代码库证据检索")
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk_size 必须为正整数")
        if type(chunk_overlap) is not int or not 0 <= chunk_overlap < chunk_size:
            raise ValueError("chunk_overlap 必须为非负整数且小于 chunk_size")
        if parent_max_tokens is not None and (type(parent_max_tokens) is not int or parent_max_tokens < chunk_size):
            raise ValueError("类父块预算必须是大于等于子块预算的整数")
        self.knowledge_base_path = Path(knowledge_base_path)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        default_parent = DEFAULT_PARENT_TOKENS if chunk_size == DEFAULT_CHUNK_TOKENS else 2 * chunk_size
        self.parent_max_tokens = parent_max_tokens if parent_max_tokens is not None else default_parent
        self.token_counter = token_counter if token_counter is not None else _RAGTokenCounter()
        self.chunks: list[DocumentChunk] = []
        self.sources: dict[str, str] = {}
        self.reranker = reranker
        self.query_model = query_model
        if search_options is not None and not isinstance(search_options, RAGSearchOptions):
            raise ValueError("search_options 必须是 RAGSearchOptions")
        self.search_options = search_options if search_options is not None else RAGSearchOptions()
        if repo_id is not None and (not isinstance(repo_id, str) or not repo_id.strip()):
            raise ValueError("repo_id 必须为非空字符串")
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError("revision 必须为非空字符串")
        self.repo_id = repo_id if repo_id is not None else str(self.knowledge_base_path.resolve())
        self.revision = revision
        self._vector = None
        if embedding_index is not None:
            from ..rag.embedding_bridge import RAGEmbeddingBridge

            self._vector = RAGEmbeddingBridge(embedding_index, self.repo_id, revision)

    def run(self, parameters: dict[str, Any]) -> ToolResult:
        """分发索引和搜索操作。"""
        if not isinstance(parameters, dict):
            raise ValueError("工具参数必须为字典")
        action = parameters.get("action")
        routes = {
            "add_document": (self.add_document, {"file_path"}, {"file_path"}),
            "restore_document": (self.restore_document, {"file_path"}, {"file_path"}),
            "delete_document": (self.delete_document, {"file_path"}, {"file_path"}),
            "process_pending": (self.process_pending, {"limit"}, set()),
            "search": (self.search, {"query", "limit", "options"}, {"query"}),
        }
        if not isinstance(action, str) or action not in routes:
            raise ValueError("不支持的 rag action")
        function, allowed, required = routes[action]
        arguments = {key: value for key, value in parameters.items() if key != "action"}
        if set(arguments) - allowed or required - set(arguments):
            raise ValueError("rag 参数字段不符合所选 action")
        if isinstance(arguments.get("options"), dict):
            arguments["options"] = RAGSearchOptions(**arguments["options"])
        data = function(**arguments)
        return ToolResult(success=True, data=data, metadata={"tool": self.name, "action": action})

    def add_document(self, file_path: Path) -> list[DocumentChunk]:
        """读取、分块并替换单文件快照，已配置向量时同时登记增量任务。"""
        source, original, language = self._read_source(file_path)
        if language == "python":
            chunks = split_python(
                source, original, self.chunk_size, self.token_counter, parent_max_tokens=self.parent_max_tokens,
            )
        else:
            chunks = []
            for section in self._split_paragraphs_with_headings(original):
                for chunk in self._chunk_paragraphs(original, section):
                    chunk.metadata.update(parent_start=section["start"], parent_end=section["end"])
                    chunks.append(chunk)
        starts = [0, *(match.end() for match in re.finditer(r"\r\n|[\r\n]", original))]
        source_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        occurrences: Counter[str] = Counter()
        prepared = []
        for chunk in chunks:
            if (not 0 <= chunk.start < chunk.end <= len(original)
                    or original[chunk.start:chunk.end] != chunk.content or not chunk.content.strip()
                    or self.token_counter.count(chunk.content) > self.chunk_size):
                raise ValueError("文件块必须是预算内的非空原文切片")
            metadata = dict(chunk.metadata)
            if self.chunk_size == DEFAULT_CHUNK_TOKENS:
                metadata["index_header_max_chars"] = DEFAULT_INDEX_HEADER_CHARS
            default_parent = (0, len(original)) if language != "python" and not chunk.heading_path else (chunk.start, chunk.end)
            parent_start = metadata.get("parent_start", default_parent[0])
            parent_end = metadata.get("parent_end", default_parent[1])
            if (type(parent_start) is not int or type(parent_end) is not int
                    or not 0 <= parent_start <= chunk.start < chunk.end <= parent_end <= len(original)):
                raise ValueError("父区间必须包含子块并位于原文内")
            key = json.dumps(
                [source, chunk.heading_path, metadata.get("qualified_symbol") or metadata.get("symbol"), chunk.content],
                ensure_ascii=False,
            )
            occurrences[key] += 1
            identity = hashlib.sha256(f"{occurrences[key]}\n{key}".encode("utf-8")).hexdigest()
            metadata.update(
                language=language, source_hash=source_hash, parent_start=parent_start, parent_end=parent_end,
                start_line=bisect_right(starts, chunk.start), end_line=bisect_right(starts, chunk.end - 1),
            )
            prepared.append(DocumentChunk(
                f"chunk_{identity}", source, chunk.content, chunk.heading_path, chunk.start, chunk.end, metadata,
            ))
        if self._vector is not None:
            self._vector.sync_source(source, prepared)
        self._replace_source(source, original, prepared)
        return prepared

    def restore_document(self, file_path: Path) -> list[DocumentChunk]:
        """显式从持久索引恢复已同步文件的本地候选。"""
        if self._vector is None:
            raise ValueError("恢复索引需要配置向量索引")
        source, original, _ = self._read_source(file_path)
        chunks = self._vector.restore_source(source, original)
        self._replace_source(source, original, chunks)
        return chunks

    def delete_document(self, file_path: Path) -> None:
        """移除单个来源的块，文件已经从工作区删除时也可以调用。"""
        _, source = self._source_path(file_path)
        if self._vector is not None:
            self._vector.sync_source(source, [])
        self.chunks = [chunk for chunk in self.chunks if chunk.source != source]
        self.sources.pop(source, None)

    def process_pending(self, limit: int | None = None) -> int:
        """显式运行已有索引的嵌入任务处理器。"""
        if self._vector is None:
            raise ValueError("处理向量任务需要配置向量索引")
        return self._vector.index.process_pending(limit=limit)

    def prepare_index(self) -> IndexStatus:
        """在用户查询前处理当前仓库版本的向量任务。"""
        if self._vector is None:
            raise ValueError("准备向量索引需要配置向量索引")
        return self._vector.index.prepare_namespace(self._vector.namespace)

    def _source_path(self, file_path: Path) -> tuple[Path, str]:
        root = self.knowledge_base_path.resolve()
        path = (root / Path(file_path)).resolve()
        if path.is_relative_to(root):
            return path, path.relative_to(root).as_posix()
        if self._vector is not None:
            raise ValueError("向量索引文件必须位于仓库根目录内")
        return path, path.as_posix()

    def _read_source(self, file_path: Path) -> tuple[str, str, str]:
        path, source = self._source_path(file_path)
        language = {".md": "markdown", ".txt": "text", ".py": "python"}.get(path.suffix.lower())
        if language is None:
            raise ValueError("仅支持 UTF-8 .md、.txt 和 .py")
        with path.open(encoding="utf-8", newline="") as file:
            return source, file.read(), language

    def _replace_source(self, source: str, original: str, chunks: list[DocumentChunk]) -> None:
        self.chunks = [chunk for chunk in self.chunks if chunk.source != source] + chunks
        self.sources[source] = original

    def _split_paragraphs_with_headings(self, text: str) -> list[_TextSection]:
        """按行首标题划分章节，按空行分段，并保留两层原文区间。"""
        current = 0
        section_start = 0
        paragraph_start: int | None = None
        paragraph_end = 0
        headings: list[tuple[int, str]] = []
        section_paragraphs: list[tuple[str, int, int]] = []
        sections: list[_TextSection] = []

        def flush_paragraph() -> None:
            """把待保存的段落放入当前章节。"""
            nonlocal paragraph_start
            if paragraph_start is None:
                return
            section_paragraphs.append((
                text[paragraph_start:paragraph_end], paragraph_start, paragraph_end
            ))
            paragraph_start = None

        def flush_section(end: int) -> None:
            """保存包含正文的章节，并清空待保存的段落。"""
            if not section_paragraphs:
                return
            sections.append({
                "heading_path": " > ".join(title for _, title in headings if title) or None,
                "start": section_start,
                "end": end,
                "paragraphs": section_paragraphs.copy(),
            })
            section_paragraphs.clear()

        for raw_line in text.splitlines(keepends=True):
            line_start = current
            current += len(raw_line)
            line = raw_line.rstrip("\r\n")
            level = len(line) - len(line.lstrip("#"))
            if not line.strip():
                flush_paragraph()
            elif 1 <= level <= 6 and (level == len(line) or line[level] in " \t"):
                flush_paragraph()
                flush_section(line_start)
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, line[level:].strip()))
                section_start = line_start
            else:
                if paragraph_start is None:
                    paragraph_start = line_start
                paragraph_end = line_start + len(line)
        flush_paragraph()
        flush_section(len(text))
        return sections

    def _chunk_paragraphs(
        self,
        original_text: str,
        section: _TextSection,
    ) -> list[DocumentChunk]:
        """在一个章节内按 token 预算组块，保留正文的原文半开区间。

        优先合并完整段落，超长段落改用完整句子组块，超长单句按 token 滑窗。
        段落和句子的重叠只复用完整的尾部区间。入库入口填写文件和父章节信息。
        """
        token_counter = self.token_counter
        chunk_size = self.chunk_size
        chunk_overlap = self.chunk_overlap
        sentence_end_pattern = re.compile(r"[。！？!?]+|\.(?=\s|$)")

        def pack_spans(
            spans: Iterable[tuple[int, int]],
            split_oversized: Callable[[int, int], Iterator[tuple[int, int]]],
        ) -> Iterator[tuple[int, int]]:
            """组合同一层的完整区间，将超长区间交给下一层。"""
            current: list[tuple[int, int]] = []
            for start, end in spans:
                content = original_text[start:end]
                if not content.strip():
                    continue
                if token_counter.count(content) > chunk_size:
                    if current:
                        yield current[0][0], current[-1][1]
                        current.clear()
                    yield from split_oversized(start, end)
                    continue

                if current and token_counter.count(original_text[current[0][0]:end]) > chunk_size:
                    previous_end = current[-1][1]
                    yield current[0][0], previous_end
                    keep_from = len(current)
                    while chunk_overlap and keep_from > 0:
                        tail_start = current[keep_from - 1][0]
                        if token_counter.count(original_text[tail_start:previous_end]) > chunk_overlap:
                            break
                        if token_counter.count(original_text[tail_start:end]) > chunk_size:
                            break
                        keep_from -= 1
                    current = current[keep_from:]
                current.append((start, end))

            if current:
                yield current[0][0], current[-1][1]

        def slide_sentence(start: int, end: int) -> Iterator[tuple[int, int]]:
            """按 token 窗口续拆一句话，并确保每次覆盖新的原文。"""
            stride = chunk_size - chunk_overlap
            window_start = covered_end = start
            while window_start < end:
                content = token_counter.truncate(original_text[window_start:end], chunk_size)
                window_end = window_start + len(content)
                if window_end <= covered_end:
                    if window_start == covered_end:
                        raise ValueError("块预算放不下剩余正文的首个字符")
                    window_start = covered_end
                    continue

                if content.strip():
                    yield window_start, window_end
                if window_end == end:
                    return

                prefix = token_counter.truncate(content, stride)
                next_start = window_start + max(1, len(prefix))
                while (
                    next_start < window_end
                    and token_counter.count(original_text[next_start:window_end]) > chunk_overlap
                ):
                    next_start += 1
                covered_end = window_end
                window_start = next_start

        def pack_sentences(start: int, end: int) -> Iterator[tuple[int, int]]:
            """识别超长段落的句界，并按完整句子组块。"""
            sentence_spans = []
            sentence_start = start
            for match in sentence_end_pattern.finditer(original_text, start, end):
                sentence_end = match.end()
                sentence_spans.append((sentence_start, sentence_end))
                sentence_start = sentence_end
            if sentence_start < end:
                sentence_spans.append((sentence_start, end))
            yield from pack_spans(sentence_spans, slide_sentence)

        paragraph_spans = ((start, end) for _, start, end in section["paragraphs"])
        return [
            DocumentChunk(
                id="",
                source="",
                content=original_text[start:end],
                heading_path=section["heading_path"],
                start=start,
                end=end,
            )
            for start, end in pack_spans(paragraph_spans, pack_sentences)
        ]

    def search(
        self, query: str, limit: int = 5, *, options: RAGSearchOptions | None = None,
    ) -> list[SearchHit]:
        """按范围召回文件块，依显式选项重排和扩展。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 必须是非空字符串")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        options = options if options is not None else self.search_options
        if not isinstance(options, RAGSearchOptions):
            raise ValueError("options 必须是 RAGSearchOptions")
        if options.mode != "lexical" and self._vector is None:
            raise ValueError("向量或混合检索需要配置向量索引")
        if options.rerank and self.reranker is None:
            raise ValueError("开启重排需要注入文本重排器")
        candidates = filter_chunks(self.chunks, options)
        if not candidates:
            return []

        plan = QueryPlan((query,), (query,))
        if options.rewrite_query or options.hyde:
            plan = plan_query(query, self.query_model, rewrite=options.rewrite_query, hyde=options.hyde)
        recall_limit = max(limit, options.candidate_limit)
        hits = self._recall(plan, candidates, recall_limit, options.mode)
        if options.rounds > 1 and hits:
            hits = retrieve_iteratively(
                query, hits, lambda follow_up: self._recall(
                    QueryPlan((follow_up,), (follow_up,)), candidates, recall_limit, options.mode,
                ),
                options.rounds, recall_limit,
            )
        if options.rerank and hits:
            hits = self.reranker.rerank(query, hits, limit)
        hits = hits[:limit]
        matched_hits = hits
        if options.follow_dependencies and hits:
            primary_count = max(1, limit // 2)
            expanded = expand_dependencies(hits[:primary_count], build_symbol_index(candidates), limit)
            recalled_by_id = {hit.chunk.id: hit for hit in hits}
            expanded = [recalled_by_id.get(hit.chunk.id, hit) for hit in expanded]
            used_ids = {hit.chunk.id for hit in expanded}
            hits = (expanded + [hit for hit in hits[primary_count:] if hit.chunk.id not in used_ids])[:limit]
        if options.expand_parents and hits:
            hits = expand_parents(
                hits, self.sources, options.max_context_tokens, self.token_counter,
                candidates=candidates, matched_hits=matched_hits, max_parent_tokens=self.parent_max_tokens,
            )
        return hits

    def _recall(
        self, plan: QueryPlan, candidates: list[DocumentChunk], limit: int, mode: str,
    ) -> list[SearchHit]:
        """复用 BM25 和 RRF，先合并同路查询，再融合词法和向量两路。"""
        routes = []
        for route, queries in (("lexical", plan.lexical_queries), ("vector", plan.vector_queries)):
            if mode != "hybrid" and mode != route:
                continue
            variants = []
            for index, query in enumerate(queries):
                if route == "lexical":
                    ranked = bm25_rank(query, candidates, limit, key=lambda chunk: chunk.id, text=index_text)
                else:
                    ranked = [(hit.chunk, hit.score) for hit in self._vector.search(query, candidates, limit)]
                variants.append((str(index), 1.0, ranked))
            if len(variants) == 1:
                ranked = variants[0][2]
            else:
                ranked = [(hit.item, hit.score) for hit in fuse_rankings(
                    variants, limit, key=lambda chunk: chunk.id,
                )]
            routes.append((route, 1.0, ranked))
        if len(routes) == 1:
            route, _, ranked = routes[0]
            return [SearchHit(
                chunk, score, lexical_score=score if route == "lexical" else None,
                vector_score=score if route == "vector" else None,
            ) for chunk, score in ranked]
        return [SearchHit(
            hit.item, hit.score, lexical_score=hit.scores.get("lexical"),
            vector_score=hit.scores.get("vector"), ranks=hit.ranks, fusion_score=hit.score,
        ) for hit in fuse_rankings(routes, limit, key=lambda chunk: chunk.id)]
