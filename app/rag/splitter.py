"""知识库切块模块（多格式加载 + 父子结构）。

父子检索（small-to-big）设计：
- **子块**：向量化与检索单元，必须短（~400字）才能语义聚焦、向量准
- **父块**：返回给 LLM 的单元，是子块所属的完整小节（上限 1500 字），
  上下文完整，LLM 不用从碎片里猜政策全貌
- 超长小节（>1500字）：子块自为父块（自我兜底），避免父块过长稀释注意力

格式支持：
- .md：按 #/## 标题切，标题层级作为元数据（客服文档天然"主题-小节"结构）
- .txt/.docx/.pdf：无标题结构，按空行分段 → 相邻段落聚合为"小节"后走同一套父子切分
- 其他后缀：跳过并记 debug 日志（目录里混入无关文件不应炸掉入库）
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 提取的标题层级：# 和 ## 足够表达客服文档结构，更深层级意义不大
_HEADERS_TO_SPLIT_ON = [("#", "h1"), ("##", "h2")]

# 子块字符上限：BGE-small 的最佳输入长度约 256~512 token，
# 中文 1 字 ≈ 1 token，400 字以内能保证向量质量
_MAX_CHUNK_CHARS = 400
# 兜底二次切的重叠字符数：防止关键句子被切在边界上
_OVERLAP_CHARS = 50
# 父块（小节）字符上限：超过则子块自我兜底，父块过长会稀释 LLM 注意力
_PARENT_MAX_CHARS = 1500
# 无标题文档的"段落聚合"上限：模拟出小节粒度供父子检索用
_SECTION_MAX_CHARS = 800

@dataclass
class Chunk:
    """一块知识库片段：正文 + 来源元数据 + 父块（父子检索的返回单元）。"""

    content: str
    source: str           # 文件名，如 returns.md
    h1: str               # 所属一级标题，如 "退货政策"
    h2: str               # 所属二级标题，可能为空
    parent_content: str   # 所属小节全文；超长小节的子块自为父块

def _make_fallback() -> RecursiveCharacterTextSplitter:
    """兜底切分器：只处理超长文本。"""
    return RecursiveCharacterTextSplitter(
        chunk_size=_MAX_CHUNK_CHARS,
        chunk_overlap=_OVERLAP_CHARS,
    )

def _chunks_from_section(content: str, source: str, h1: str, h2: str) -> list[Chunk]:
    """把一个"小节"切成父子结构块。

    parent = 小节全文（≤1500字）；超长小节子块自为父块。
    md 与 txt/docx/pdf 的小节概念在此统一。
    """
    parent = content if len(content) <= _PARENT_MAX_CHARS else ""
    chunks: list[Chunk] = []
    for piece in _make_fallback().split_text(content):
        chunks.append(
            Chunk(
                content=piece,
                source=source,
                h1=h1,
                h2=h2,
                # 父块缺失（超长小节）时自我兜底：检索返回至少有完整可用文本
                parent_content=parent or piece,
            )
        )
    return chunks

def split_markdown(text: str, source: str, strategy: str | None = None) -> list[Chunk]:
    """把一篇 Markdown 文档切成父子结构块。

    strategy：
    - header（默认）：按 #/## 标题切，保留标题元数据——结构化文档首选
    - paragraph：无视标题，按空行分段聚合——适合结构混乱/伪标题文档
    - fixed：不识别任何结构，固定窗口滑切——对照组，效果通常最差
    None 时读全局配置 KB_SPLIT_STRATEGY（生产重建口径与对比工具共用）。
    """
    if strategy is None:
        strategy = get_settings().kb_split_strategy
    if strategy not in _STRATEGIES:
        raise ValueError(f"未知切分策略: {strategy}，可选: {sorted(_STRATEGIES)}")

    if strategy == "fixed":
        # 不识别结构：固定窗口滑切，每块自为父块（对照基准）
        return [
            Chunk(
                content=piece,
                source=source,
                h1=source,
                h2="",
                parent_content=piece,
            )
            for piece in _make_fallback().split_text(text.strip())
            if piece.strip()
        ]

    if strategy == "paragraph":
        return _split_plain_text(text, source)

    # header 策略（默认）
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_HEADERS_TO_SPLIT_ON,
        strip_headers=False,  # 保留标题文字在正文里，向量化时标题是强语义信号
    )
    sections = header_splitter.split_text(text)

    chunks: list[Chunk] = []
    for sec in sections:
        meta = sec.metadata  # {"h1": ..., "h2": ...}
        content = sec.page_content.strip()
        if not content:
            continue
        chunks.extend(_chunks_from_section(content, source, meta.get("h1", ""), meta.get("h2", "")))
    return chunks

# 可选切分策略（step-17）：策略选择难以凭直觉判断，
# 提供对照工具用真实查询比效果，再定生产口径
_STRATEGIES = {"header", "paragraph", "fixed"}

def _split_plain_text(text: str, source: str) -> list[Chunk]:
    """无标题文档的通用切法：空行分段 → 相邻段落聚合为小节 → 父子切分。

    聚合上限 800 字：模拟 md 的小节粒度，父块能覆盖上下文又不会过长。
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[Chunk] = []
    section: list[str] = []
    section_len = 0

    def flush() -> None:
        nonlocal section, section_len
        if not section:
            return
        content = "\n\n".join(section)
        chunks.extend(_chunks_from_section(content, source, h1=source, h2=""))
        section, section_len = [], 0

    for para in paragraphs:
        # 单段超限：整段独立成节，交给 _chunks_from_section 二次切
        if section and section_len + len(para) > _SECTION_MAX_CHARS:
            flush()
        section.append(para)
        section_len += len(para)
    flush()
    return chunks

