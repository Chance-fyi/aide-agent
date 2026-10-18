"""LangGraph 客服 Agent（单 Agent + tool calling）。

架构（MVP 版，后续迭代会重构为 multi-agent）：

    START
      │
      ▼
   route_input（关键词兜底 + 待人工守卫）
      │
      ├── 命中转接关键词或已在排队/服务中 ──> transfer ──> END
      │
      └── 正常消息 ──> agent ⇄ tools ──> END

异常防护双保险设计：
1. LLM 自主判断：工具 bind 后由模型决定何时 transfer_to_human
2. 规则兜底：用户消息命中关键词（"转人工/投诉/退款电话"）时
   **跳过 LLM 直接转接**——防止模型漏判，也省一次 LLM 调用
3. 状态守卫：会话一旦进入排队/服务中，后续消息不再进 agent，
   统一回复对应话术（坐席端在后续迭代实现）
"""

import logging
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from app.agents import handoff
from app.core.config import get_settings
from app.core.llm import get_llm

logger = logging.getLogger(__name__)

# 待人工状态下的统一话术（坐席端上线前的过渡回复）
_PENDING_HUMAN_REPLY = "您的工单已提交，人工客服正在接入，请稍候。客服会尽快与您联系。"
# 大段统回复：坐席正在服务中的话术（与"排队中"区分，让用户知道进展）
_IN_SERVICE_REPLY = "人工客服正在为您服务，请直接描述您的问题。"


# ============================================================
# 共用小工具
# ============================================================

def _get_thread_status(thread_id: str, fallback: str) -> str:
    """查询会话转接状态，查询失败降级为 fallback。

    状态查询失败不能拖垮对话（Postgres 抖动时用户还能继续问答），
    由调用方按场景传入保守降级值。
    """
    try:
        return handoff.get_status(thread_id)
    except Exception:
        logger.exception("转接状态查询失败，按 %s 处理 thread=%s", fallback, thread_id)
        return fallback


def _last_text_message(messages: list[AnyMessage], msg_type: str) -> AnyMessage | None:
    """从消息列表末尾向前找第一条指定类型且有文本内容的消息；没有则 None。"""
    return next(
        (m for m in reversed(messages) if m.type == msg_type and m.text().strip()),
        None,
    )

# ============================================================
# 工具定义
# ============================================================

def search_kb(query: str) -> str:
    """搜索客服知识库，返回最相关的知识片段。

    工具的 docstring 很关键：LLM 靠它理解"什么场景该调这个工具"。
    """
    from app.rag import store  # 延迟导入，避免循环依赖

    try:
        hits = store.search(query, top_k=get_settings().retrieval_top_k)
    except Exception:
        # 降级：知识库故障（Milvus 挂掉/未建库）是最常见的局部故障，
        # 不应拖垮整个对话。把故障告知作为工具结果交还 LLM，
        # 由它向用户致歉并引导转人工——对话继续、故障可控
        logger.exception("search_kb 检索失败 query=%r", query)
        return (
            "【系统提示】知识库暂时无法访问。请向用户致歉并说明系统繁忙、"
            "暂时无法查询知识库，建议稍后重试；如用户急需帮助，"
            "请调用 transfer_to_human 转接人工客服。"
        )

    if not hits:
        # 无命中/命中全部低于阈值：明确告诉 LLM"没有资料"，
        # 配合系统提示词的"禁止编造"规则，引导转人工而不是硬答
        return (
            "【系统提示】知识库中没有与该问题相关的内容。请坦诚告知用户"
            "暂时没有查到相关资料，不要猜测或编造答案；"
            "如用户仍需解决该问题，请调用 transfer_to_human 转接人工客服。"
        )

    # 父子检索（small-to-big）：用短子块检索保证"找得准"，
    # 用长父块喂 LLM 保证"答得全"。多个子块命中同一小节时去重，
    # 避免同一政策的不同片段重复占用上下文
    seen_parents: set[tuple] = set()
    parts: list[str] = []
    for h in hits:
        parent = h["parent_content"]
        key = (h["source"], h["h1"], h["h2"], parent[:64])
        if key in seen_parents:
            continue
        seen_parents.add(key)
        parts.append(
            f"【{h['source']} | {h['h1']}/{h['h2']} | 相关度 {h['score']:.2f}】\n{parent}"
        )
    return "\n\n---\n\n".join(parts)


def transfer_to_human(reason: str, config: RunnableConfig) -> str:
    """转接人工客服。当判断无法自助解决问题，或用户明确要求人工时调用。

    注意 config 参数：LangGraph 的 ToolNode 会自动注入运行时配置，
    不占用 LLM 的参数位——用它可以拿到 thread_id 做业务落库。
    """
    thread_id = config["configurable"]["thread_id"]
    handoff.mark_pending_human(thread_id, reason=f"LLM判断: {reason}")
    return f"已发起人工转接，原因：{reason}"


_TOOLS = [search_kb, transfer_to_human]

