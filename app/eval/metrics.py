"""评估打分：纯函数，不碰 Milvus / LLM / Postgres。

为什么单独拆一层：评估结论的可信度取决于"尺子"本身。
把打分写成纯函数，黄金集解析与打分规则就能各自单测——
不用起中间件、不用花 LLM 费用，就能验证"判对判错"的规则是否正确。
反过来，如果打分逻辑埋在跑分脚本里，就只能靠跑一次全量评估来试，
慢、贵、而且分不清"模型变差了"还是"打分写错了"。
"""

import unicodedata
from dataclasses import dataclass, field

from app.eval.dataset import GoldenCase


def normalize(text: str) -> str:
    """归一化后再比对：全角转半角 + 去空白 + 小写。

    中文回答里"7 天""7天""７天"是同一件事，LLM 每次措辞不同。
    要点匹配若因空格或全角判错，评估结论就不可信——
    宁可归一化，也不要用宽松的模糊匹配把真正的缺失放过去。
    """
    return "".join(unicodedata.normalize("NFKC", text).split()).lower()

# ============================================================
# 检索层
# ============================================================

@dataclass(frozen=True)
class RetrievalScore:
    """检索层得分：期望来源文件是否出现在 top_k 命中里。

    checked=False 表示该用例不校验检索（如转人工、超范围用例，
    本就不该命中知识库）。

    top1_ok / mrr 是针对"召回饱和"补的两个排序指标：
    语料只有 31 个块时，top_k=3 几乎总能覆盖到目标文件，
    只看 recall 永远 100%，加 rerank 也测不出收益。
    """

    checked: bool
    hit: tuple[str, ...]
    missed: tuple[str, ...]
    any_ok: bool | None = None    # expect_sources_any 是否至少命中一个
    top1_ok: bool | None = None   # 第一条命中是否就是期望来源
    mrr: float = 0.0              # 第一个正确来源的倒数排名（1.0 = 首条就对）

    @property
    def ok(self) -> bool | None:
        if not self.checked:
            return None
        return not self.missed and self.any_ok is not False

def score_retrieval(case: GoldenCase, hit_sources: list[str]) -> RetrievalScore:
    """按命中来源打分（hit_sources 必须按相似度降序）。

    判据分两层：
    - expect_sources：必须**全部**命中（AND）——多跳题真的需要多个文件
    - expect_sources_any：至少命中**一个**（OR）——通用政策与品类细则均可接受
    两层同时存在时：AND 全部满足，且 OR 至少一个。

    排序质量另用 top1_ok / mrr 记录——
    这两个才是 rerank、query 改写这类优化真正能推动的指标。
    """
    if not (case.expect_sources or case.expect_sources_any):
        return RetrievalScore(checked=False, hit=(), missed=())
    got = set(hit_sources)
    hit = tuple(s for s in case.expect_sources if s in got)
    missed = tuple(s for s in case.expect_sources if s not in got)
    any_ok = (
        None
        if not case.expect_sources_any
        else any(s in got for s in case.expect_sources_any)
    )

    # 第一个命中"期望或可接受来源"的位次 → MRR；一个都没命中则记 0
    acceptable = set(case.expect_sources) | set(case.expect_sources_any)
    rank = next((i for i, s in enumerate(hit_sources, start=1) if s in acceptable), None)
    return RetrievalScore(
        checked=True,
        hit=hit,
        missed=missed,
        any_ok=any_ok,
        top1_ok=(rank == 1),
        mrr=0.0 if rank is None else 1.0 / rank,
    )

# ============================================================
# 生成层
# ============================================================

@dataclass(frozen=True)
class AnswerScore:
    """生成层得分：要点覆盖 + 转人工行为是否符合预期。"""

    answer: str | None            # None = 该用例没拿到回复（LLM 异常等）
    missing: tuple[str, ...]      # expect_points 中未出现的
    any_ok: bool | None           # expect_any 是否命中；None = 不校验
    transferred: bool
    transfer_ok: bool | None      # None = 不校验转接行为

    @property
    def ok(self) -> bool:
        if self.answer is None:
            return False
        if self.missing:
            return False
        if self.any_ok is False:
            return False
        return self.transfer_ok is not False

def score_answer(case: GoldenCase, answer: str | None, transferred: bool) -> AnswerScore:
    """按要点与转接行为打分。

    两类转接错误分开统计，因为处置方式完全不同：
    - 误转（不该转却转了）：体验问题，用户在自助能解决时被推给人工，成本也高
    - 漏转（该转没转）：服务问题，用户卡在自助里出不去
    混成一个"转接准确率"看不出该往哪边调。
    """
    if answer is None:
        return AnswerScore(
            answer=None, missing=case.expect_points, any_ok=None,
            transferred=transferred, transfer_ok=None,
        )

    norm = normalize(answer)
    missing = tuple(p for p in case.expect_points if normalize(p) not in norm)
    any_ok = None if not case.expect_any else any(normalize(a) in norm for a in case.expect_any)
    transfer_ok = None if case.expect_transfer is None else (transferred == case.expect_transfer)
    return AnswerScore(
        answer=answer,
        missing=missing,
        any_ok=any_ok,
        transferred=transferred,
        transfer_ok=transfer_ok,
    )

# ============================================================
# 汇总
# ============================================================

@dataclass(frozen=True)
class CaseResult:
    """单条用例的完整结果（检索层 + 生成层）。"""

    case: GoldenCase
    retrieval: RetrievalScore
    answer: AnswerScore | None = None   # 只跑检索层时为 None
    error: str | None = None            # 执行异常摘要（如限流/超时），区分"环境问题"与"质量问题"

    @property
    def passed(self) -> bool | None:
        """该用例是否通过；None = 两层都没跑（理论上不会出现）。"""
        if self.answer is None:
            return self.retrieval.ok
        if self.retrieval.ok is False:
            return False
        return self.answer.ok