def _load_markdown(path: Path) -> list[Chunk]:
    return split_markdown(path.read_text(encoding="utf-8"), source=path.name)

def _load_txt(path: Path) -> list[Chunk]:
    return _split_plain_text(path.read_text(encoding="utf-8"), source=path.name)

def _load_docx(path: Path) -> list[Chunk]:
    """docx：提取所有非空段落，按纯文本流程处理（标题样式暂不区分）。"""
    from docx import Document

    doc = Document(str(path))
    text = "\n\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())
    return _split_plain_text(text, source=path.name)

def _load_pdf(path: Path) -> list[Chunk]:
    """pdf：逐页提取文字后按纯文本流程处理。扫描件无文字层会得到空结果（预期行为）。"""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    text = "\n\n".join(p for p in pages if p)
    return _split_plain_text(text, source=path.name)

# 后缀 → 加载器注册表：新格式加一行即可扩展
_LOADERS = {
    ".md": _load_markdown,
    ".txt": _load_txt,
    ".docx": _load_docx,
    ".pdf": _load_pdf,
}

def load_and_split_all(
    kb_dir: str,
    strategies: dict[str, str] | None = None,
    force: str | None = None,
) -> list[Chunk]:
    """读取知识库目录下所有受支持格式的文件并切块。

    切分策略按文件独立选择（不同文档结构不同，全局统一效果差），
    优先级：force（对比工具强制统一）> strategies（kb_document 表的覆盖）
    > 后缀默认（.md → header，其余 → paragraph，md 天然有标题结构，
    纯文本格式没有）。
    """
    if force is not None and force not in _STRATEGIES:
        raise ValueError(f"未知切分策略: {force}，可选: {sorted(_STRATEGIES)}")
    strategies = strategies or {}

    all_chunks: list[Chunk] = []
    for f in sorted(Path(kb_dir).iterdir()):
        if not f.is_file():
            continue
        strategy = force or strategies.get(f.name) or _default_strategy(f.suffix.lower())
        if f.suffix.lower() == ".md":
            all_chunks.extend(split_markdown(f.read_text(encoding="utf-8"), source=f.name, strategy=strategy))
            continue
        loader = _LOADERS.get(f.suffix.lower())
        if loader is None:
            logger.debug("跳过不支持的文件格式: %s", f.name)
            continue
        # 纯文本格式只有一种切法（段落聚合），loader 不接受策略参数；
        # 但 force=fixed 时也尊重配置（对照实验覆盖所有文件）
        if strategy == "fixed" and f.suffix.lower() != ".md":
            all_chunks.extend(split_markdown(f.read_text(encoding="utf-8"), source=f.name, strategy="fixed"))
        else:
            all_chunks.extend(loader(f))
    return all_chunks

def split_file(path: Path, strategy: str) -> list[Chunk]:
    """按指定策略切分单个文件（重建流程逐文件调用的入口）。

    与 load_and_split_all 共用加载器注册表；策略合法性在调用前校验。
    """
    if strategy not in _STRATEGIES:
        raise ValueError(f"未知切分策略: {strategy}，可选: {sorted(_STRATEGIES)}")
    suffix = path.suffix.lower()
    if suffix == ".md":
        return split_markdown(path.read_text(encoding="utf-8"), source=path.name, strategy=strategy)
    loader = _LOADERS.get(suffix)
    if loader is None:
        logger.debug("跳过不支持的文件格式: %s", path.name)
        return []
    if strategy == "fixed":
        # 纯文本格式没有标题结构，fixed 走“不识别结构”的通用路径
        return split_markdown(path.read_text(encoding="utf-8"), source=path.name, strategy="fixed")
    return loader(path)

def _default_strategy(suffix: str) -> str:
    """后缀 → 默认策略：结构化 md 按标题切，无标题格式按段落聚合。"""
    return "header" if suffix == ".md" else "paragraph"
