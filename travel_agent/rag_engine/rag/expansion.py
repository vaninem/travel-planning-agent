"""检索后的上下文扩展与跨文件查找。"""

import hashlib
from collections import defaultdict
from dataclasses import replace
from typing import Any, Callable

from ..retrieval import fuse_rankings
from ..types import DocumentChunk, SearchHit


def expand_parents(
    hits: list[SearchHit], sources: dict[str, str], max_tokens: int, token_counter: Any,
    *, candidates: list[DocumentChunk], matched_hits: list[SearchHit] | None = None,
    max_parent_tokens: int | None = None,
) -> list[SearchHit]:
    """按排名和总预算选择证据，合并命中过半的 Python 父块。

    候选和召回命中按子块 ID 去重。一个父块至少有两个子块命中才尝试合并，
    合并后用首个命中作为引用，父正文放在 context_chunk。父范围不可用、
    超预算、命中不足或属于文本文件时，尝试保留原始子块。
    """
    if type(max_tokens) is not int or max_tokens < 0:
        raise ValueError("max_tokens 必须是非负整数")

    def parent_key(chunk: DocumentChunk) -> tuple[str, int, int] | None:
        language = chunk.metadata.get("language")
        is_python = chunk.source.lower().endswith(".py") if language is None else language == "python"
        if not is_python:
            return None
        start = chunk.metadata.get("parent_start")
        end = chunk.metadata.get("parent_end")
        original = sources.get(chunk.source)
        if (original is None or type(start) is not int or type(end) is not int
                or not 0 <= start <= chunk.start < chunk.end <= end <= len(original)):
            return None
        return chunk.source, start, end

    matched_children: dict[tuple[str, int, int], set[str]] = defaultdict(set)
    retrieved_hits = hits if matched_hits is None else matched_hits
    for hit in retrieved_hits:
        key = parent_key(hit.chunk)
        if key is not None:
            matched_children[key].add(hit.chunk.id)

    candidate_children: dict[tuple[str, int, int], set[str]] = defaultdict(set)
    for chunk in candidates:
        key = parent_key(chunk)
        if key is not None and key in matched_children:
            candidate_children[key].add(chunk.id)

    mergeable_parents = set()
    for key, candidate_ids in candidate_children.items():
        matched_count = len(matched_children[key] & candidate_ids)
        if matched_count >= 2 and 2 * matched_count > len(candidate_ids):
            mergeable_parents.add(key)

    results: list[SearchHit] = []
    merged_parents = set()
    source_hashes: dict[str, str] = {}
    remaining = max_tokens
    for hit in hits:
        if remaining == 0:
            break
        child = hit.chunk
        key = parent_key(child)
        if key in merged_parents:
            continue

        if key is not None and key in mergeable_parents:
            source, start, end = key
            original = sources[source]
            expected_hash = child.metadata.get("source_hash")
            if expected_hash is not None and source not in source_hashes:
                source_hashes[source] = hashlib.sha256(original.encode("utf-8")).hexdigest()
            same_snapshot = expected_hash is None or source_hashes[source] == expected_hash
            if same_snapshot and original[child.start:child.end] == child.content:
                parent_text = original[start:end]
                parent_tokens = token_counter.count(parent_text)
                # 预算只会减少，同一个父块在后续位置也需要相同的预算。
                mergeable_parents.remove(key)
                if parent_tokens <= remaining and (max_parent_tokens is None or parent_tokens <= max_parent_tokens):
                    parent = DocumentChunk(
                        id=f"parent:{source}:{start}:{end}", source=source, content=parent_text,
                        heading_path=child.heading_path, start=start, end=end,
                    )
                    results.append(replace(hit, context_chunk=parent))
                    merged_parents.add(key)
                    remaining -= parent_tokens
                    continue

        child_tokens = token_counter.count(child.content)
        if child_tokens <= remaining:
            results.append(hit if hit.context_chunk is None else replace(hit, context_chunk=None))
            remaining -= child_tokens
    return results


