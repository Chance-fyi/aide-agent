# Step 4 — 知识库语料 + 按标题切块 + BGE Embedding

## 目标

完成 RAG 的"知识侧"：语料准备、切块、向量化。

## 语料

`data/knowledge_base/` 下 9 篇 Markdown：退货 / 换货 / 退款 / 物流 / 支付 /
会员 / 账号 / 优惠券 / 保修 / 常见问题。
每篇按 `# 大主题 + ## 小节` 组织，模拟真实客服知识库的结构。

## 切块策略（`app/rag/splitter.py`）

按标题结构切，而不是定长切：

1. `MarkdownHeaderTextSplitter` 按 `#/##` 切，标题层级进元数据（h1/h2）
2. `strip_headers=False`：**保留标题在正文里**——标题是强语义信号，
   向量化时"## 退货条件"这行字能显著提升召回精度
3. 超长小节（>400 字）用 `RecursiveCharacterTextSplitter` 二次切
   （chunk=400 / overlap=50），防止一块内容过长稀释向量语义

实测：9 篇文档 → 31 块，最大块 250 字，无溢出。

## Embedding 选型（`app/rag/embedder.py`）

本地 BAAI/bge-small-zh-v1.5：

- 中文检索专用（C-MTEB 小模型梯队），512 维，CPU 推理足够
- 免费、无 Key、无网络依赖（首次自动下载约 100MB 到本地缓存）
- `normalize_embeddings=True`：归一化后内积 = 余弦相似度，BGE 官方推荐

## 验证

```bash
uv run python -c "
from app.rag.splitter import load_and_split_all
print(len(load_and_split_all('data/knowledge_base')))  # 31
"
# 语义排序验证：'怎么退货' 与退货块相似度 > 积分块
```

## 遗留问题

- HuggingFace 下载慢时可设 `HF_ENDPOINT=https://hf-mirror.com`
- 检索环节（Milvus 入库 + search_kb）在步骤 5 完成
