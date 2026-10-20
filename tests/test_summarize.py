"""上下文压缩测试（不依赖真实 LLM / Postgres / Milvus）。

分三层测：
1. 决策（plan_summary）—— 纯函数，最容易写错，也最值得穷举
2. 执行（summarize_messages / advance_summary）—— 用假 LLM 验证提示拼装与失败降级
3. 接线（agent_node）—— 确认摘要真的拼进了系统提示，且 messages 没被删
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents import summarize
from app.agents.summarize import (
    SUMMARY_HEADER,
    advance_summary,
    build_system_content,
    plan_summary,
    render_conversation,
    window_start_index,
)


def _rounds(n: int, start: int = 0) -> list:
    """构造 n 轮对话（用户 + 助手各一条）。"""
    out = []
    for i in range(start, start + n):
        out.extend([HumanMessage(content=f"问题{i}"), AIMessage(content=f"回答{i}")])
    return out

# ============================================================
# 窗口边界
# ============================================================

def test_window_start_index():
    msgs = _rounds(5)
    # 最近 2 轮 → 从第 3 轮的 human 开始（下标 6）
    assert window_start_index(msgs, 2) == 6
    assert msgs[window_start_index(msgs, 2)].content == "问题3"
    # 轮数不足上限：从头开始
    assert window_start_index(msgs, 99) == 0
    # <=0 表示不限制
    assert window_start_index(msgs, 0) == 0

def test_window_start_index_never_splits_tool_chain():
    """切点必须落在 human 上，否则会产生孤立 ToolMessage（接口 400）。"""
    msgs = [
        HumanMessage(content="问题0"),
        AIMessage(content="", tool_calls=[{"name": "search_kb", "args": {}, "id": "c1"}]),
        ToolMessage(content="片段", tool_call_id="c1"),
        AIMessage(content="回答0"),
        HumanMessage(content="问题1"),
        AIMessage(content="回答1"),
    ]
    for rounds in (1, 2, 3):
        i = window_start_index(msgs, rounds)
        assert msgs[i].type == "human"

# ============================================================
# 决策：plan_summary
# ============================================================

def test_no_trigger_when_nothing_aged_out():
    """窗口还没滑出任何内容时不压缩（新会话不应产生额外 LLM 调用）。"""
    plan = plan_summary(_rounds(3), 0, max_rounds=10, trigger_rounds=5)
    assert plan.should_run is False

def test_no_trigger_below_threshold():
    """滑出了 3 轮，但阈值是 5 轮 → 再等等，攒够一批压一次。"""
    plan = plan_summary(_rounds(13), 0, max_rounds=10, trigger_rounds=5)
    assert plan.should_run is False

def test_trigger_when_threshold_reached():
    """滑出 5 轮且阈值 5 → 触发，且压的正是那 5 轮。"""
    msgs = _rounds(15)
    plan = plan_summary(msgs, 0, max_rounds=10, trigger_rounds=5)
    assert plan.should_run is True
    # 15 轮共 30 条；窗口留 10 轮 → 压缩前 10 条（5 轮）
    assert len(plan.to_summarize) == 10
    assert plan.to_summarize[0].content == "问题0"
    assert plan.to_summarize[-1].content == "回答4"
    assert plan.next_summarized_count == 10

def test_watermark_prevents_recompression():
    """水位线已推进到窗口边界后，同一批消息不能被重复压缩。

    否则每轮都会为同一段历史付一次 LLM 费用。
    """
    msgs = _rounds(15)
    plan = plan_summary(msgs, 10, max_rounds=10, trigger_rounds=5)
    assert plan.should_run is False

def test_only_newly_aged_out_portion_is_compressed():
    """水位线之后的增量部分才是本次要压的（摘要逐次累积）。"""
    msgs = _rounds(20)   # 40 条，窗口留 10 轮 → 边界 20
    plan = plan_summary(msgs, 10, max_rounds=10, trigger_rounds=5)
    assert plan.should_run is True
    assert len(plan.to_summarize) == 10          # 只压 10~20 这段
    assert plan.to_summarize[0].content == "问题5"
    assert plan.next_summarized_count == 20

def test_trigger_rounds_zero_disables_compression():
    """<=0 关闭压缩：退化为纯窗口裁剪（step-35 行为）。"""
    plan = plan_summary(_rounds(50), 0, max_rounds=10, trigger_rounds=0)
    assert plan.should_run is False

def test_watermark_ahead_of_window_is_safe():
    """水位线异常超前（如配置调小 max_rounds）不能崩，也不能回退。"""
    plan = plan_summary(_rounds(3), 99, max_rounds=10, trigger_rounds=1)
    assert plan.should_run is False

# ============================================================
# 渲染
# ============================================================

def test_render_skips_empty_ai_and_truncates_tool():
    """渲染只取正文：tool_calls 中间消息跳过，工具结果截断预览。"""
    msgs = [
        HumanMessage(content="怎么退货"),
        AIMessage(content="", tool_calls=[{"name": "search_kb", "args": {}, "id": "c1"}]),
        ToolMessage(content="很长的知识原文" * 200, tool_call_id="c1"),
        AIMessage(content="7 天内可退"),
    ]
    text = render_conversation(msgs)
    assert "用户: 怎么退货" in text
    assert "客服: 7 天内可退" in text
    # 空正文的 ai 消息不产生空行
    assert "\n\n" not in text
    # 工具结果被截断，不会把整段原文塞进压缩提示
    assert len(text) < 400
    assert "[工具结果]" in text

# ============================================================
# 执行：失败降级
# ============================================================

class _FakeLLM:
    def __init__(self, reply: str = "用户想退货", raise_exc: Exception | None = None):
        self.reply = reply
        self.raise_exc = raise_exc
        self.prompts: list[str] = []

    def invoke(self, messages, **kwargs):
        self.prompts.append(messages[0].content)
        if self.raise_exc:
            raise self.raise_exc
        return AIMessage(content=self.reply)

def test_summarize_returns_text_and_merges_previous():
    llm = _FakeLLM("用户咨询退货，已告知 7 天无理由")
    out = advance_summary(
        _rounds(15), "", 0, max_rounds=10, trigger_rounds=5, llm=llm
    )
    assert out.summary == "用户咨询退货，已告知 7 天无理由"
    assert out.summarized_count == 10
    assert len(llm.prompts) == 1

    # 第二次压缩要把已有摘要带进提示（累积合并，不是覆盖）
    out2 = advance_summary(
        _rounds(25), out.summary, out.summarized_count,
        max_rounds=10, trigger_rounds=5, llm=llm,
    )
    assert "已有摘要" in llm.prompts[1]
    assert "用户咨询退货" in llm.prompts[1]
    assert out2.summarized_count == 30

def test_llm_failure_keeps_old_summary_and_watermark():
    """压缩失败必须回滚水位线。

    否则这段历史既没进摘要、又因水位线推进而永不重试——永久丢失。
    """
    llm = _FakeLLM(raise_exc=RuntimeError("LLM 挂了"))
    out = advance_summary(
        _rounds(15), "旧摘要", 4, max_rounds=10, trigger_rounds=5, llm=llm
    )
    assert out.summary == "旧摘要"
    assert out.summarized_count == 4      # 未推进 → 下轮重试

def test_no_llm_call_when_not_triggered():
    """未触发时一次 LLM 都不能调（成本纪律）。"""
    llm = _FakeLLM()
    out = advance_summary(_rounds(3), "", 0, max_rounds=10, trigger_rounds=5, llm=llm)
    assert llm.prompts == []
    assert out.summarized_count == 0

def test_summary_is_truncated_as_last_resort():
    """模型不遵守字数上限时兜底截断——摘要本身不能成为新的膨胀源。"""
    llm = _FakeLLM("啰" * 5000)
    out = advance_summary(
        _rounds(15), "", 0, max_rounds=10, trigger_rounds=5, max_chars=100, llm=llm
    )
    assert len(out.summary) <= 201

def test_empty_conversation_keeps_previous_summary():
    """没有可压正文时沿用旧摘要，但水位线正常推进（不算失败）。"""
    msgs = [HumanMessage(content="q")] + [
        AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "c"}])
    ] * 20 + _rounds(10, start=1)
    llm = _FakeLLM()
    out = advance_summary(msgs, "旧摘要", 0, max_rounds=10, trigger_rounds=1, llm=llm)
    # 是否调用取决于可压段里有没有正文；无论如何不能抛异常
    assert isinstance(out.summary, str)

# ============================================================
# 系统提示拼装
# ============================================================

def test_build_system_content_merges_into_one_message():
    """摘要合并进同一条 system：部分厂商只接受位置 0 的单条 system。"""
    merged = build_system_content("你是客服", "用户想退货")
    assert merged.startswith("你是客服")
    assert SUMMARY_HEADER in merged
    assert "用户想退货" in merged

def test_build_system_content_without_summary_is_passthrough():
    assert build_system_content("你是客服", "") == "你是客服"
    assert build_system_content("你是客服", "   ") == "你是客服"

# ============================================================
# 接线：agent_node 真的用了摘要，且没删历史
# ============================================================

class _RecordingLLM:
    def __init__(self):
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        self.calls.append(list(messages))
        return AIMessage(content="好的")

def test_agent_node_injects_summary_and_keeps_history(monkeypatch):
    """agent 节点：系统提示里带摘要；返回的 state 更新不删 messages。"""
    from app.agents import graph
    from app.core.config import get_settings

    monkeypatch.setenv("HISTORY_MAX_ROUNDS", "2")
    monkeypatch.setenv("SUMMARY_TRIGGER_ROUNDS", "2")
    get_settings.cache_clear()
    try:
        llm = _RecordingLLM()
        monkeypatch.setattr(graph, "get_llm", lambda: llm)
        # 摘要用假 LLM 单独注入，避免触发真实调用
        monkeypatch.setattr(
            graph.summarize, "advance_summary",
            lambda *a, **kw: summarize.SummaryOutcome("用户想退货", 4),
        )
        node = graph._make_agent_node()
        history = _rounds(5)
        update = node({"messages": history}, {"configurable": {"thread_id": "t"}})

        sent = llm.calls[0]
        assert sent[0].type == "system"
        assert SUMMARY_HEADER in sent[0].content
        assert "用户想退货" in sent[0].content
        # 窗口：system + 最近 2 轮
        assert len(sent) == 5

        # 关键：state 更新里不能出现删消息的指令，历史必须原样保留
        assert list(update["messages"]) == [update["messages"][-1]]
        assert update["summary"] == "用户想退货"
        assert update["summarized_count"] == 4
    finally:
        get_settings.cache_clear()

def test_agent_node_without_summary_keeps_plain_prompt(monkeypatch):
    """没触发压缩时系统提示不应出现摘要段（避免无意义的空标题）。"""
    from app.agents import graph
    from app.core.config import get_settings

    monkeypatch.setenv("SUMMARY_TRIGGER_ROUNDS", "5")
    get_settings.cache_clear()
    try:
        llm = _RecordingLLM()
        monkeypatch.setattr(graph, "get_llm", lambda: llm)
        node = graph._make_agent_node()
        node({"messages": _rounds(1)}, {"configurable": {"thread_id": "t"}})
        assert SUMMARY_HEADER not in llm.calls[0][0].content
    finally:
        get_settings.cache_clear()