def build_symbol_index(chunks: list[DocumentChunk]) -> dict[str, list[DocumentChunk]]:
    """为当前检索范围内的定义建立短名和限定名索引。

    同一文件中共享限定名的子块使用起始位置最早的一块作为代表。
    缺少限定名时按短名分组。不同文件中的同名定义各自保留。
    """
    def names_of(chunk: DocumentChunk) -> tuple[str, ...]:
        names = (chunk.metadata.get("qualified_symbol"), chunk.metadata.get("symbol"))
        return tuple(dict.fromkeys(name for name in names if isinstance(name, str) and name.strip()))

    representatives: dict[tuple[str, str], DocumentChunk] = {}

    for chunk in chunks:
        names = names_of(chunk)
        if not names:
            continue
        key = (chunk.source, names[0])
        existing = representatives.get(key)
        if existing is None or chunk.start < existing.start:
            representatives[key] = chunk

    symbol_to_chunk: dict[str, list[DocumentChunk]] = {}
    for chunk in representatives.values():
        for name in names_of(chunk):
            symbol_to_chunk.setdefault(name, []).append(chunk)

    return symbol_to_chunk


def expand_dependencies(
    hits: list[SearchHit], symbols: dict[str, list[DocumentChunk]], limit: int,
) -> list[SearchHit]:
    """保留原命中顺序，沿引用补充定义块，结果总数受 limit 限制。

    唯一候选可以直接加入，短名存在歧义时仅选同文件的唯一候选。
    每个定义块最多追加一次，并在块副本的元数据里记录触发它的引用。
    """
    if type(limit) is not int or limit < 0:
        raise ValueError("limit 必须是非负整数")

    results = hits[:limit]
    if len(results) == limit:
        return results
    seen_ids = {hit.chunk.id for hit in results}
    for hit in hits:
        references = hit.chunk.metadata.get("references", [])
        if not isinstance(references, (list, tuple)):
            continue
        for reference in references:
            if not isinstance(reference, str) or not reference.strip():
                continue
            candidates = symbols.get(reference, [])
            if len(candidates) == 1:
                target = candidates[0]
            elif len(candidates) > 1 and "." not in reference:
                same_file = [
                    chunk for chunk in candidates
                    if chunk.source == hit.chunk.source
                ]
                if len(same_file) != 1:
                    continue
                target = same_file[0]
            else:
                continue
            if target.id in seen_ids:
                continue
            dependency = replace(
                target,
                metadata={
                    **target.metadata,
                    "dependency_from_chunk_id": hit.chunk.id,
                    "dependency_reference": reference,
                },
            )
            results.append(SearchHit(chunk=dependency, score=0.0))
            seen_ids.add(target.id)
            if len(results) >= limit:
                return results
    return results


def retrieve_iteratively(
    query: str, initial: list[SearchHit], lookup: Callable[[str], list[SearchHit]],
    max_rounds: int, limit: int,
) -> list[SearchHit]:
    """沿新命中里的引用继续召回，最后按块 ID 融合各轮排名。

    max_rounds 包含首轮。每轮用原查询和一个未搜索过的引用调用 lookup。
    找不到新引用、没有新块或达到轮数上限时停止。RRF 保留块首次命中时
    的原始分路分数。只有首轮结果时直接保留原排名和分数。
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    if type(max_rounds) is not int or max_rounds < 1:
        raise ValueError("max_rounds 必须是正整数")
    if type(limit) is not int or limit < 0:
        raise ValueError("limit 必须是非负整数")
    if limit == 0 or not initial:
        return []

    def unique_hits(hits: list[SearchHit]) -> list[SearchHit]:
        by_id: dict[str, SearchHit] = {}
        for hit in hits:
            by_id.setdefault(hit.chunk.id, hit)
        return list(by_id.values())

    first = unique_hits(initial)
    rounds = [first]
    frontier = first
    seen_ids = {hit.chunk.id for hit in first}
    used_references: set[str] = set()
    for _ in range(max_rounds - 1):
        selected_reference = None
        for hit in frontier:
            references = hit.chunk.metadata.get("references", ())
            if not isinstance(references, (list, tuple)):
                continue
            for reference in references:
                if not isinstance(reference, str):
                    continue
                reference = reference.strip()
                if reference and reference not in used_references:
                    selected_reference = reference
                    break
            if selected_reference is not None:
                break

        if selected_reference is None:
            break
        used_references.add(selected_reference)
        current = unique_hits(lookup(f"{query} {selected_reference}"))
        if not current:
            break

        rounds.append(current)
        frontier = [hit for hit in current if hit.chunk.id not in seen_ids]
        if not frontier:
            break
        seen_ids.update(hit.chunk.id for hit in frontier)

    if len(rounds) == 1:
        return first[:limit]

    rankings = [
        (f"round_{index}", 1.0, [(hit, hit.score) for hit in hits])
        for index, hits in enumerate(rounds, start=1)
    ]
    fused = fuse_rankings(rankings, limit, key=lambda hit: hit.chunk.id)
    return [replace(result.item, score=result.score, fusion_score=result.score) for result in fused]
