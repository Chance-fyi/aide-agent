"""上下文压缩：把"已经滑出窗口的旧对话"压成一段摘要。

**为什么不直接用框架的**

- `langchain_core.messages.trim_messages`：只做裁剪，不做压缩。
  它能把旧消息丢掉，但丢掉的信息就真没了（多轮指代会失效）。
- `langmem` 的 `SummarizationNode`：能压缩，但依赖链重
  （`langchain` 全量 + `langchain-anthropic` + `trustcall` + `langsmith`），
  版本还是 0.0.x；且它用 `RemoveMessage` **真删 state 里的消息**，
  而摘要是 LLM 产出的有损派生物（可能丢关键事实、也可能被用户诱导固化错误），
  删掉原文就失去了回滚能力；另外 `chat.py` 在 `chat_message` 为空时会回退读
  Checkpointer 快照（存量会话兜底），删了会让那些会话历史全空。
- `langchain` 1.x 的 `SummarizationMiddleware`：只能挂在 `create_agent` 上，
  而本项目是手写 `StateGraph`（需要三态 `route_input` 与 `transfer_node`）。

所以自研一个约 150 行的节点级实现：只依赖 `langchain_core` 的消息类型，
摘要存进独立 state 键，**不动 messages**。

**分层设计（为可测性）**

- `plan_summary()`：纯函数，决定"要不要压缩、压缩哪一段"——无 IO，可单测
- `summarize_messages()`：唯一有 IO 的一步（调 LLM），失败降级为旧摘要

把决策与执行拆开，是因为触发条件/水位线这类逻辑最容易写错，
而它恰好完全不需要 LLM 就能验证。
"""

import logging
from dataclasses import dataclass

from langchain_core.messages import AnyMessage, HumanMessage

logger = logging.getLogger(__name__)

# 摘要消息的前缀：让模型清楚"这是压缩过的历史，不是本轮用户发言"
SUMMARY_HEADER = "【历史对话摘要】"

# 工具结果在压缩提示里的截断长度。
# 为什么要截：search_kb 返回的是整段父块正文（可达上千字），
# 原样塞进压缩提示会让"压缩"本身变成一次昂贵调用，得不偿失。
# 压缩只需要知道"检索到哪份资料"，不需要全文
_TOOL_PREVIEW_CHARS = 200

_SUMMARY_PROMPT = """你负责压缩客服对话历史。把下面的对话压缩成一段简洁的中文摘要，供后续对话参考。

要求：
1. 只保留对后续对话有用的信息：用户的关键事实（身份、订单、商品）、已确认的诉求、
   已经给出的结论与承诺、用户明确表达的偏好
2. 丢弃寒暄、重复内容、以及被后续对话否定的信息
3. 不要编造对话中没有出现的内容
4. 直接输出摘要正文，不要加"摘要："之类的开头，不要分点编号
5. 总长度不超过 {max_chars} 字

{previous_block}【待压缩对话】
{conversation}
"""

# ============================================================
# 窗口边界（与 graph._window_messages 共用同一套语义）
# ============================================================

def window_start_index(messages: list[AnyMessage], max_rounds: int) -> int:
    """最近 max_rounds 轮的起始下标（窗口左边界）。

    在 human 消息边界切，保证：窗口首条是用户发言；
    且一轮内部的 tool_calls/ToolMessage 不会被拆散
    （拆散会产出孤立 ToolMessage，接口直接 400）。
    max_rounds <= 0 表示不限制，返回 0。
    """
    if max_rounds <= 0:
        return 0
    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].type == "human":
            seen += 1
            if seen == max_rounds:
                return i
    return 0

def _rounds_in(messages: list[AnyMessage]) -> int:
    """一段消息里包含几轮对话（按 human 消息数计）。"""
    return sum(1 for m in messages if m.type == "human")

# ============================================================
# 决策：纯函数
# ============================================================

@dataclass(frozen=True)
class SummaryPlan:
    """一次压缩动作的计划。

    should_run=False 时其余字段无意义——调用方直接沿用旧摘要与旧水位线。
    """

    should_run: bool
    to_summarize: tuple[AnyMessage, ...] = ()
    # 压缩完成后应写入的"已压缩消息条数"（水位线）。
    # 存条数而不是存轮数/时间戳：messages 是 append-only 的，
    # 条数是唯一不会随重放而漂移的锚点
    next_summarized_count: int = 0

def plan_summary(
    messages: list[AnyMessage],
    summarized_count: int,
    max_rounds: int,
    trigger_rounds: int,
) -> SummaryPlan:
    """决定本轮是否需要压缩，以及压缩哪一段。

    触发条件：**已滑出窗口、但还没被压缩过**的轮数 >= trigger_rounds。

    为什么要"攒够一批再压"而不是滑出一轮就压：
    每次压缩都是一次额外 LLM 调用。逐轮压缩会让长会话的成本翻倍；
    攒 5 轮压一次，成本降一个数量级，而信息损失可忽略
    （被延迟压缩的旧消息本来就已经不在窗口里了）。

    trigger_rounds <= 0 视为关闭压缩，永远不触发。
    """
    if trigger_rounds <= 0:
        return SummaryPlan(should_run=False)

    start = window_start_index(messages, max_rounds)
    # 水位线已越过窗口边界，或水位线异常超前：本轮无新内容可压
    if summarized_count >= start:
        return SummaryPlan(should_run=False)

    pending = messages[summarized_count:start]
    if _rounds_in(pending) < trigger_rounds:
        return SummaryPlan(should_run=False)

    return SummaryPlan(
        should_run=True,
        to_summarize=tuple(pending),
        next_summarized_count=start,
    )