# 系统提示词：客服 Agent 的人设与行为约束
_SYSTEM_PROMPT = """你是电商平台"舒心购"的智能客服助手，名叫小舒。

职责：
- 基于知识库回答售前售后问题（退货、退款、物流、支付、会员、优惠券等）
- 无法解决或用户要求人工时，调用 transfer_to_human 工具转接人工客服

行为准则：
1. 回答必须基于 search_kb 检索到的知识，禁止编造政策细节
2. 引用知识时必须声明依据（如"根据平台退货政策"）；检索不到依据的细节一律不说
3. 知识库没有的内容，坦诚告知并调用转人工工具，不要猜测
4. 语气友好专业，回复简洁，避免大段复述原文
5. 用户情绪激动（如投诉、与商家纠纷）时，主动转接人工
6. 只回答电商售前售后相关问题（退货、退款、物流、支付、会员、优惠券等）。
   与平台业务无关的话题（闲聊、时事、代码、医疗/法律/投资建议等），
   礼貌说明"我是舒心购的售前售后客服，这方面帮不了您"，
   并引导用户回到购物相关问题，或询问是否需要转接人工客服
7. 涉及违法违规、危险行为、绕过平台规则的请求（如刷单、站外交易、套取优惠），
   明确拒绝，不提供任何帮助，也不展开讨论
"""


# ============================================================
# 图状态定义
# ============================================================

class AgentState(TypedDict):
    """图的状态：节点间流转的数据。

    messages 用 add_messages reducer：新消息**追加**而非覆盖，
    这是多轮对话记忆的基础。
    """

    messages: Annotated[list[AnyMessage], add_messages]


# ============================================================
# 路由与守卫
# ============================================================

def route_input(state: AgentState, config: RunnableConfig) -> str:
    """入口路由：三级判断（顺序即优先级）。

    1. 会话已在排队/坐席服务中 → 不再打扰用户和 LLM，直接回复对应话术
    2. 消息命中转接关键词 → 规则兜底直接转接（跳过 LLM）
    3. 其他 → 正常走 agent
    """
    thread_id = config["configurable"]["thread_id"]

    # 守卫：受转接影响的会话不再进 agent（排队中/坐席服务中）。
    # 降级为 active——代价是极端情况下已转接会话多问一轮，可接受
    status = _get_thread_status(thread_id, handoff.STATUS_ACTIVE)
    if handoff.is_live_status(status):
        return "transfer"

    if status == handoff.STATUS_CLOSED:
        # closed 会话来了新消息：视为新问题回自助，同时复位状态行。
        # 复位失败不影响本轮（closed 不在守卫集合里，agent 照常执行）
        try:
            handoff.reset_active(thread_id)
        except Exception:
            logger.exception("复位 closed 会话失败 thread=%s", thread_id)

    # 关键词兜底：只检查本轮最新一条用户消息
    last_user = _last_text_message(state["messages"], "human")
    if last_user:
        text = last_user.content
        if any(kw in text for kw in get_settings().transfer_keyword_list):
            logger.info("关键词兜底触发转接: thread=%s", thread_id)
            handoff.mark_pending_human(thread_id, reason=f"关键词命中: {text[:50]}")
            return "transfer"

    return "agent"


# ============================================================
# 节点定义
# ============================================================

def _make_agent_node():
    """构造 agent 节点（闭包持有绑定了工具的 LLM）。

    bind_tools：把工具的 JSON Schema 告诉 LLM，
    LLM 决定是否调用（返回 tool_calls）——tool calling 是"模型能力"，
    LangGraph 负责"执行工具并回填结果"。
    """
    llm_with_tools = get_llm().bind_tools(_TOOLS)

    def agent_node(state: AgentState):
        # 每次都在消息最前面注入系统提示（系统提示不进历史，避免 Checkpointer 里膨胀）
        messages = [SystemMessage(content=_SYSTEM_PROMPT)] + state["messages"]
        return {"messages": [llm_with_tools.invoke(messages)]}

    return agent_node


def transfer_node(state: AgentState, config: RunnableConfig):
    """转接节点：按当前状态生成对应话术。

    排队中与坐席服务中给用户不同反馈，避免"一直等待"的困惑。
    注意：这里不推进状态——in_service 只能由坐席接入动作产生
    （handoff.agent_accept），否则"排队"就名存实亡了。
    """
    thread_id = config["configurable"]["thread_id"]
    # 状态不可知时按排队处理，话术偏保守
    status = _get_thread_status(thread_id, handoff.STATUS_QUEUED)

    # 坐席已接入后的重复提醒只会刷屏：首次接入时提示一次，
    # 之后不再追加提示消息（本轮直接不产生回复）。
    # 实现：检查上一条 assistant 消息是否已是接入提示，是则空回复；
    # add_messages reducer 对空列表是追加操作，写空列表即"本轮不产生新消息"
    if status == handoff.STATUS_IN_SERVICE:
        last_assistant = _last_text_message(state["messages"], "ai")
        if last_assistant and last_assistant.text().strip() == _IN_SERVICE_REPLY:
            return {"messages": []}
        return {"messages": [AIMessage(content=_IN_SERVICE_REPLY)]}

    return {"messages": [AIMessage(content=_PENDING_HUMAN_REPLY)]}


# ============================================================
# 组装图
# ============================================================

def build_graph_with_memory(checkpointer: AsyncPostgresSaver):
    """构建带 Postgres Checkpointer 的图（生产入口）。

    checkpointer 的作用：每次图执行后把完整状态（含全部消息）存入 Postgres，
    下次带同一 thread_id 请求时自动恢复历史——多轮记忆无需手工拼 prompt。
    """
    g = StateGraph(AgentState)
    g.add_node("agent", _make_agent_node())
    g.add_node("tools", ToolNode(_TOOLS))
    g.add_node("transfer", transfer_node)

    # 入口条件路由：route_input 的返回值决定第一个节点（agent 或 transfer）
    g.add_conditional_edges(START, route_input, {"agent": "agent", "transfer": "transfer"})
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")

    return g.compile(checkpointer=checkpointer)
