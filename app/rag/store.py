"""Milvus 向量库管理（版本化集合 + 蓝绿重建）。

职责：
- ensure_collection(): 集合不存在则创建（幂等），带固定的索引参数
- rebuild_index(): 蓝绿重建——新集合构建+冒烟测试通过后才切别名，
  失败则旧版本继续服务，永不出现"重建到一半检索不可用"
- search(): 相似度检索（走别名，调用方无感知版本切换）

设计决策：
- **版本化集合**：集合名含 schema 版本与 embedding 模型指纹，
  别名（kb_alias）永远指向当前线上版本。
  - 切分逻辑/表结构变化 → 手动 bump kb_schema_version
  - 换 embedding 模型 → 模型名进指纹，重建自动生成新集合
    （注意同步改 embedding_dim，维度不同无法共用集合）
  旧版本在切换成功后清理，失败时天然保留（回滚 = 不切别名）
- 用 pymilvus 原生 SDK 而不是 langchain-milvus 封装——
  schema 显式可控，别名/重建这类运维操作也更直接
- metric 用 IP（内积）：BGE 归一化向量下内积即余弦相似度（见 embedder.py）
"""

import hashlib
import logging
from pathlib import Path

from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient
from pymilvus.exceptions import MilvusException

from app.core.config import get_settings
from app.rag import kb_store
from app.rag.embedder import embed_query, embed_texts

logger = logging.getLogger(__name__)


def alias_name() -> str:
    """线上检索别名（调用方永远只认别名，不直接碰版本化集合名）。"""
    return get_settings().kb_alias

def _target_collection_name() -> str:
    """当前配置对应的版本化集合名。

    指纹 = sha1(模型名 | 维度 | schema 版本)：
    - 换 embedding 模型 → 新指纹 → 新集合（向量空间不同，必须隔离）
    - 切分逻辑/字段变化 → bump kb_schema_version → 新集合
    """
    s = get_settings()
    fingerprint = hashlib.sha1(
        f"{s.embedding_model}|{s.embedding_dim}|v{s.kb_schema_version}".encode()
    ).hexdigest()[:8]
    return f"{s.kb_alias}_v{s.kb_schema_version}_{fingerprint}"

def _get_client() -> MilvusClient:
    """创建 Milvus 客户端（每次短连接，量小开销可忽略；长连接池在应用层再加）。"""
    return MilvusClient(uri=get_settings().milvus_uri)

def ensure_collection(client: MilvusClient, name: str) -> None:
    """幂等创建集合：存在则跳过，不存在则建集合 + 索引。

    索引参数 AUTOINDEX：Milvus standalone 下自动选择最合适的索引类型，
    开发环境不用手调 IVF/HNSW 参数。
    """
    if client.has_collection(name):
        return

    s = get_settings()
    schema = CollectionSchema(
        fields=[
            # 自增主键：Milvus 要求必须有主键
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            # 向量字段：维度由配置决定（换 embedding 模型必须同步改 EMBEDDING_DIM）
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=s.embedding_dim),
            # 正文：检索命中后要返回给 LLM/前端
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=2048),
            # Postgres kb_chunk 表的主键引用：父块不存 Milvus（与小节正文大量
            # 重复、浪费且难同步），检索命中后凭此 id 回查 Postgres 取父块
            FieldSchema(name="chunk_id", dtype=DataType.INT64),
            # 元数据：溯源与展示用。VARCHAR 必须声明 max_length
            FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="h1", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="h2", dtype=DataType.VARCHAR, max_length=128),
        ],
        description=f"客服知识库切块 (schema v{s.kb_schema_version})",
    )

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_type="AUTOINDEX",
        metric_type="IP",  # 归一化向量的内积 = 余弦相似度
    )

    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    logger.info("Milvus 集合已创建: %s", name)

def _resolve_collection(client: MilvusClient, alias: str) -> str | None:
    """别名 → 实际集合名；别名不存在（未入库）返回 None。"""
    try:
        desc = client.describe_alias(alias)
        return desc.get("collection_name")
    except MilvusException:
        # describe_alias 对不存在的别名抛 MilvusException
        return None

