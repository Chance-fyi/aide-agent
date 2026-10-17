"""检索层测试。

需要 Milvus 可用（podman compose up -d）+ 本地 BGE 模型已缓存。
知识库已入库（步骤 5 已执行 rebuild），这里只读不写。
"""

import pytest

from app.rag import store


@pytest.fixture(scope="module", autouse=True)
def require_collection():
    """前置：集合必须存在（没入库的检索测试没有意义，直接失败提示）。"""
    from pymilvus import MilvusClient

    from app.core.config import get_settings

    client = MilvusClient(uri=get_settings().milvus_uri)
    if not client.has_collection(store.COLLECTION_NAME):
        pytest.fail("kb_chunks 集合不存在，请先入库：store.rebuild_index('data/knowledge_base')")


def test_search_returns_relevant_result():
    """退货相关查询应命中退货文档（语义检索正确性的冒烟用例）。"""
    hits = store.search("怎么退货", top_k=3)
    assert len(hits) == 3
    top = hits[0]
    assert "returns.md" in top["source"]
    assert top["score"] > 0.5  # 归一化余弦相似度，语义相关应显著大于 0.5


def test_search_scores_are_sorted():
    """结果应按相似度降序。"""
    hits = store.search("退款多久到账", top_k=3)
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_search_result_schema():
    """检索结果包含完整字段（content/source/h1/h2/score）。"""
    hit = store.search("会员积分", top_k=1)[0]
    assert set(hit.keys()) == {"score", "content", "source", "h1", "h2"}
    assert isinstance(hit["score"], float)
    assert hit["content"]


def test_search_empty_collection_raises():
    """集合不存在时显式报错（区分"没建库"与"没检索到"）。"""
    from pymilvus import MilvusClient

    from app.core.config import get_settings

    client = MilvusClient(uri=get_settings().milvus_uri)
    if client.has_collection("nonexistent_test_col"):
        client.drop_collection("nonexistent_test_col")
    with pytest.raises(RuntimeError, match="集合不存在"):
        store.search("test", top_k=1, collection_name="nonexistent_test_col")
