"""评估集与打分逻辑的单元测试。

刻意不依赖 Milvus / LLM / Postgres：
"尺子"本身必须能在任何环境下一秒钟验证，
否则评估跑出的分数出了问题，没人能判断是模型退化了还是打分写错了。
"""

import json

import pytest

from app.eval.dataset import DEFAULT_DATASET, DatasetError, GoldenCase, load_cases
from app.eval.metrics import (
    CaseResult,
    RetrievalScore,
    normalize,
    score_answer,
    score_retrieval,
    summarize,
)


def _case(**kw) -> GoldenCase:
    """构造用例，只覆盖关心的字段。"""
    base = {"id": "t-1", "question": "测试问题"}
    base.update(kw)
    return GoldenCase(**base)

def _write(tmp_path, lines: list[dict | str]) -> str:
    """写一份临时黄金集；dict 自动转 JSON 行，str 原样写入（用于构造坏行）。"""
    p = tmp_path / "golden.jsonl"
    p.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else x for x in lines),
        encoding="utf-8",
    )
    return str(p)

@pytest.fixture
def kb_dir(tmp_path):
    """最小知识库目录：只有 returns.md。"""
    d = tmp_path / "kb"
    d.mkdir()
    (d / "returns.md").write_text("# 退货", encoding="utf-8")
    return str(d)

# ============================================================
# 黄金集加载与校验
# ============================================================

def test_real_dataset_is_valid():
    """生产黄金集必须始终可加载——这条测试就是"尺子防腐"的守门员。

    改了知识库文件名、写错 JSON、id 重复，都会在这里先炸，
    而不是等到跑分时表现为一堆莫名的失败。
    """
    cases = load_cases(DEFAULT_DATASET)
    assert len(cases) >= 20
    assert len({c.id for c in cases}) == len(cases)
    # 每条用例至少要校验点东西，否则它跑通了也没有意义
    assert all(c.expect_sources or c.expect_points or c.expect_any
               or c.expect_transfer is not None for c in cases)

def test_loads_fields_and_skips_comments(tmp_path, kb_dir):
    path = _write(tmp_path, [
        "# 这是注释行",
        "",
        {"id": "a", "question": "问一", "category": "退货",
         "expect_sources": ["returns.md"], "expect_points": ["7天"],
         "expect_transfer": False},
    ])
    cases = load_cases(path, kb_dir)
    assert len(cases) == 1
    c = cases[0]
    assert (c.id, c.question, c.category) == ("a", "问一", "退货")
    assert c.expect_sources == ("returns.md",)
    assert c.expect_points == ("7天",)
    assert c.expect_transfer is False

def test_unknown_source_file_is_rejected(tmp_path, kb_dir):
    """来源文件不存在 = 黄金集已腐化（知识库改名/删除），必须报错并指出真实文件名。"""
    path = _write(tmp_path, [{"id": "a", "question": "q", "expect_sources": ["nope.md"]}])
    with pytest.raises(DatasetError, match="nope.md"):
        load_cases(path, kb_dir)

def test_duplicate_id_is_rejected(tmp_path, kb_dir):
    path = _write(tmp_path, [
        {"id": "a", "question": "q1"},
        {"id": "a", "question": "q2"},
    ])
    with pytest.raises(DatasetError, match="重复"):
        load_cases(path, kb_dir)

def test_bad_json_line_reports_line_number(tmp_path, kb_dir):
    path = _write(tmp_path, [{"id": "a", "question": "q"}, "{不是 json"])
    with pytest.raises(DatasetError, match=":2"):
        load_cases(path, kb_dir)

def test_transfer_must_be_bool_or_null(tmp_path, kb_dir):
    path = _write(tmp_path, [{"id": "a", "question": "q", "expect_transfer": "yes"}])
    with pytest.raises(DatasetError, match="expect_transfer"):
        load_cases(path, kb_dir)

def test_missing_question_is_rejected(tmp_path, kb_dir):
    path = _write(tmp_path, [{"id": "a"}])
    with pytest.raises(DatasetError, match="question"):
        load_cases(path, kb_dir)

# ============================================================
# 检索层打分
# ============================================================

def test_normalize_ignores_space_and_fullwidth():
    """LLM 措辞在空格/全角上不稳定，归一化后比对才不会误判要点缺失。"""
    assert normalize("7 天") == normalize("７天") == "7天"
    assert normalize("  Deposit ") == "deposit"

def test_score_retrieval_hit():
    case = _case(expect_sources=("returns.md", "refund.md"))
    s = score_retrieval(case, ["refund.md", "shipping.md"])
    assert s.checked is True and s.ok is False
    assert s.hit == ("refund.md",) and s.missed == ("returns.md",)