# ============================================================
# 执行：唯一有 IO 的一步
# ============================================================

def render_conversation(messages: list[AnyMessage]) -> str:
    """把消息渲染成压缩提示用的纯文本。

    只取 human / ai 的正文，tool 结果截断预览：
    - ai 消息可能只有 tool_calls 没有正文（中间步骤），跳过避免空行噪音
    - tool 结果是检索到的知识原文，对"记住用户问过什么"帮助有限，截断即可
    """
    lines: list[str] = []
    for m in messages:
        if m.type == "human":
            lines.append(f"用户: {m.text}")
        elif m.type == "ai" and m.text.strip():
            lines.append(f"客服: {m.text}")
        elif m.type == "tool":
            preview = m.text.strip().replace("\n", " ")[:_TOOL_PREVIEW_CHARS]
            if preview:
                lines.append(f"[工具结果] {preview}")
    return "\n".join(lines)

def summarize_messages(
    previous_summary: str,
    messages: list[AnyMessage],
    max_chars: int = 600,
    llm=None,
) -> str | None:
    """调 LLM 把 messages 压进摘要，与已有摘要合并。

    返回值语义：
    - str：新摘要（可能为空串，表示 LLM 认为没有值得保留的信息）
    - None：**压缩失败**。调用方必须据此保持旧摘要与旧水位线不变，
      绝不能把水位线推上去——否则这段历史既没进摘要、又不再重试，
      等于永久丢失

    失败只记日志不抛异常：摘要属于"锦上添花"，
    它挂掉不能让整轮对话失败（降级后等价于 step-35 的纯窗口裁剪）。
    """
    conversation = render_conversation(messages)
    if not conversation.strip():
        # 没有可压的正文（比如全是空正文的 tool_calls 消息）：
        # 直接返回旧摘要，但不算失败——水位线可以正常推进
        return previous_summary

    previous_block = ""
    if previous_summary.strip():
        previous_block = (
            f"【已有摘要（请把新对话合并进去，整体保持简洁）】\n{previous_summary}\n\n"
        )

    prompt = _SUMMARY_PROMPT.format(
        max_chars=max_chars,
        previous_block=previous_block,
        conversation=conversation,
    )

    try:
        if llm is None:
            from app.core.llm import get_llm  # 延迟导入：单测可完全绕开真实客户端

            llm = get_llm()
        reply = llm.invoke([HumanMessage(content=prompt)])
        text = reply.text if hasattr(reply, "text") else str(getattr(reply, "content", ""))
        text = text.strip()
    except Exception:
        logger.exception("对话摘要生成失败，降级为旧摘要")
        return None

    # 兜底截断：模型不一定严格遵守 max_chars。
    # 摘要本身无限增长会让"压缩"变成新的上下文膨胀源
    if len(text) > max_chars * 2:
        text = text[: max_chars * 2] + "…"
    return text

def build_system_content(base_prompt: str, summary: str) -> str:
    """把摘要拼进系统提示。

    为什么不单独发一条 SystemMessage：部分 OAI 兼容厂商只接受
    位置 0 的一条 system 消息，多条会被拒或行为不一致。
    合并成一条最稳，也顺带保证摘要不会被后续裁剪逻辑挤掉。
    """
    if not summary.strip():
        return base_prompt
    return f"{base_prompt}\n\n{SUMMARY_HEADER}\n{summary}"

# ============================================================
# 组合入口：给 graph 节点用
# ============================================================

@dataclass(frozen=True)
class SummaryOutcome:
    summary: str
    summarized_count: int

def advance_summary(
    messages: list[AnyMessage],
    previous_summary: str,
    summarized_count: int,
    max_rounds: int,
    trigger_rounds: int,
    max_chars: int = 600,
    llm=None,
) -> SummaryOutcome:
    """按计划执行压缩；不需要压缩或压缩失败时原样返回。

    这是 graph 节点唯一需要调用的函数：把"决策 + 执行 + 失败回滚"封装好，
    节点本身只关心"拿到新的 (summary, summarized_count)"。
    """
    plan = plan_summary(messages, summarized_count, max_rounds, trigger_rounds)
    if not plan.should_run:
        return SummaryOutcome(previous_summary, summarized_count)

    new_summary = summarize_messages(
        previous_summary, list(plan.to_summarize), max_chars=max_chars, llm=llm
    )
    if new_summary is None:
        # 失败：水位线不动，下一轮重试（此时行为等价于纯窗口裁剪）
        return SummaryOutcome(previous_summary, summarized_count)

    logger.info(
        "对话历史已压缩 %d 条消息 → 摘要 %d 字（累计水位线 %d）",
        len(plan.to_summarize),
        len(new_summary),
        plan.next_summarized_count,
    )
    return SummaryOutcome(new_summary, plan.next_summarized_count)
