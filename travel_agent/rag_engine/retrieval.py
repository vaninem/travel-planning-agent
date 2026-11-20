"""Memory 与 RAG 共用的中英文分词、BM25 和 RRF。"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
TERMS = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """保留 ASCII 标识符，中文使用相邻双字。"""
    tokens = []
    for match in TERMS.finditer(text):
        segment = match.group()
        if segment.isascii():
            tokens.append(segment.lower())
        elif len(segment) == 1:
            tokens.append(segment)
        else:
            tokens.extend(segment[i:i + 2] for i in range(len(segment) - 1))
    return tokens


def bm25_rank(
    query: str, candidates: list[T], limit: int, *, key: Callable[[T], str],
    text: Callable[[T], str], k1: float = 1.5, b: float = 0.75,
    split: Callable[[str], list[str]] = tokenize,
) -> list[tuple[T, float]]:
    """按 BM25 分数排序，保留原候选及其身份。"""
    if type(limit) is not int or limit < 0:
        raise ValueError("limit 必须是非负整数")
    if not candidates or limit == 0:
        return []
    query_tokens = list(dict.fromkeys(split(query)))
    if not query_tokens:
        return []
    df = dict.fromkeys(query_tokens, 0)
    matching_documents = []
    total_length = 0
    for candidate in candidates:
        content_tokens = split(text(candidate))
        length = len(content_tokens)
        total_length += length
        counts = Counter(content_tokens)
        matched = {token: counts[token] for token in query_tokens if counts[token]}
        for token in matched:
            df[token] += 1
        if matched:
            matching_documents.append((candidate, length, matched))
    average_length = total_length / len(candidates)
    idf = {
        token: math.log1p((len(candidates) - frequency + 0.5) / (frequency + 0.5))
        for token, frequency in df.items() if frequency
    }
    hits = []
    for candidate, length, matched in matching_documents:
        norm = k1 * (1 - b + b * length / average_length)
        score = 0.0
        for token, frequency in matched.items():
            score += idf[token] * frequency * (k1 + 1) / (frequency + norm)
        if score > 0:
            hits.append((candidate, score))
    hits.sort(key=lambda hit: (-hit[1], key(hit[0])))
    return hits[:limit]


@dataclass
class FusedRank(Generic[T]):
    item: T
    score: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    ranks: dict[str, int] = field(default_factory=dict)


def fuse_rankings(
    routes: list[tuple[str, float, list[tuple[T, float]]]], limit: int,
    *, key: Callable[[T], str], rrf_k: int = 60,
) -> list[FusedRank[T]]:
    """按身份合并各路名次，分别保留原始分数和融合分数。"""
    if type(limit) is not int or limit < 0:
        raise ValueError("limit 必须是非负整数")
    if limit == 0:
        return []
    by_id: dict[str, FusedRank[T]] = {}
    for route, weight, hits in routes:
        for rank, (item, score) in enumerate(hits, start=1):
            identity = key(item)
            fused = by_id.get(identity)
            if fused is None:
                fused = by_id[identity] = FusedRank(item)
            if route in fused.ranks:
                continue
            fused.ranks[route] = rank
            fused.scores[route] = score
            fused.score += weight / (rrf_k + rank)
    return sorted(by_id.values(), key=lambda hit: (-hit.score, key(hit.item)))[:limit]
