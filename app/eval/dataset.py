"""黄金问答集（golden set）加载与校验。

黄金集是评估的"尺子"，而尺子自己会腐化：
- 知识库改了文件名（returns.md → return.md）→ 期望来源全部失效
- 政策数字变了（7 天改 15 天）→ 要点还写着旧值
- 手写 JSON 时漏了 id、重复了 id

这类腐化如果只在跑分时表现为"分数掉了"，排查成本极高。
所以加载阶段就做硬校验，问题在跑分前炸出来，并带上行号。
"""

import json
from dataclasses import dataclass
from pathlib import Path

# 默认黄金集路径（相对项目根；调用方可用 --dataset 覆盖）
DEFAULT_DATASET = "data/eval/golden.jsonl"
DEFAULT_KB_DIR = "data/knowledge_base"

class DatasetError(Exception):
    """黄金集格式/内容错误（带行号，直接指向要改的那一行）。"""

@dataclass(frozen=True)
class GoldenCase:
    """一条黄金用例。

    三层校验字段对应两条链路：
    - expect_sources：检索层。期望命中的知识库文件（校验"找得准不准"）
    - expect_points / expect_any：生成层。答案必须包含 / 至少包含其一
    - expect_transfer：生成层。是否应当转人工；None 表示不校验
    """

    id: str
    question: str
    category: str = "未分类"
    expect_sources: tuple[str, ...] = ()
    expect_points: tuple[str, ...] = ()
    expect_any: tuple[str, ...] = ()
    expect_transfer: bool | None = None

def _str_tuple(raw: object, field: str, where: str) -> tuple[str, ...]:
    """把 JSON 里的数组字段规整成字符串元组，类型不对直接报错。"""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise DatasetError(f"{where}：字段 {field} 必须是非空字符串数组")
    return tuple(x.strip() for x in raw)

def load_cases(
    path: str | Path = DEFAULT_DATASET,
    kb_dir: str | Path = DEFAULT_KB_DIR,
) -> list[GoldenCase]:
    """读取并校验黄金集。

    空行与 `#` 开头的注释行跳过——黄金集要人工维护，
    允许用注释分组比强制纯 JSONL 更好用。
    """
    p = Path(path)
    if not p.exists():
        raise DatasetError(f"黄金集不存在: {p}")

    # 知识库现有文件名：用来校验 expect_sources 不是历史残留
    kb_names = {f.name for f in Path(kb_dir).glob("*") if f.is_file()}

    cases: list[GoldenCase] = []
    seen: dict[str, int] = {}

    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{p.name}:{lineno}"

        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise DatasetError(f"{where}：不是合法 JSON（{e.msg}）") from None
        if not isinstance(raw, dict):
            raise DatasetError(f"{where}：每行必须是一个 JSON 对象")

        cid = raw.get("id")
        if not isinstance(cid, str) or not cid.strip():
            raise DatasetError(f"{where}：缺少 id")
        if cid in seen:
            # id 唯一才能稳定对比两次跑分（按 id 对齐逐条 diff）
            raise DatasetError(f"{where}：id {cid!r} 与第 {seen[cid]} 行重复")
        seen[cid] = lineno

        question = raw.get("question")
        if not isinstance(question, str) or not question.strip():
            raise DatasetError(f"{where}：缺少 question")

        sources = _str_tuple(raw.get("expect_sources"), "expect_sources", where)
        missing = [s for s in sources if s not in kb_names]
        if missing:
            # 来源文件名写错是最常见的腐化形式，直接指出真实文件名提示
            raise DatasetError(
                f"{where}：expect_sources 中的 {missing} 不在知识库目录 "
                f"（现有: {sorted(kb_names)}）"
            )

        transfer = raw.get("expect_transfer")
        if transfer is not None and not isinstance(transfer, bool):
            raise DatasetError(f"{where}：expect_transfer 只能是 true / false / null")

        category = raw.get("category") or "未分类"
        if not isinstance(category, str):
            raise DatasetError(f"{where}：category 必须是字符串")

        cases.append(
            GoldenCase(
                id=cid.strip(),
                question=question.strip(),
                category=category.strip(),
                expect_sources=sources,
                expect_points=_str_tuple(raw.get("expect_points"), "expect_points", where),
                expect_any=_str_tuple(raw.get("expect_any"), "expect_any", where),
                expect_transfer=transfer,
            )
        )

    if not cases:
        raise DatasetError(f"黄金集为空: {p}")
    return cases