def test_score_retrieval_unchecked_when_no_expectation():
    """转人工/超范围用例不校验检索，不能计入分母拉低命中率。"""
    s = score_retrieval(_case(), ["whatever.md"])
    assert s.checked is False and s.ok is None

# ============================================================
# 生成层打分
# ============================================================

def test_points_are_all_required():
    case = _case(expect_points=("7天", "无理由"))
    assert score_answer(case, "签收 7 天内可申请无理由退货", False).ok is True
    # 少一个要点就是没答全：AND 语义，不是"命中一个算过"
    s = score_answer(case, "签收 7 天内可以申请退货", False)
    assert s.ok is False and s.missing == ("无理由",)

def test_expect_any_requires_at_least_one():
    case = _case(expect_any=("售前", "售后"))
    assert score_answer(case, "我是售前售后客服", False).any_ok is True
    s = score_answer(case, "这个我不会", False)
    assert s.any_ok is False and s.ok is False

def test_transfer_mismatch_both_directions():
    """误转与漏转都判失败——两者处置方式不同，不能只拦一边。"""
    assert score_answer(_case(expect_transfer=True), "已转接", True).ok is True
    assert score_answer(_case(expect_transfer=False), "已转接", True).ok is False
    assert score_answer(_case(expect_transfer=True), "没转", False).ok is False

def test_transfer_unchecked_when_expectation_is_null():
    case = _case(expect_transfer=None, expect_points=("可以",))
    assert score_answer(case, "可以的", True).transfer_ok is None

def test_missing_answer_always_fails():
    """没拿到回复就是失败，不能因为用例没写要点而算通过。"""
    s = score_answer(_case(), None, False)
    assert s.ok is False and s.answer is None

# ============================================================
# 汇总
# ============================================================

def test_summary_counts_layers_and_transfer_errors():
    hit = CaseResult(
        case=_case(id="ok", expect_sources=("returns.md",), expect_points=("7天",)),
        retrieval=RetrievalScore(checked=True, hit=("returns.md",), missed=()),
        answer=score_answer(_case(id="ok", expect_points=("7天",)), "7天内可退", False),
    )
    miss = CaseResult(
        case=_case(id="miss", expect_sources=("refund.md",)),
        retrieval=RetrievalScore(checked=True, hit=(), missed=("refund.md",)),
    )
    false_t = CaseResult(
        case=_case(id="ft", expect_transfer=False),
        retrieval=RetrievalScore(checked=False, hit=(), missed=()),
        answer=score_answer(_case(id="ft", expect_transfer=False), "已转人工", True),
    )
    missed_t = CaseResult(
        case=_case(id="mt", expect_transfer=True),
        retrieval=RetrievalScore(checked=False, hit=(), missed=()),
        answer=score_answer(_case(id="mt", expect_transfer=True), "请自助处理", False),
    )

    s = summarize([hit, miss, false_t, missed_t])
    assert s.total == 4
    # 分母只算"校验过的"：4 条里只有 2 条校验了检索 / 3 条校验了答案
    assert (s.retrieval_checked, s.retrieval_hit) == (2, 1)
    assert (s.answer_checked, s.answer_passed) == (3, 1)
    assert (s.false_transfer, s.missed_transfer) == (1, 1)
    assert s.failed_ids == ["miss", "ft", "mt"]
    assert s.by_category["未分类"] == {"total": 4, "checked": 4, "passed": 1}

def test_summary_rates_are_none_when_nothing_checked():
    """检索层-only 跑分时答案率应为 n/a，而不是 0%。"""
    s = summarize([CaseResult(case=_case(), retrieval=RetrievalScore(False, (), ()))])
    assert s.retrieval_rate is None and s.answer_rate is None

def test_error_is_counted_separately_from_quality():
    """限流/超时造成的无回复也要报失败（不能造假通过），

    但必须单独计数：否则一次厂商 429 就会被误读成"模型退化了"。
    """
    r = CaseResult(
        case=_case(id="e", expect_points=("7天",)),
        retrieval=RetrievalScore(checked=False, hit=(), missed=()),
        answer=score_answer(_case(id="e", expect_points=("7天",)), None, False),
        error="RateLimitError: 429",
    )
    s = summarize([r])
    assert s.errors == 1 and s.answer_passed == 0
    assert s.failed_ids == ["e"]

def test_unchecked_case_is_neither_pass_nor_fail():
    """只跑检索层时，转人工用例没有检查项：

    既不能算失败（会把失败数刷高、退出码非 0，CI 永远红），
    也不能算通过（会把通过率虚抬）。
    """
    r = CaseResult(case=_case(expect_transfer=True), retrieval=RetrievalScore(False, (), ()))
    s = summarize([r])
    assert r.passed is None
    assert s.unchecked == 1 and s.failed_ids == []
    assert s.by_category["未分类"] == {"total": 1, "checked": 0, "passed": 0}
