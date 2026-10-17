"""检索层测试。

需要 Milvus 可用（podman compose up -d）+ 本地 BGE 模型已缓存。
知识库已入库（步骤 5 已执行 rebuild），这里只读不写。
"""

import pytest

from app.rag import store


@pytest.fixture(scope="module", autouse=True)
def require_collection():
    """前置：线上别名必须可用且 schema 为最新。

    - 别名不存在：没入库，直接失败提示
    - 集合缺 parent_content 字段（v1 旧 schema）：自动触发蓝绿重建升级，
      旧数据无法平滑加字段，重建是唯一正道（step-15 的别名机制保证不丟线上可用性）
    """
    from pymilvus import MilvusClient

    from app.core.config import get_settings

    client = MilvusClient(uri=get_settings().milvus_uri)
    resolved = store._resolve_collection(client, store.alias_name())
    if resolved is None:
        pytest.fail(
            "知识库别名未指向任何集合，请先入库："
            "store.rebuild_index('data/knowledge_base') 或 POST /debug/kb/rebuild"
        )
    fields = {f["name"] for f in client.describe_collection(resolved)["fields"]}
    if "parent_content" not in fields:
        store.rebuild_index("data/knowledge_base")


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
    """检索结果包含完整字段（content/parent_content/source/h1/h2/score）。"""
    hit = store.search("会员积分", top_k=1)[0]
    assert set(hit.keys()) == {"score", "content", "parent_content", "source", "h1", "h2"}
    assert isinstance(hit["score"], float)
    assert hit["content"]
    assert hit["parent_content"], "父子检索：父块不应为空"

def test_parent_child_retrieval_returns_context():
    """父子检索：父块应包含子块之外的小节上下文（或自为父块）。"""
    hit = store.search("退货条件", top_k=1)[0]
    # 父块至少不短于子块：短小节父块=全文，超长小节自为父块
    assert len(hit["parent_content"]) >= len(hit["content"])
    assert hit["content"] in hit["parent_content"] or hit["parent_content"] == hit["content"]


def test_search_min_score_filters_irrelevant():
    """阈值过滤：提高 min_score 应滤掉低分命中；置 0 则不过滤。"""
    # 正常阈值下退货查询应有命中
    hits = store.search("怎么退货", top_k=3)
    assert hits, "退货查询在默认阈值下不应为空"
    # 阈值拉到 0.99：任何真实相似度都达不到，应返回空列表而不是报错
    assert store.search("怎么退货", top_k=3, min_score=0.99) == []
    # 显式置 0：不过滤，条数应不少于默认阈值的结果
    assert len(store.search("怎么退货", top_k=3, min_score=0)) >= len(hits)

def test_search_empty_collection_raises():
    """集合不存在时显式报错（区分"没建库"与"没检索到"）。"""
    from pymilvus import MilvusClient

    from app.core.config import get_settings

    client = MilvusClient(uri=get_settings().milvus_uri)
    if client.has_collection("nonexistent_test_col"):
        client.drop_collection("nonexistent_test_col")
    with pytest.raises(RuntimeError, match="集合不存在"):
        store.search("test", top_k=1, collection_name="nonexistent_test_col")

# ---------- step-15：版本化集合与蓝绿重建 ----------

def _clear_settings_cache():
    from app.core.config import get_settings

    get_settings.cache_clear()

def test_target_name_stable_and_model_sensitive(monkeypatch):
    """版本集合名：同配置稳定；换 embedding 模型必须得到不同集合（向量空间隔离）。"""
    name1 = store._target_collection_name()
    assert name1 == store._target_collection_name()  # 纯函数，同配置必稳定

    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-base-zh-v1.5")
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    _clear_settings_cache()
    try:
        name2 = store._target_collection_name()
        assert name2 != name1, "换模型必须指向新集合，否则新旧向量混在一个空间"
    finally:
        monkeypatch.undo()
        _clear_settings_cache()

def test_rebuild_switches_alias_and_cleans_old():
    """蓝绿重建：别名应指向新版本集合，历史版本被清理，检索照常可用。"""
    from pymilvus import MilvusClient

    from app.core.config import get_settings

    count = store.rebuild_index("data/knowledge_base")
    assert count > 0

    s = get_settings()
    client = MilvusClient(uri=s.milvus_uri)
    target = store._target_collection_name()
    # 别名指向目标版本
    assert store._resolve_collection(client, s.kb_alias) == target
    # 无残留 staging、无旧版本
    names = client.list_collections()
    assert names == [target]
    # 切换后检索照常工作
    hits = store.search("怎么退货", top_k=3)
    assert hits

def test_rebuild_failure_keeps_old_version(monkeypatch):
    """重建中途翻车：旧版本必须原地可用，且 staging 被清理干净。"""
    from pymilvus import MilvusClient

    from app.core.config import get_settings

    # 先确保有稳定的旧版本（上一步刚重建过）
    alias = store.alias_name()
    before = MilvusClient(uri=get_settings().milvus_uri)
    old_target = store._resolve_collection(before, alias)

    def boom(self, *args, **kwargs):
        raise RuntimeError("模拟写入故障")

    monkeypatch.setattr(MilvusClient, "insert", boom)
    with pytest.raises(RuntimeError, match="旧版本未受影响"):
        store.rebuild_index("data/knowledge_base")

    monkeypatch.undo()
    after = MilvusClient(uri=get_settings().milvus_uri)
    # 别名仍指向旧版本，检索正常
    assert store._resolve_collection(after, alias) == old_target
    assert store.search("怎么退货", top_k=1)
    # staging 集合已清理
    assert not any(n.endswith("_building") for n in after.list_collections())
