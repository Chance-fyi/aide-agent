"""Embedding 封装。

选型：本地运行 BAAI/bge-small-zh-v1.5（中文检索专用小模型）。
- 免费零成本、无网络依赖、无 Key
- 中文语义检索效果好（C-MTEB 榜单小模型里的常客）
- 首次运行会自动从 HuggingFace 下载模型（约 100MB），之后走本地缓存

统一走 get_embeddings() 单例，避免重复加载模型占用内存。
"""

from functools import lru_cache

from langchain_huggingface import HuggingFaceEmbeddings

from app.core.config import get_settings


@lru_cache
def get_embeddings() -> HuggingFaceEmbeddings:
    """创建本地 BGE embedding 模型（进程内单例）。

    normalize_embeddings=True：向量归一化后用内积（IP）等价于余弦相似度，
    这是 BGE 官方推荐用法，也是 Milvus 检索的标准姿势。
    """
    s = get_settings()
    return HuggingFaceEmbeddings(
        model_name=s.embedding_model,
        model_kwargs={"device": "cpu"},          # small 模型 CPU 足够
        encode_kwargs={"normalize_embeddings": True},
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化。BGE 对短文本检索不需要额外前缀（query/passage 同构）。"""
    return get_embeddings().embed_documents(texts)


def embed_query(text: str) -> list[float]:
    """单条查询向量化。"""
    return get_embeddings().embed_query(text)

def warmup() -> None:
    """预热 embedding 模型与 Milvus 连接（应用启动时调用）。

    为什么需要：首次 `embed_query` 要加载 torch 权重（实测约 19 秒），
    首次 Milvus 检索要建 gRPC 连接。若不预热，这些成本会全部落到
    **第一个真实用户**头上——表现为"发第一条消息要等 20 秒"，
    而后续消息 0.02 秒。用户不会知道这是冷启动，只会觉得"这系统很卡"。

    失败只记日志不抛：预热是优化，不能成为启动前提
    （Milvus 未就绪时服务仍应能起来，等真正检索时再报错）。
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        embed_query("warmup")
    except Exception:
        logger.warning("embedding 预热失败（首个请求会承担加载开销）", exc_info=True)
