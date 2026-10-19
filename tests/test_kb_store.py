"""kb_store（Postgres 侧知识库存储）单元测试。

需要 Postgres 可用（podman compose up -d）。
测试自建临时文档记录，结束后清理，不污染重建生成的正式数据。
"""

import pytest

from app.rag import kb_store
from app.rag.splitter import Chunk


@pytest.fixture(autouse=True)
def _ensure_and_cleanup():
    kb_store.ensure_table()
    yield
    # 清理测试文档（CASCADE 连带删 chunks）
    import psycopg

    from app.core.config import get_settings
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM kb_document WHERE path LIKE 'test_%'")

def _fake_chunks(n: int = 2) -> list[Chunk]:
    return [
        Chunk(content=f"子块{i}", source="test_a.md", h1="标题", h2="", parent_content=f"父块{i}全文")
        for i in range(n)
    ]

def test_set_and_get_strategy():
    """策略覆盖的写入/读取/清除。"""
    kb_store.set_strategy("test_a.md", "fixed")
    assert kb_store.get_strategies().get("test_a.md") == "fixed"
    kb_store.set_strategy("test_a.md", None)   # 清除 = 回退后缀默认
    assert kb_store.get_strategies().get("test_a.md") is None

def test_set_invalid_strategy_rejected():
    with pytest.raises(ValueError, match="未知切分策略"):
        kb_store.set_strategy("test_a.md", "magic")

def test_replace_chunks_and_get_parents():
    """事务内全量替换切块后，能按 chunk_id 回查父块。"""
    import psycopg

    from app.core.config import get_settings
    with psycopg.connect(get_settings().postgres_dsn) as conn, conn.transaction():
        ids = kb_store.replace_chunks_transaction(
            conn, [("test_a.md", "header", _fake_chunks(3))]
        )
    assert len(ids["test_a.md"]) == 3

    parents = kb_store.get_parents(ids["test_a.md"])
    assert len(parents) == 3
    assert all(p.startswith("父块") for p in parents.values())
    # 查不存在的 id：返回空 dict（调用方回退用子块正文）
    assert kb_store.get_parents([10**9]) == {}

def test_replace_is_full_refresh():
    """再次替换时旧块全部清除（重建语义，不留陈旧数据）。"""
    import psycopg

    from app.core.config import get_settings
    with psycopg.connect(get_settings().postgres_dsn) as conn:
        with conn.transaction():
            first = kb_store.replace_chunks_transaction(conn, [("test_a.md", "header", _fake_chunks(3))])
        with conn.transaction():
            second = kb_store.replace_chunks_transaction(conn, [("test_a.md", "header", _fake_chunks(1))])
    assert len(first["test_a.md"]) == 3 and len(second["test_a.md"]) == 1
    # 旧 id 已不存在
    assert kb_store.get_parents(first["test_a.md"]) == {}
    assert len(kb_store.get_parents(second["test_a.md"])) == 1

def test_stats():
    kb_store.stats()  # 不抛错即正常
