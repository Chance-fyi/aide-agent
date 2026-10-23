"""知识库文档元数据与切块的 Postgres 存储。

职责划分（与 Milvus 的分工）：
- Milvus：只存向量 + 子块正文 + chunk_id 引用——向量库擅长的是相似度检索
- Postgres：存文件路径、切分策略、父块全文——父子检索的"大文本"与
  文档管理元数据放关系库，检索命中后按 chunk_id 回查父块

表结构（三层，均以 BIGSERIAL 代理主键 id 为口径）：

    kb_document (文档)          kb_parent (父块/小节)        kb_chunk (子块)
    id, path, strategy   ──1:N──> id, doc_path, content,  ──1:N──> id, parent_id,
                                   h1, h2                          content

为什么父块单独成表，而不是在 kb_chunk 里存一份 parent_content：
- 一个小节通常切成多个子块，父块正文会被重复存 N 份（语料大时浪费明显）
- "哪些子块共享同一父块"靠文本比较判定既不可靠（不同章节可能有完全
  相同的段落）又昂贵；有了 parent_id 就是一次等值比较
- 父块正文变更（改语料重切）只影响父块表，子块行数不变

为什么父块不放 Milvus：父块不参与向量检索，只作为上下文返回；
关系库主键点查是毫秒级，而放进 Milvus 会为同一小节重复存 N 份向量字段。
"""

import logging

import psycopg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

def _get_conn() -> psycopg.Connection:
    s = get_settings()
    return psycopg.connect(s.postgres_dsn, autocommit=True)

def ensure_table() -> None:
    """兼容入口：执行数据库迁移（建表逻辑已迁到 app/core/migrations.py）。

    知识库表结构也在统一迁移里管理——它虽然是可重建的派生数据，
    但让所有 DDL 只有一处来源，才不会出现"某张表没人管"的情况。
    **不要再往这里加 DDL**。
    """
    from app.core import migrations

    migrations.run()

# ---- 切分策略管理 ----

def set_strategy(path: str, strategy: str | None) -> None:
    """设置/清除某文件的切分策略覆盖（strategy=None 恢复后缀默认）。

    幂等 UPSERT：文件还没入库也允许先配策略（下次重建即生效）。
    """
    if strategy is not None and strategy not in _all_strategies():
        raise ValueError(f"未知切分策略: {strategy}，可选: {sorted(_all_strategies())}")
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO kb_document (path, strategy) VALUES (%s, %s)
            ON CONFLICT (path) DO UPDATE SET strategy = %s, updated_at = now()
            """,
            (path, strategy, strategy),
        )

def get_strategies() -> dict[str, str]:
    """读全部策略覆盖（重建时一次拉全量，避免逐文件 N 次查询）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT path, strategy FROM kb_document WHERE strategy IS NOT NULL"
        ).fetchall()
    return {r[0]: r[1] for r in rows}

def _all_strategies() -> set[str]:
    """延迟导入取合法策略集合（避免 splitter ↔ kb_store 循环依赖）。"""
    from app.rag.splitter import _STRATEGIES
    return _STRATEGIES

# ---- 切块读写（重建流程用）----