def _smoke_test(client: MilvusClient, name: str) -> None:
    """重建后的冒烟测试：预设业务查询必须命中且分数达标，否则判定新数据不可用。

    这是蓝绿切换的守门员：语料损坏/模型维度配错/切分逻辑翻车，
    都会在这一步被拦下，旧版本继续服务。
    """
    s = get_settings()
    queries = [q.strip() for q in s.rebuild_smoke_queries.split(",") if q.strip()]
    for q in queries:
        results = client.search(
            collection_name=name,
            data=[embed_query(q)],
            limit=1,
            output_fields=["source"],
            search_params={"metric_type": "IP"},
        )
        hits = results[0]
        if not hits:
            raise RuntimeError(f"冒烟测试失败：查询 {q!r} 无命中")
        top_score = float(hits[0]["distance"])
        if top_score < s.retrieval_min_score:
            raise RuntimeError(
                f"冒烟测试失败：查询 {q!r} 最高分 {top_score:.3f} 低于阈值 {s.retrieval_min_score}"
            )
    logger.info("冒烟测试通过: %d 条查询", len(queries))

def rebuild_index(kb_dir: str) -> int:
    """蓝绿全量重建：staging 集合构建 → 冒烟测试 → 切别名 → 清理旧版本。

    流程（检索可用性全程不受影响，直到最后一步原子切换）：
      1. 读语料：逐文件按「kb_document 覆盖策略 > 后缀默认」切块
      2. Postgres 事务内写入文档元数据与切块（拿到 chunk_id），
         同事务内建 staging 集合并写入 Milvus、冒烟测试——
         Milvus 失败则 Postgres 一起回滚，两侧永远一致
      3. 切别名指向新集合，重命名去掉 _building 后缀
      4. 清理其余旧版本集合
    任一步失败：删 staging、事务回滚，旧版本原样在线。

    返回插入的块数。
    """
    from app.rag.splitter import _default_strategy, split_file

    s = get_settings()
    client = _get_client()
    target = _target_collection_name()
    staging = f"{target}_building"
    alias = alias_name()

    # 上次重建中断留下的 staging 一律作废重建
    if client.has_collection(staging):
        client.drop_collection(staging)

    # 策略解析（事务外完成）：按文件覆盖 > 后缀默认；日志留痕实际用的策略
    overrides = kb_store.get_strategies()
    per_file: list[tuple[str, str, list]] = []
    all_chunks = []
    for f in sorted(Path(kb_dir).iterdir()):
        if not f.is_file():
            continue
        strategy = overrides.get(f.name) or _default_strategy(f.suffix.lower())
        chunks = split_file(f, strategy)
        if chunks:
            logger.info("切块: %s strategy=%s chunks=%d", f.name, strategy, len(chunks))
        all_chunks.extend(chunks)
        per_file.append((f.name, strategy, chunks))
    if not all_chunks:
        raise RuntimeError("知识库语料为空，拒绝重建（请检查语料目录）")

    # 批量向量化（一次调用比逐条快很多，BGE 内部会分批跑）
    vectors = embed_texts([c.content for c in all_chunks])

    try:
        ensure_collection(client, staging)
        # Postgres 与 Milvus 绑在同一事务：chunk_id 先在 Postgres 生成，
        # Milvus 行只存引用；任一侧失败整体回滚，避免两侧不一致
        with kb_store._get_conn() as conn, conn.transaction():
            doc_chunk_ids = kb_store.replace_chunks_transaction(conn, per_file)
            rows = []
            idx = 0
            for doc_path, chunks in ((p, c) for p, _, c in per_file):
                for c, chunk_id in zip(chunks, doc_chunk_ids[doc_path], strict=True):
                    rows.append({
                        "embedding": vectors[idx],
                        "content": c.content,
                        "chunk_id": chunk_id,
                        "source": c.source,
                        "h1": c.h1,
                        "h2": c.h2,
                    })
                    idx += 1
            client.insert(collection_name=staging, data=rows)
            # flush：把 growing segment 落盘为 sealed segment。
            # 不 flush 的话统计与检索可能看不到数据（Milvus 最终一致性），必须显式调用
            client.flush(staging)
            _smoke_test(client, staging)
    except Exception as exc:
        # 蓝绿核心承诺：新版本有任何问题，旧版本继续服务
        client.drop_collection(staging)
        raise RuntimeError(f"知识库重建失败（旧版本未受影响）：{exc}") from exc

    # ---- 切换窗口：先清旧集合（别名不能与现存集合同名），最后建别名 ----
    current = _resolve_collection(client, alias)
    if client.has_collection(alias) or current:
        try:
            client.drop_alias(alias)
        except MilvusException:
            # 别名不存在时抛 MilvusException，属预期路径，无需处理
            logger.debug("drop_alias 跳过（别名 %s 不存在或已清理）", alias)
    if client.has_collection(target):
        # 同版本重复重建：先清掉旧 target（此刻已无别名指向它）
        client.drop_collection(target)
    client.rename_collection(staging, target)

    # 清理其他历史版本集合，只保留当前线上版本
    prefix = f"{s.kb_alias}_"
    for name in client.list_collections():
        if name != target and name.startswith(prefix):
            client.drop_collection(name)
            logger.info("已清理旧版本集合: %s", name)

    # 别名创建 = 上线时刻：此后所有检索走新版本
    client.create_alias(collection_name=target, alias=alias)

    logger.info("知识库蓝绿重建完成: %s (%d 块)", target, len(rows))
    return len(rows)

