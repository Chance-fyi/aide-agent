"""上下文窗口裁剪测试（不依赖 LLM / Postgres / Milvus）。

这是纯函数级测试：裁剪规则本身必须能在毫秒内验证，
否则只能靠跑长会话、看账单来猜它有没有生效。
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.graph import _window_messages


def _round(i: int) -> list:
    """构造一轮：用户提问 + 助手回答。"""
    return [HumanMessage(content=f"问题{i}"), AIMessage(content=f"回答{i}")]

def _rounds(n: int) -> list:
    """构造 n 轮完整对话。"""
    out = []
    for i in range(n):
        out.extend(_round(i))
    return out

def test_under_limit_returns_all():
    """轮数不足上限时全量送出——不能因为裁剪把仅有的上下文弄丢。"""
    msgs = _rounds(2)
    assert _window_messages(msgs, 10) == msgs

def test_exactly_at_limit_returns_all():
    """恰好等于上限也不裁剪（边界不应多切一轮）。"""
    msgs = _rounds(3)
    assert _window_messages(msgs, 3) == msgs

def test_over_limit_keeps_last_n_rounds():
    """超过上限：只保留最近 N 轮，且按原顺序。"""
    msgs = _rounds(5)
    got = _window_messages(msgs, 2)
    assert [m.content for m in got] == ["问题3", "回答3", "问题4", "回答4"]

def test_window_always_starts_with_human():
    """窗口首条必须是 HumanMessage。

    模型看到"从助手回答开始"的对话会混乱；更重要的是——
    若窗口从 ToolMessage 开始，就是孤立工具结果，接口直接报错。
    """
    got = _window_messages(_rounds(10), 3)
    assert got[0].type == "human"

def test_tool_chain_within_round_is_not_split():
    """一轮内的 tool_calls / ToolMessage 必须整体保留。

    tool_calls 与对应的 ToolMessage 必须成对出现，
    截断在中间会让 [OI] 兼容接口直接 400。
    因为切点永远在 human 消息上，一轮内部的工具往返天然完整。
    """
    msgs = [
        *_round(0),
        # 第 2 轮：用户问 → 助手调工具 → 工具结果 → 助手最终回答
        HumanMessage(content="问题1"),
        AIMessage(
            content="",
            tool_calls=[{"name": "search_kb", "args": {"query": "退货"}, "id": "c1"}],
        ),
        ToolMessage(content="知识库片段", tool_call_id="c1"),
        AIMessage(content="回答1"),
        *_round(2),
    ]
    got = _window_messages(msgs, 2)
    # 窗口应覆盖第 2、3 轮，且工具链三件套齐全
    assert [m.type for m in got] == ["human", "ai", "tool", "ai", "human", "ai"]
    assert got[1].tool_calls[0]["id"] == "c1"
    assert got[2].tool_call_id == "c1"

def test_zero_or_negative_means_unlimited():
    """0/负数 = 不限制：调试时想看全量历史，不该被配置卡住。"""
    msgs = _rounds(20)
    assert _window_messages(msgs, 0) == msgs
    assert _window_messages(msgs, -1) == msgs

def test_empty_messages():
    """空列表不能炸（新会话首轮）。"""
    assert _window_messages([], 10) == []

def test_does_not_mutate_input():
    """裁剪只返回窗口，不修改入参——state 里的完整历史必须原样保留。"""
    msgs = _rounds(5)
    snapshot = list(msgs)
    _window_messages(msgs, 1)
    assert msgs == snapshot

# ============================================================
# 接线测试：确认 agent 节点真的用了窗口
# ============================================================
# 只测 _window_messages 是不够的——本步要修的 bug 恰恰是
# "配置项存在、函数也写好了，但没有任何地方读它"。
# 所以这里必须验证：构建出来的 agent 节点送给 LLM 的消息确实被裁剪过。

class _RecordingLLM:
    """假 LLM：记录每次收到的消息，返回固定回复。"""

    def __init__(self):
        self.calls: list[list] = []

    def bind_tools(self, tools):
        # 真实 LLM 的 bind_tools 返回新实例；这里无所谓，返回自己即可
        return self

    def invoke(self, messages, **kwargs):
        self.calls.append(list(messages))
        return AIMessage(content="好的")

def _agent_node_with(monkeypatch, llm) -> object:
    """用假 LLM 构造 agent 节点。"""
    from app.agents import graph

    monkeypatch.setattr(graph, "get_llm", lambda: llm)
    return graph._make_agent_node()

def test_agent_node_sends_only_recent_rounds(monkeypatch):
    """agent 节点必须把历史裁剪到 history_max_rounds 轮再送 LLM。"""
    from app.core.config import get_settings

    monkeypatch.setenv("HISTORY_MAX_ROUNDS", "2")
    get_settings.cache_clear()  # 配置有 lru_cache，改环境变量后必须清
    try:
        llm = _RecordingLLM()
        node = _agent_node_with(monkeypatch, llm)
        node({"messages": _rounds(5)}, {"configurable": {"thread_id": "t"}})

        sent = llm.calls[0]
        # 1 条系统提示 + 最近 2 轮（4 条）= 5 条
        assert len(sent) == 5
        assert sent[0].type == "system"
        assert [m.content for m in sent[1:]] == ["问题3", "回答3", "问题4", "回答4"]
    finally:
        get_settings.cache_clear()

def test_agent_node_always_keeps_system_prompt(monkeypatch):
    """裁剪绝不能把系统提示裁掉——那是人设与行为准则的唯一来源。"""
    from app.core.config import get_settings

    monkeypatch.setenv("HISTORY_MAX_ROUNDS", "1")
    get_settings.cache_clear()
    try:
        llm = _RecordingLLM()
        node = _agent_node_with(monkeypatch, llm)
        node({"messages": _rounds(9)}, {"configurable": {"thread_id": "t"}})

        sent = llm.calls[0]
        assert sent[0].type == "system"
        assert "小舒" in sent[0].content
        assert len(sent) == 3  # system + 最近 1 轮
    finally:
        get_settings.cache_clear()
