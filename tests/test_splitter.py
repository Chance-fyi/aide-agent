"""切块单元测试。

只测纯函数逻辑，不依赖任何外部服务（Milvus/Postgres/LLM）。
"""

from pathlib import Path

from app.rag.splitter import load_and_split_all, split_markdown

KB_DIR = str(Path(__file__).resolve().parent.parent / "data" / "knowledge_base")


def test_split_keeps_headers_in_content():
    """标题应保留在正文里（标题是强语义信号，见 splitter.py 注释）。"""
    md = "# 退货政策\n\n## 退货条件\n\n7天内可退货。"
    chunks = split_markdown(md, source="test.md")
    assert len(chunks) == 1
    assert "## 退货条件" in chunks[0].content
    assert chunks[0].h1 == "退货政策"
    assert chunks[0].h2 == "退货条件"


def test_split_by_h2_sections():
    """两个 ## 小节应切成两块，元数据正确。"""
    md = "# 物流\n\n## 时效\n\n48小时发货。\n\n## 范围\n\n全国配送。"
    chunks = split_markdown(md, source="test.md")
    assert len(chunks) == 2
    assert {c.h2 for c in chunks} == {"时效", "范围"}


def test_long_section_is_fallback_split():
    """超过 400 字的小节应被二次切分，且所有块都继承标题元数据。"""
    body = "这是一段很长的内容。" * 80  # 800 字
    md = f"# 政策\n\n## 细则\n\n{body}"
    chunks = split_markdown(md, source="test.md")
    assert len(chunks) >= 2
    assert all(c.h2 == "细则" for c in chunks)
    assert all(len(c.content) <= 400 + 50 for c in chunks)  # 允许 overlap 余量


def test_real_kb_loads():
    """真实知识库可加载，块数与长度合理。"""
    chunks = load_and_split_all(KB_DIR)
    assert len(chunks) >= 20
    assert all(c.content.strip() for c in chunks)
    assert all(c.source.endswith(".md") for c in chunks)
    # 长度约束：所有块都不超过兜底上限（400 字正文 + overlap 余量）
    assert all(len(c.content) <= 460 for c in chunks)