def replace_chunks_transaction(conn: psycopg.Connection, files: list[tuple[str, str, list]]) -> dict:
    """在调用方的事务里全量替换切块，返回 doc_path → chunk_id 列表。

    事务由调用方（store.rebuild_index）开启：Postgres 写入与 Milvus 构建
    绑定在同一事务里，Milvus 构建失败时 Postgres 一起回滚，两侧永远一致。

    files 元素为 (doc_path, strategy, chunks)。
    父块去重依据是 Chunk.parent_key（切分器分配的"文件内小节序号"），
    不比较正文文本——同一 parent_key 的子块共用一条 kb_parent 记录。
    """
    conn.execute("DELETE FROM kb_chunk")
    conn.execute("DELETE FROM kb_parent")
    conn.execute("DELETE FROM kb_document")

    result: dict[str, list[int]] = {}
    for doc_path, strategy, chunks in files:
        conn.execute(
            "INSERT INTO kb_document (path, strategy) VALUES (%s, %s)",
            (doc_path, strategy),
        )
        # parent_key → kb_parent.id：同一小节的子块只插一次父块
        parent_ids: dict = {}
        chunk_ids: list[int] = []
        for c in chunks:
            parent_id = parent_ids.get(c.parent_key)
            if parent_id is None:
                parent_id = conn.execute(
                    """
                    INSERT INTO kb_parent (doc_path, content, h1, h2)
                    VALUES (%s, %s, %s, %s) RETURNING id
                    """,
                    (doc_path, c.parent_content, c.h1, c.h2),
                ).fetchone()[0]
                parent_ids[c.parent_key] = parent_id
            chunk_id = conn.execute(
                "INSERT INTO kb_chunk (parent_id, content) VALUES (%s, %s) RETURNING id",
                (parent_id, c.content),
            ).fetchone()[0]
            chunk_ids.append(chunk_id)
        result[doc_path] = chunk_ids
    return result

def get_parents(chunk_ids: list[int]) -> dict[int, dict]:
    """按 Milvus 返回的 chunk_id 批量回查父块（含标题元数据）。

    返回 {chunk_id: {"parent_id", "parent_content", "source", "h1", "h2"}}。
    一次 JOIN 取齐，避免每命中一条查两次库。查不到的 id 不在结果里，
    调用方回退用子块正文当父块（防御 Milvus/Postgres 版本短暂不一致）。
    """
    if not chunk_ids:
        return {}
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, p.id, p.content, p.doc_path, p.h1, p.h2
            FROM kb_chunk c
            JOIN kb_parent p ON p.id = c.parent_id
            WHERE c.id = ANY(%s)
            """,
            (chunk_ids,),
        ).fetchall()
    return {
        r[0]: {
            "parent_id": r[1],
            "parent_content": r[2],
            "source": r[3],
            "h1": r[4],
            "h2": r[5],
        }
        for r in rows
    }

def stats() -> dict:
    """知识库 Postgres 侧统计（调试接口用）。"""
    with _get_conn() as conn:
        docs = conn.execute("SELECT count(*) FROM kb_document").fetchone()[0]
        parents = conn.execute("SELECT count(*) FROM kb_parent").fetchone()[0]
        chunks = conn.execute("SELECT count(*) FROM kb_chunk").fetchone()[0]
    return {"documents": docs, "parents": parents, "chunks": chunks}

def file_stats() -> list[dict]:
    """按文件统计块数（知识库管理界面用）。

    一次 JOIN 聚合出每个文件的父块/子块数量，避免前端逐文件请求。
    LEFT JOIN 保证「已入库但没有块」的文件也出现（重建失败时会这样）。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.path, d.strategy, d.updated_at,
                   count(DISTINCT p.id) AS parents,
                   count(c.id) AS chunks
            FROM kb_document d
            LEFT JOIN kb_parent p ON p.doc_path = d.path
            LEFT JOIN kb_chunk  c ON c.parent_id = p.id
            GROUP BY d.path, d.strategy, d.updated_at
            ORDER BY d.path
            """
        ).fetchall()
    return [
        {
            "path": r[0],
            "strategy": r[1],
            "indexed_at": r[2].isoformat(),
            "parents": r[3],
            "chunks": r[4],
        }
        for r in rows
    ]

def recent_chunks(path: str, limit: int = 3) -> list[dict]:
    """取某文件的若干切块预览（管理界面「看看切得对不对」用）。

    按 id 升序取前几条：文件内顺序即原文顺序，看开头就能判断切分是否合理。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.content, p.h1, p.h2
            FROM kb_chunk c
            JOIN kb_parent p ON p.id = c.parent_id
            WHERE p.doc_path = %s
            ORDER BY c.id ASC
            LIMIT %s
            """,
            (path, limit),
        ).fetchall()
    return [
        {"id": r[0], "content": r[1], "h1": r[2], "h2": r[3]}
        for r in rows
    ]
