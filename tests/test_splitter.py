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

# ---------- step-16：父子结构 + 多格式 ----------

def test_parent_is_full_section():
    """子块的 parent_content 应是完整小节：父块可含子块之外的上下文。"""
    md = "# 退货政策\n\n## 退货条件\n\n7天内可退货。\n\n定制类商品除外，需保留包装。"
    chunks = split_markdown(md, source="test.md")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.parent_content == c.content  # 单块小节，父块即全文

def test_parent_dedup_when_long_section_split():
    """超长小节二次切后，所有子块共享同一父块（完整小节）。"""
    body = "这是政策细则的内容。" * 30  # 300 字，不超过父块上限但超过子块上限？
    # 用更长的：600 字 → 超过子块上限 400，二次切；小于父块上限 1500
    body = "这是政策细则的内容。" * 60
    md = f"# 政策\n\n## 细则\n\n{body}"
    chunks = split_markdown(md, source="test.md")
    assert len(chunks) >= 2
    parents = {c.parent_content for c in chunks}
    assert len(parents) == 1, "同一小节的子块应共享同一父块"
    parent = parents.pop()
    assert len(parent) > len(chunks[0].content), "父块应比子块更长（含完整上下文）"

def test_overlong_section_self_parent():
    """超过父块上限（1500字）的小节：子块自为父块，避免父块过长稀释注意力。"""
    body = "很长的细则内容。" * 400  # 2800 字
    md = f"# 政策\n\n## 细则\n\n{body}"
    chunks = split_markdown(md, source="test.md")
    assert all(c.parent_content == c.content for c in chunks)

def test_txt_loader(tmp_path):
    """txt：空行分段聚合为小节，元数据 h1=文件名。"""
    f = tmp_path / "notice.txt"
    f.write_text("第一段：发货时效说明，48小时内发货。\n\n第二段：偏远地区顺延一至两天。\n\n第三段：节假日顺延。", encoding="utf-8")
    chunks = load_and_split_all(str(tmp_path))
    assert len(chunks) >= 1
    assert all(c.h1 == "notice.txt" for c in chunks)
    assert "48小时" in "".join(c.content for c in chunks)

def test_docx_loader(tmp_path):
    """docx：提取段落正文，与 txt 同流程。"""
    from docx import Document

    doc = Document()
    doc.add_paragraph("会员积分在订单签收后 7 天自动到账。")
    doc.add_paragraph("积分可用于抵扣现金，100 积分抵 1 元。")
    f = tmp_path / "membership.docx"
    doc.save(str(f))

    chunks = load_and_split_all(str(tmp_path))
    assert len(chunks) >= 1
    assert "积分" in "".join(c.content for c in chunks)

def test_unsupported_format_skipped(tmp_path):
    """目录里的无关文件（图片等）应被跳过，不炸掉入库流程。"""
    (tmp_path / "logo.png").write_bytes(b"fake")
    (tmp_path / "a.md").write_text("# T\n\n内容", encoding="utf-8")
    chunks = load_and_split_all(str(tmp_path))
    assert len(chunks) == 1

def test_split_strategies_all_work():
    """三种策略都能切块且口径正确：fixed 每块自为父块，header 保留标题元数据。"""
    md = "# 政策\n\n## 退货\n\n七天无理由退货。超出 400 字的内容。" * 12

    header = split_markdown(md, "t.md", strategy="header")
    para = split_markdown(md, "t.md", strategy="paragraph")
    fixed = split_markdown(md, "t.md", strategy="fixed")
    assert all(len(x) >= 1 for x in (header, para, fixed))
    # header：标题进元数据；fixed/paragraph：h1 退化为文件名
    assert header[0].h2 == "退货"
    assert fixed[0].h1 == "t.md" and fixed[0].parent_content == fixed[0].content
    assert para[0].h1 == "t.md"

def test_unknown_strategy_rejected():
    """非法策略名应显式报错，而不是静默走默认。"""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="未知切分策略"):
        split_markdown("# T\n\n内容", "t.md", strategy="magic")
