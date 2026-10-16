"""LangGraph 客服 Agent（单 Agent + tool calling）。

架构（MVP 版，后续迭代会重构为 multi-agent）：

    START
      │
      ▼
    agent ──决定调用工具──> tools ──工具结果──> agent（循环）
      │                       │
      │（无工具调用，直接回答）  │（transfer_to_human 被调用）
      ▼                       ▼
    END                  interrupt_to_human（中断标记，步骤 7 细化）

核心概念：
- StateGraph：以 TypedDict 状态为核心的图，节点读写状态，边决定流转
- ToolNode：工具执行节点；agent 节点的 LLM 绑定了工具后，
  会返回 tool_calls，条件边据此决定去 tools 还是结束
- MemorySaver → PostgresCheckpointer：会话状态持久化，
  thread_id 即会话 ID，多轮记忆由框架自动管理（这是 LangGraph 招牌能力）
"""

import logging
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt

from app.core.config import get_settings
from app.core.llm import get_llm

logger = logging.getLogger(__name__)


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


def transfer_to_human(reason: str) -> str:
    """转接人工客服。当判断无法自助解决问题，或用户明确要求人工时调用。"""
    # 步骤 7 会在这里做状态机（标记会话为 pending_human 等）
    # 现在先返回确认信息，让 LLM 能组织出"已为您转接"的回复
    return f"已发起人工转接，原因：{reason}"


# 工具注册表：ToolNode 按函数名自动分发
_TOOLS = [search_kb, transfer_to_human]

# 系统提示词：客服 Agent 的人设与行为约束
_SYSTEM_PROMPT = """你是电商平台"舒心购"的智能客服助手，名叫小舒。

职责：
- 基于知识库回答售前售后问题（退货、退款、物流、支付、会员、优惠券等）
- 无法解决或用户要求人工时，转接人工客服

行为准则：
1. 回答必须基于 search_kb 检索到的知识，禁止编造政策细节
2. 引用知识时说明依据（如"根据平台退货政策"）
3. 知识库没有的内容，坦诚告知并建议转人工，不要猜测
4. 语气友好专业，回复简洁，避免大段复述原文
5. 用户情绪激动（如投诉、与商家纠纷）时，主动提出转接人工
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


def _make_transfer_node():
    """转接节点：人工介入中断点。

    interrupt() 是 LangGraph 的人工介入（human-in-the-loop）机制：
    图在此暂停，状态持久化；后续可以用 Command(resume=...) 恢复，
    这为步骤 7 的"坐席接入后继续会话"留好了框架级支持。
    """

    def transfer_node(state: AgentState):
        # 取最后一个 ToolMessage 的内容作为转接原因
        reason = next(
            (m.content for m in reversed(state["messages"]) if isinstance(m, ToolMessage)),
            "用户需要人工服务",
        )
        logger.info("人工转接触发: %s", reason)
        return {"messages": [ToolMessage(content=str(reason), name="transfer_to_human", tool_call_id="transfer")]}

    return transfer_node


# ============================================================
# 组装图
# ============================================================

def build_graph():
    """构建并编译客服 Agent 图（不绑定 Checkpointer 的版本，便于测试）。"""
    g = StateGraph(AgentState)

    g.add_node("agent", _make_agent_node())
    g.add_node("tools", ToolNode(_TOOLS))

    g.add_edge(START, "agent")
    # 条件边：LLM 返回 tool_calls 则去 tools 节点，否则结束
    # （tools_condition 是官方内置的判断函数）
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    # 工具执行完回到 agent：LLM 拿着工具结果继续决策（可能继续调工具，也可能直接回答）
    g.add_edge("tools", "agent")

    return g.compile()


def build_graph_with_memory(checkpointer):
    """构建带 Postgres Checkpointer 的图（生产入口）。

    checkpointer 的作用：每次图执行后把完整状态（含全部消息）存入 Postgres，
    下次带同一 thread_id 请求时自动恢复历史——多轮记忆无需手工拼 prompt。
    """
    g = StateGraph(AgentState)
    g.add_node("agent", _make_agent_node())
    g.add_node("tools", ToolNode(_TOOLS))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
