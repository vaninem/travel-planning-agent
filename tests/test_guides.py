import hashlib
from types import SimpleNamespace

import pytest

from travel_agent import guides
from travel_agent.domain import ProviderError


@pytest.fixture(autouse=True)
def fresh_guide_index(monkeypatch):
    monkeypatch.delenv("TRAVEL_GUIDE_DIR", raising=False)
    monkeypatch.setenv("TRAVEL_GUIDE_RETRIEVAL", "lexical")
    guides._index.cache_clear()
    yield
    guides._index.cache_clear()


def test_bundled_guides_return_ranked_sources_and_line_numbers():
    hits = guides.search_guides("南京 博物馆 历史街区", limit=2)
    assert len(hits) == 2
    assert hits[0]["source"] == "urban-trips.md"
    assert hits[0]["heading"] == "城市旅行建议 > 南京和西安 | 城市历史"
    assert (hits[0]["start_line"], hits[0]["end_line"]) == (17, 17)
    assert "南京博物院" in hits[0]["content"]
    assert guides.search_guides("无匹配XYZ999") == []


def test_custom_guide_directory_preserves_original_markdown_location(tmp_path, monkeypatch):
    (tmp_path / "hangzhou.md").write_text(
        "# 浙江\n\n## 杭州\n\n游览西湖时留出步行时间。\n", encoding="utf-8"
    )
    (tmp_path / "other.md").write_text("# 上海\n\n推荐外滩。\n", encoding="utf-8")
    monkeypatch.setenv("TRAVEL_GUIDE_DIR", str(tmp_path))

    assert guides.search_guides("西湖", limit=1) == [{
        "content": "游览西湖时留出步行时间。",
        "source": "hangzhou.md",
        "heading": "浙江 > 杭州",
        "start_line": 5,
        "end_line": 5,
    }]


def test_empty_guide_directory_reports_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVEL_GUIDE_DIR", str(tmp_path))
    with pytest.raises(ProviderError) as failure:
        guides.search_guides("上海")
    assert failure.value.code == "rag_unavailable"


def test_vector_and_hybrid_use_existing_recall_and_rrf(tmp_path, monkeypatch):
    pytest.importorskip("psycopg")
    pytest.importorskip("pgvector")
    from travel_agent.rag_engine.embeddings.types import EmbeddingHit, IndexedChunk, IndexStatus, SyncReport

    (tmp_path / "a.md").write_text("# 景点\n\n西湖适合观景。", encoding="utf-8")
    (tmp_path / "b.md").write_text("# 推荐\n\n断桥适合漫步。", encoding="utf-8")
    monkeypatch.setenv("TRAVEL_GUIDE_DIR", str(tmp_path))
    embedding_queries = []

    class FakeIndex:
        profile_id = "test-profile"

        def __init__(self):
            self.chunks = []
            self.client = SimpleNamespace(embed=self.embed)
            self.store = self

        def sync_source(self, namespace, source, chunks):
            self.chunks.extend(IndexedChunk(
                self.profile_id, namespace, source, chunk.id, chunk.text,
                hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(), chunk.metadata, "ready",
            ) for chunk in chunks)
            return SyncReport(created=len(chunks))

        def status(self, namespace):
            return IndexStatus(ready=len(self.chunks))

        prepare_namespace = status

        def embed(self, texts):
            embedding_queries.extend(texts)
            return [[1.0, 0.0]]

        def search_ready(self, profile_id, namespace, vector, limit, source_id=None):
            chunks = sorted(self.chunks, key=lambda item: item.source_id, reverse=True)
            return [EmbeddingHit(item, 1.0 - i * 0.1) for i, item in enumerate(chunks[:limit])]

    monkeypatch.setattr(guides, "_new_embedding_index", FakeIndex)
    monkeypatch.setenv("TRAVEL_GUIDE_RETRIEVAL", "hybrid")
    index = guides.prepare_guides()
    assert index.search_options.mode == "hybrid"
    assert embedding_queries == []  # 查询前完成准备，测试替身不需要生成文档向量。
    assert [hit["source"] for hit in guides.search_guides("西湖", mode="vector")] == ["b.md", "a.md"]
    assert [hit["source"] for hit in guides.search_guides("西湖")] == ["a.md", "b.md"]
    assert embedding_queries == ["西湖", "西湖"]


def test_embedding_unavailable_preserves_local_guide_retrieval(monkeypatch):
    monkeypatch.setenv("TRAVEL_GUIDE_RETRIEVAL", "hybrid")

    def unavailable():
        raise RuntimeError("unavailable")

    monkeypatch.setattr(guides, "_new_embedding_index", unavailable)
    assert guides.prepare_guides().search_options.mode == "lexical"
    assert guides.search_guides("南京 博物馆 历史街区", limit=1)[0]["source"] == "urban-trips.md"
