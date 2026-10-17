"""Milvus 向量库管理。

职责：
- ensure_collection(): 集合不存在则创建（幂等），带固定的索引参数
- upsert_chunks(): 全量重建知识库数据（删除旧数据再插入）
- search(): 相似度检索

设计决策：
- 用 pymilvus 原生 SDK 而不是 langchain-milvus 封装——
  schema 显式可控（元数据字段 h1/h2/source 对展示和溯源很重要），
  也避免多一层抽象导致调试困难
- metric 用 IP（内积）：BGE 归一化向量下内积即余弦相似度（见 embedder.py）
- 知识库量小（几十条）， Cosmos/Nutzbek 动态字段都不需要，用固定 schema
"""

import logging

from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient

from app.core.config import get_settings
from app.rag.embedder import embed_query, embed_texts

logger = logging.getLogger(__name__)

# 集合名固定：本项目只有一个知识库，不做多租户
COLLECTION_NAME = "kb_chunks"

# 向量维度：BGE-small-zh-v1.5 固定 512 维（换 embedding 模型必须同步改这里并重建集合）
EMBEDDING_DIM = 512


def _get_client() -> MilvusClient:
    """创建 Milvus 客户端（每次短连接，量小开销可忽略；长连接池在应用层再加）。"""
    return MilvusClient(uri=get_settings().milvus_uri)


def ensure_collection(client: MilvusClient) -> None:
    """幂等创建集合：存在则跳过，不存在则建集合 + 索引。

    索引参数 AUTOINDEX：Milvus standalone 下自动选择最合适的索引类型，
    开发环境不用手调 IVF/HNSW 参数。
    """
    if client.has_collection(COLLECTION_NAME):
        return

    schema = CollectionSchema(
        fields=[
            # 自增主键：Milvus 要求必须有主键
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            # 向量字段
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
            # 正文：检索命中后要返回给 LLM/前端
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=2048),
            # 元数据：溯源与展示用。VARCHAR 必须声明 max_length
            FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="h1", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="h2", dtype=DataType.VARCHAR, max_length=128),
        ],
        description="客服知识库切块",
    )

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_type="AUTOINDEX",
        metric_type="IP",  # 归一化向量的内积 = 余弦相似度
    )

    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=schema,
        index_params=index_params,
    )
    logger.info("Milvus 集合已创建: %s", COLLECTION_NAME)


def rebuild_index(kb_dir: str) -> int:
    """全量重建：清空旧数据 -> 全部切块 -> 向量化 -> 插入。

    知识库更新走"全量重建"而非增量：文档量小，重建简单可靠，
    不需要处理"文档改了但旧块没删干净"的一致性问题。
    返回插入的块数。
    """
    from app.rag.splitter import load_and_split_all

    client = _get_client()
    ensure_collection(client)

    # 全量清空：按主键删除需要先查出来，直接 drop+recreate 更省事
    client.drop_collection(COLLECTION_NAME)
    ensure_collection(client)

    chunks = load_and_split_all(kb_dir)
    if not chunks:
        return 0

    # 批量向量化（一次调用比逐条快很多，BGE 内部会分批跑）
    vectors = embed_texts([c.content for c in chunks])

    rows = [
        {
            "embedding": vec,
            "content": c.content,
            "source": c.source,
            "h1": c.h1,
            "h2": c.h2,
        }
        for c, vec in zip(chunks, vectors, strict=True)
    ]
    client.insert(collection_name=COLLECTION_NAME, data=rows)
    # flush：把 growing segment 落盘为 sealed segment。
    # 不 flush 的话统计与检索可能看不到数据（Milvus 最终一致性），必须显式调用
    client.flush(COLLECTION_NAME)

    logger.info("知识库重建完成: %d 块", len(rows))
    return len(rows)


def search(query: str, top_k: int | None = None, collection_name: str = COLLECTION_NAME) -> list[dict]:
    """语义检索：返回 top_k 个相关块（含相似度分数与元数据）。

    collection_name 参数化：便于测试注入不存在的集合名（异常路径测试）。
    """
    s = get_settings()
    top_k = top_k or s.retrieval_top_k

    client = _get_client()
    if not client.has_collection(collection_name):
        # 集合不存在说明知识库还没入库——显式报错而不是静默返回空，
        # 让调用方能区分"没建库"和"没检索到"
        raise RuntimeError("知识库集合不存在，请先执行入库（rebuild_index）")

    results = client.search(
        collection_name=collection_name,
        data=[embed_query(query)],
        limit=top_k,
        output_fields=["content", "source", "h1", "h2"],
        # 归一化向量 + IP 度量下，score 就是余弦相似度
        search_params={"metric_type": "IP"},
    )

    hits = results[0]  # 只有一个查询，取第一个结果集
    return [
        {
            "score": float(hit["distance"]),
            "content": hit["entity"]["content"],
            "source": hit["entity"]["source"],
            "h1": hit["entity"]["h1"],
            "h2": hit["entity"]["h2"],
        }
        for hit in hits
    ]