def _attach_parents(hits: list[dict]) -> list[dict]:
    """批量回查父块，拼回检索结果（父子检索的 Postgres 半场）。

    chunk_id 查不到时回退用子块正文当父块：只在 Milvus/Postgres 版本
    短暂不一致（如重建事务已提交但别名未切）时发生，降级优于报错。
    """
    parents = kb_store.get_parents([h["chunk_id"] for h in hits])
    for h in hits:
        h["parent_content"] = parents.get(h["chunk_id"]) or h["content"]
    # chunk_id 是纯实现细节，不对外暴露
    return [{k: v for k, v in h.items() if k != "chunk_id"} for h in hits]

def search(
    query: str,
    top_k: int | None = None,
    collection_name: str | None = None,
    min_score: float | None = None,
) -> list[dict]:
    """语义检索：返回 top_k 个相关块（含相似度分数与元数据）。

    collection_name 默认走别名（生产口径）；显式传集合名便于测试注入
    不存在的集合名（异常路径测试）。
    min_score：相似度阈值过滤。None 时读全局配置——低于阈值的命中视为"不相关"，
    直接丢弃而不是塞给 LLM。这是缓解幻觉的第一道闸：宁可"没检到"，不可"检错了"。
    """
    s = get_settings()
    top_k = top_k or s.retrieval_top_k
    # 显式传 0 表示不过滤（调试场景想看全量分数分布）
    threshold = s.retrieval_min_score if min_score is None else min_score

    client = _get_client()
    name = collection_name or alias_name()
    resolved = _resolve_collection(client, name)
    if resolved is None:
        # 集合/别名都不存在：说明知识库还没入库——显式报错而不是静默返回空，
        # 让调用方能区分"没建库"和"没检索到"
        raise RuntimeError("知识库集合不存在，请先执行入库（POST /debug/kb/rebuild）")

    results = client.search(
        collection_name=resolved,
        data=[embed_query(query)],
        limit=top_k,
        output_fields=["content", "chunk_id", "source", "h1", "h2"],
        # 归一化向量 + IP 度量下，score 就是余弦相似度
        search_params={"metric_type": "IP"},
    )

    hits = results[0]  # 只有一个查询，取第一个结果集
    out = [
        {
            "score": float(hit["distance"]),
            "content": hit["entity"]["content"],
            "chunk_id": hit["entity"]["chunk_id"],
            "source": hit["entity"]["source"],
            "h1": hit["entity"]["h1"],
            "h2": hit["entity"]["h2"],
        }
        for hit in hits
    ]
    # 低分过滤放在父块回查前：被过滤的命中不值得再查一次 Postgres
    out = [h for h in out if h["score"] >= threshold]
    return _attach_parents(out)