@dataclass
class Summary:
    """跑分汇总。

    分母一律用"实际校验过的条数"，不是总条数——
    不校验检索的用例不该拉低检索命中率，否则指标没法跨版本比较。
    """

    total: int = 0
    retrieval_checked: int = 0
    retrieval_hit: int = 0
    retrieval_top1: int = 0     # 首条命中数（排序质量）
    retrieval_mrr: float = 0.0  # MRR 累加，汇总时除以 retrieval_checked
    answer_checked: int = 0
    answer_passed: int = 0
    false_transfer: int = 0    # 不该转却转了
    missed_transfer: int = 0   # 该转却没转
    unchecked: int = 0         # 当前模式下没有任何检查项的用例（不计入分母也不算失败）
    errors: int = 0            # 执行异常条数（限流/超时等环境问题）
    failed_ids: list[str] = field(default_factory=list)
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def retrieval_rate(self) -> float | None:
        return None if not self.retrieval_checked else self.retrieval_hit / self.retrieval_checked

    @property
    def top1_rate(self) -> float | None:
        return None if not self.retrieval_checked else self.retrieval_top1 / self.retrieval_checked

    @property
    def mrr(self) -> float | None:
        return None if not self.retrieval_checked else self.retrieval_mrr / self.retrieval_checked

    @property
    def answer_rate(self) -> float | None:
        return None if not self.answer_checked else self.answer_passed / self.answer_checked

    def as_dict(self) -> dict:
        """落盘用（JSON）：两次跑分按 id 逐条对比，指标存下来才能看趋势。"""
        return {
            "total": self.total,
            "retrieval": {
                "checked": self.retrieval_checked,
                "hit": self.retrieval_hit,
                "rate": self.retrieval_rate,
                "top1": self.retrieval_top1,
                "top1_rate": self.top1_rate,
                "mrr": self.mrr,
            },
            "answer": {
                "checked": self.answer_checked,
                "passed": self.answer_passed,
                "rate": self.answer_rate,
            },
            "transfer": {
                "false_transfer": self.false_transfer,
                "missed_transfer": self.missed_transfer,
            },
            "unchecked": self.unchecked,
            "errors": self.errors,
            "failed_ids": self.failed_ids,
            "by_category": self.by_category,
        }

def summarize(results: list[CaseResult]) -> Summary:
    """汇总逐条结果。"""
    s = Summary(total=len(results))
    for r in results:
        cat = s.by_category.setdefault(
            r.case.category, {"total": 0, "checked": 0, "passed": 0}
        )
        cat["total"] += 1

        if r.error:
            s.errors += 1

        if r.retrieval.checked:
            s.retrieval_checked += 1
            s.retrieval_hit += 1 if r.retrieval.ok else 0
            s.retrieval_top1 += 1 if r.retrieval.top1_ok else 0
            s.retrieval_mrr += r.retrieval.mrr

        if r.answer is not None:
            s.answer_checked += 1
            s.answer_passed += 1 if r.answer.ok else 0
            if r.answer.transfer_ok is False:
                if r.answer.transferred:
                    s.false_transfer += 1
                else:
                    s.missed_transfer += 1

        # passed is None：该用例在当前模式下没有可校验的检查项
        # （如只跑检索层时的转人工用例）——既不算通过也不算失败，
        # 计入分母会把命中率/通过率拉成假数字
        if r.passed is None:
            s.unchecked += 1
            continue
        cat["checked"] += 1
        if r.passed:
            cat["passed"] += 1
        else:
            s.failed_ids.append(r.case.id)
    return s

def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"

def format_summary(s: Summary) -> str:
    """人读的汇总文本（CLI 输出）。"""
    lines = [
        "",
        "=" * 62,
        f"用例总数        {s.total}",
        f"检索命中率      {_pct(s.retrieval_rate)}"
        + f"  ({s.retrieval_hit}/{s.retrieval_checked} 条校验了检索)",
        f"首条命中率      {_pct(s.top1_rate)}"
        + f"  ({s.retrieval_top1}/{s.retrieval_checked} 条首条即目标文件)",
        f"MRR            {'n/a' if s.mrr is None else f'{s.mrr:.3f}'}"
        + "  (第一个正确来源的倒数排名，越接近 1 越好)",
        f"答案通过率      {_pct(s.answer_rate)}"
        + f"  ({s.answer_passed}/{s.answer_checked} 条校验了答案)",
        f"误转人工        {s.false_transfer} 条（不该转却转了）",
        f"漏转人工        {s.missed_transfer} 条（该转却没转）",
    ]
    if s.unchecked:
        lines.append(f"未校验          {s.unchecked} 条（当前模式无对应检查项）")
    if s.errors:
        # 限流/超时不是模型质量问题，单独报出来避免被当成退化
        lines.append(f"执行异常        {s.errors} 条（限流/超时等环境问题，不代表质量）")
    if s.by_category:
        lines.append("-" * 62)
        for name, c in sorted(s.by_category.items()):
            score = "-" if not c["checked"] else f"{c['passed']}/{c['checked']}"
            lines.append(f"  {name:<8} {score}")
    if s.failed_ids:
        lines.append("-" * 62)
        lines.append(f"未通过: {', '.join(s.failed_ids)}")
    lines.append("=" * 62)
    return "\n".join(lines)
