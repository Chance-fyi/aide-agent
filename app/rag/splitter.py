"""知识库切块模块。

策略（按标题结构切）：
1. 用 MarkdownHeaderTextSplitter 按 #/## 标题切，保留标题层级作为元数据——
   客服文档天然是"主题-小节"结构，按标题切能保证一块内容语义完整
2. 超长小节（超过 max_chunk_chars）用 RecursiveCharacterTextSplitter 兜底二次切，
   避免单块过长稀释向量语义
"""

from dataclasses import dataclass

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

# 提取的标题层级：# 和 ## 足够表达客服文档结构，更深层级意义不大
_HEADERS_TO_SPLIT_ON = [("#", "h1"), ("##", "h2")]

# 单块字符上限：BGE-small 的最佳输入长度约 256~512 token，
# 中文 1 字 ≈ 1 token，400 字以内能保证向量质量
_MAX_CHUNK_CHARS = 400
# 兜底二次切的重叠字符数：防止关键句子被切在边界上
_OVERLAP_CHARS = 50


@dataclass
class Chunk:
    """一块知识库片段：正文 + 来源元数据（便于检索结果溯源/展示）。"""

    content: str
    source: str   # 文件名，如 returns.md
    h1: str       # 所属一级标题，如 "退货政策"
    h2: str       # 所属二级标题，可能为空


def split_markdown(text: str, source: str) -> list[Chunk]:
    """把一篇 Markdown 文档切成带元数据的块列表。"""
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_HEADERS_TO_SPLIT_ON,
        strip_headers=False,  # 保留标题文字在正文里，向量化时标题是强语义信号
    )
    sections = header_splitter.split_text(text)

    # 兜底切分器：只处理超长小节
    fallback = RecursiveCharacterTextSplitter(
        chunk_size=_MAX_CHUNK_CHARS,
        chunk_overlap=_OVERLAP_CHARS,
    )

    chunks: list[Chunk] = []
    for sec in sections:
        meta = sec.metadata  # {"h1": ..., "h2": ...}
        content = sec.page_content.strip()
        if not content:
            continue
        if len(content) <= _MAX_CHUNK_CHARS:
            chunks.append(Chunk(content, source, meta.get("h1", ""), meta.get("h2", "")))
        else:
            # 超长小节：二次切，每块继承同一份标题元数据
            for piece in fallback.split_text(content):
                chunks.append(Chunk(piece, source, meta.get("h1", ""), meta.get("h2", "")))
    return chunks


def load_and_split_all(kb_dir: str) -> list[Chunk]:
    """读取知识库目录下所有 .md 文件并切块。"""
    from pathlib import Path

    all_chunks: list[Chunk] = []
    for md_file in sorted(Path(kb_dir).glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        all_chunks.extend(split_markdown(text, source=md_file.name))
    return all_chunks
