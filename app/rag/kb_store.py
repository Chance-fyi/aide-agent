"""知识库文档元数据与切块的 Postgres 存储。

职责划分（与 Milvus 的分工）：
- Milvus：只存向量 + 子块正文 + chunk_id 引用——向量库擅长的是相似度检索
- Postgres：存文件路径、切分策略、父块全文——父子检索的"大文本"与
  文档管理元数据放关系库，检索命中后按 chunk_id 回查父块

为什么父块不放 Milvus：父块是小节全文（最长 1500 字），与子块内容大量
重复，存两份既浪费内存又要在重建时维护一致性；按主键回查 Postgres
是毫秒级操作，代价可忽略。

按文件配置切分策略：不同文档结构不同（md 有标题、pdf 是纯文本流），
全局统一策略效果差。策略存 kb_document 表（持久化，重建时生效），
无记录的文件按后缀选默认策略。
"""

import logging

import psycopg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

def _get_conn() -> psycopg.Connection:
    s = get_settings()
    return psycopg.connect(s.postgres_dsn, autocommit=True)

def ensure_table() -> None:
    """幂等建表：文档元数据 + 切块内容两张表，doc_path 外键关联。

    约定：所有表一律用 BIGSERIAL 代理主键 id（与 app_user/chat_message
    同一口径），业务列另加唯一约束——path 可变（文件重命名）时其他表
    的引用不致悬空。schema 与代码不一致时整表重建（开发期无存量负担）。
    """
    with _get_conn() as conn:
        try:
            conn.execute("SELECT id, path, strategy FROM kb_document LIMIT 1")
        except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable):
            conn.execute("DROP TABLE IF EXISTS kb_chunk")
            conn.execute("DROP TABLE IF EXISTS kb_document")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_document (
                id         BIGSERIAL PRIMARY KEY,   -- 代理主键：项目统一口径
                path       TEXT NOT NULL UNIQUE,    -- 文件路径（相对语料目录，如 returns.md）
                strategy   TEXT,                    -- 切分策略覆盖；NULL = 按后缀默认
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_chunk (
                id             BIGSERIAL PRIMARY KEY,
                doc_path       TEXT NOT NULL REFERENCES kb_document(path) ON DELETE CASCADE,
                content        TEXT NOT NULL,   -- 子块（检索单元）
                parent_content TEXT NOT NULL,   -- 父块（返回给 LLM 的小节全文）
                h1             TEXT NOT NULL DEFAULT '',
                h2             TEXT NOT NULL DEFAULT ''
            )
        """)

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
    """读全部策略覆盖（重建时逐文件查的口径，一次拉全量避免 N 次查询）。"""
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
    """在调用方的事务里全量替换切块，返回 doc_path → chunk_id 列表的映射。

    事务由调用方（store.rebuild_index）开启：Postgres 写入与 Milvus 构建
    绑定在同一事务里，Milvus 构建失败时 Postgres 一起回滚，两侧永远一致。
    files 元素为 (doc_path, strategy, chunks)；chunks 为 splitter.Chunk 列表。
    """
    result: dict = {}
    conn.execute("DELETE FROM kb_chunk")
    conn.execute("DELETE FROM kb_document")
    for doc_path, strategy, chunks in files:
        conn.execute(
            "INSERT INTO kb_document (path, strategy) VALUES (%s, %s)",
            (doc_path, strategy),
        )
        ids: list[int] = []
        for c in chunks:
            row = conn.execute(
                """
                INSERT INTO kb_chunk (doc_path, content, parent_content, h1, h2)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
                """,
                (doc_path, c.content, c.parent_content, c.h1, c.h2),
            ).fetchone()
            ids.append(row[0])
        result[doc_path] = ids
    return result

def get_parents(chunk_ids: list[int]) -> dict[int, str]:
    """按 Milvus 返回的 chunk_id 批量回查父块全文。

    检索命中（通常 top_k ≤ 10）后的主键点查，毫秒级。
    返回 {chunk_id: parent_content}；查不到的 id 不在结果里，
    调用方回退用子块正文当父块（防御 Milvus/Postgres 版本短暂不一致）。
    """
    if not chunk_ids:
        return {}
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, parent_content FROM kb_chunk WHERE id = ANY(%s)",
            (chunk_ids,),
        ).fetchall()
    return {r[0]: r[1] for r in rows}

def stats() -> dict:
    """知识库 Postgres 侧统计（调试接口用）。"""
    with _get_conn() as conn:
        docs = conn.execute("SELECT count(*) FROM kb_document").fetchone()[0]
        chunks = conn.execute("SELECT count(*) FROM kb_chunk").fetchone()[0]
    return {"documents": docs, "chunks": chunks}
