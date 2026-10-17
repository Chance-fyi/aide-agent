"""LangGraph 客服 Agent（单 Agent + tool calling）。

架构（MVP 版，后续迭代会重构为 multi-agent）：

    START
      │
      ▼
   route_input（关键词兜底 + 待人工守卫）
      │
      ├── 命中转接关键词或已是 pending_human ──> transfer ──> END
      │
      └── 正常消息 ──> agent ⇄ tools ──> END

异常防护双保险设计：
1. LLM 自主判断：工具 bind 后由模型决定何时 transfer_to_human
2. 规则兜底：用户消息命中关键词（"转人工/投诉/退款电话"）时
   **跳过 LLM 直接转接**——防止模型漏判，也省一次 LLM 调用
3. 状态守卫：会话一旦 pending_human，后续消息不再进 agent，
   统一回复"坐席接入中"（坐席端在后续迭代实现）
"""

import logging
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from app.agents import handoff
from app.core.config import get_settings
from app.core.llm import get_llm

logger = logging.getLogger(__name__)

# 待人工状态下的统一话术（坐席端上线前的过渡回复）
_PENDING_HUMAN_REPLY = "您的工单已提交，人工客服正在接入，请稍候。客服会尽快与您联系。"


# ============================================================
# 工具定义
# ============================================================

def search_kb(query: str) -> str:
    """搜索客服知识库，返回最相关的知识片段。

    工具的 docstring 很关键：LLM 靠它理解"什么场景该调这个工具"。
    """
    from app.rag import store  # 延迟导入，避免循环依赖

    hits = store.search(query, top_k=get_settings().retrieval_top_k)
    if not hits:
        return "未找到相关知识。"

    # 把多个片段拼成一篇文章返回给 LLM，附带来源方便引用
    parts = [
        f"【{h['source']} | {h['h1']}/{h['h2']} | 相关度 {h['score']:.2f}】\n{h['content']}"
        for h in hits
    ]
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
2. 引用知识时说明依据（如"根据平台退货政策"）
3. 知识库没有的内容，坦诚告知并调用转人工工具，不要猜测
4. 语气友好专业，回复简洁，避免大段复述原文
5. 用户情绪激动（如投诉、与商家纠纷）时，主动转接人工
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

    1. 会话已是 pending_human → 不再打扰用户和 LLM，直接回复等待话术
    2. 消息命中转接关键词 → 规则兜底直接转接（跳过 LLM）
    3. 其他 → 正常走 agent
    """
    thread_id = config["configurable"]["thread_id"]

    # 守卫：待人工会话不再进 agent
    if handoff.get_status(thread_id) == handoff.STATUS_PENDING_HUMAN:
        return "transfer"

    # 关键词兜底：只检查本轮最新一条用户消息
    last_user = next(
        (m for m in reversed(state["messages"]) if m.type == "human"),
        None,
    )
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


def transfer_node(state: AgentState):
    """转接节点：生成统一的转接告知消息。

    状态落库已在 route_input / transfer_to_human 工具里完成，
    这里只负责给用户一句明确的话术。
    """
    return {"messages": [AIMessage(content=_PENDING_HUMAN_REPLY)]}


# ============================================================
# 组装图
# ============================================================

def build_graph_with_memory(checkpointer: PostgresSaver):
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
