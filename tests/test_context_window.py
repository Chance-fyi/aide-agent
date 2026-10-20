"""上下文窗口测试（不依赖 LLM / Postgres / Milvus）。

窗口语义（step-37 起）：**锚定压缩水位线，不滑动**。
本文件重点锁住两件事：

1. 边界正确性：切点落在 human 上、不拆散 tool 链、越界夹紧
2. **前缀稳定性**：压缩间隔内每轮请求都是上一轮的严格延长
   ——这是前缀缓存（prompt caching）能命中的前提，
   也是最容易被后续改动悄悄破坏的性质
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.graph import _window_messages


def _round(i: int) -> list:
    """构造一轮：用户提问 + 助手回答。"""
    return [HumanMessage(content=f"问题{i}"), AIMessage(content=f"回答{i}")]

def _rounds(n: int, start: int = 0) -> list:
    out = []
    for i in range(start, start + n):
        out.extend(_round(i))
    return out

# ============================================================
# 边界正确性
# ============================================================

def test_unlimited_returns_all():
    """max_rounds <= 0 = 不限制（调试场景）。"""
    msgs = _rounds(20)
    assert _window_messages(msgs, 0) == msgs
    assert _window_messages(msgs, -1) == msgs

def test_window_starts_at_watermark():
    """窗口左边界就是水位线（已压进摘要的位置）。"""
    msgs = _rounds(10)
    got = _window_messages(msgs, 3, summarized_count=8)
    # 水位线 8 → 从第 4 轮开始（每轮 2 条），共剩 6 轮 = 12 条
    assert len(got) == 12
    assert got[0].content == "问题4"
    assert got[-1].content == "回答9"

def test_zero_watermark_means_full_history():
    """还没压过（水位线 0）= 全量发送。

    这是缓存最友好的形态：窗口只增不减。
    """
    msgs = _rounds(10)
    assert _window_messages(msgs, 3, summarized_count=0) == msgs

def test_watermark_out_of_range_is_clamped():
    """水位线异常（超出列表长度）不能切片越界报错。"""
    msgs = _rounds(3)
    assert _window_messages(msgs, 3, summarized_count=999) == []
    assert _window_messages(msgs, 3, summarized_count=-5) == msgs

def test_tool_chain_is_never_split():
    """tool_calls 与 ToolMessage 必须成对。

    因为水位线只在 human 边界推进（window_start_index 保证），
    窗口起点永远是 human 消息，一轮内部的工具往返天然完整。
    """
    msgs = [
        HumanMessage(content="q0"),
        AIMessage(content="", tool_calls=[{"name": "search_kb", "args": {}, "id": "c1"}]),
        ToolMessage(content="片段", tool_call_id="c1"),
        AIMessage(content="a0"),
        HumanMessage(content="q1"),
        AIMessage(content="a1"),
    ]
    # 水位线在任何合法的 human 边界上，窗口首条都必须是 human
    for wm in (0, 4):
        got = _window_messages(msgs, 1, summarized_count=wm)
        assert got[0].type == "human"

def test_empty_messages():
    assert _window_messages([], 10) == []

def test_does_not_mutate_input():
    """只返回窗口，不修改入参——state 里的完整历史必须原样保留。"""
    msgs = _rounds(5)
    snapshot = list(msgs)
    _window_messages(msgs, 2, summarized_count=4)
    assert msgs == snapshot

# ============================================================
# 前缀稳定性（缓存友好性）
# ============================================================

def _common_prefix(a: list, b: list) -> int:
    k = 0
    while k < min(len(a), len(b)) and a[k].content == b[k].content:
        k += 1
    return k

def test_sliding_window_would_break_cache():
    """反例：滑动窗口（取最近 N 轮）每轮整体左移，公共前缀几乎为零。

    这条测试把\"为什么不能用滑动窗口\"固化成可执行的证据，
    而不是只写在注释里。
    """
    max_rounds = 3
    prev = None
    for n in range(4, 8):
        msgs = _rounds(n)
        # 模拟旧实现：滑动取最近 max_rounds 轮
        from app.agents.summarize import window_start_index

        window = msgs[window_start_index(msgs, max_rounds):]
        if prev is not None:
            # 相邻两轮的公共前缀只有 0 条（内容完全不同）
            assert _common_prefix(prev, window) == 0
        prev = window

def test_anchored_window_grows_monotonically_without_compression():
    """锚定水位线后：未压缩期间窗口只增不减，前缀 100% 命中。"""
    prev = None
    for n in range(1, 8):
        msgs = _rounds(n)
        window = _window_messages(msgs, 3, summarized_count=0)   # 水位线不动
        if prev is not None:
            # 上一轮的整个窗口都是这一轮的前缀
            assert _common_prefix(prev, window) == len(prev)
        prev = window

def test_prefix_preserved_between_compressions():
    """压缩间隔内前缀稳定，只在压缩发生的那一轮失效。

    真实场景（N=3, trigger=2）的序列：
        第4轮 [q0,a0,q1,a1,q2,a2,q3,a3]
        第5轮 [q2,a2,q3,a3,q4,a4]       ← 压缩，前缀重置
        第6轮 [q2,a2,q3,a3,q4,a4,q5,a5] ← 完整命中第5轮
        第7轮 [q4,a4,...]                ← 又压缩
    """
    from app.agents.summarize import plan_summary

    max_rounds, trigger = 3, 2
    watermark = 0
    prev = None
    resets = 0

    for n in range(1, 13):
        msgs = _rounds(n)
        plan = plan_summary(msgs, watermark, max_rounds, trigger)
        if plan.should_run:
            watermark = plan.next_summarized_count
            resets += 1
        window = _window_messages(msgs, max_rounds, summarized_count=watermark)

        if prev is not None:
            common = _common_prefix(prev, window)
            if common < len(prev):
                # 前缀被打断 → 必须是\"本轮刚压缩\"，不允许无故漂移
                assert plan.should_run, f"第{n}轮前缀无故失效"
        prev = window

    # 12 轮、窗口 3、攒 2 → 应该压缩了若干次（且远少于 12 次）
    assert 2 <= resets <= 6

def test_window_size_stays_within_target_range():
    """窗口轮数在 [max_rounds, max_rounds+trigger] 之间——这是\"窗口略大\"的代价。"""
    from app.agents.summarize import plan_summary, window_start_index

    max_rounds, trigger = 3, 2
    watermark = 0
    for n in range(1, 15):
        msgs = _rounds(n)
        plan = plan_summary(msgs, watermark, max_rounds, trigger)
        if plan.should_run:
            watermark = plan.next_summarized_count
        window = _window_messages(msgs, max_rounds, summarized_count=watermark)
        rounds = sum(1 for m in window if m.type == "human")
        assert rounds <= max_rounds + trigger, f"第{n}轮窗口 {rounds} 轮超出上限"
        # 未压缩时水位线不动，窗口可能超过目标；压缩后回到目标
        if plan.should_run:
            assert rounds == max_rounds
    # 顺带确认边界函数本身仍按轮数工作
    assert window_start_index(_rounds(5), max_rounds) == 4
