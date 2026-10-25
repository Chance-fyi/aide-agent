"""Agent 调用层：非流式 / 流式两种执行方式 + LLM 异常翻译。

**为什么从 chat.py 抽出来**

`app/api/chat.py` 原本 665 行，混了四类职责：LLM 执行、异常翻译、
会话记录读写、HTTP 路由。其中"怎么调 Agent、怎么把异常翻译成用户话术"
与 HTTP 完全无关（WS 入口也在用），单独成模块后：

- `chat.py` 只留"一轮对话的业务编排"（守卫 → 落库 → 调用 → 推送）
- 本模块只管"怎么执行 Agent、怎么把失败讲清楚"
- 想换执行方式（比如接入别的框架）只改这里

**异常翻译为什么用一张表**

原先 `_friendly_llm_error` 与 `_error_kind` 各写了一遍 isinstance 判断链，
两者的顺序必须一致（`APITimeoutError` 是 `APIConnectionError` 的子类，
先判谁决定结果）。改一处忘另一处，就会出现"指标归类"与"用户文案"对不上。
合并成一张有序表后，顺序只存在一处。
"""

import logging
import time
from collections.abc import Callable

import openai
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.runtime import get_graph
from app.core import metrics, tracing

logger = logging.getLogger(__name__)

# LLM 生成空回复时的兜底话术（前端至少能看到"失败了"，而不是空白气泡）
EMPTY_REPLY = "抱歉，刚才的回复没有生成成功，请您再试一次。"

# LLM 异常的**唯一**分类表：(异常类型, 指标标签, 用户可读文案)。
# 顺序敏感：子类必须排在父类之前。
# 原则：对外只说"发生了什么类别的问题 + 该怎么办"，
# 绝不泄露 base_url / 模型名 / 堆栈等内部细节；细节进日志。
_LLM_ERRORS: tuple[tuple[type[BaseException], str, str], ...] = (
    (openai.APITimeoutError, "timeout", "AI 服务响应超时，请稍后重试"),
    (openai.APIConnectionError, "connection", "暂时无法连接 AI 服务，请稍后重试"),
    (openai.RateLimitError, "ratelimit", "当前咨询人数较多，请稍后重试"),
    (openai.AuthenticationError, "auth", "客服服务暂时不可用，请稍后重试"),
)
_LLM_ERROR_FALLBACK = ("other", "客服服务暂时不可用，请稍后重试")

class AgentError(Exception):
    """Agent 执行失败（对外已翻译成用户可读文案）。

    为什么不用 HTTPException：执行层不该知道 HTTP 语义——WS 入口拿到的
    是事件不是状态码。由各自的入口层决定怎么呈现（REST → 503，WS → error 事件）。
    """

    def __init__(self, message: str, kind: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code

def classify_llm_error(exc: Exception) -> tuple[str, str]:
    """异常 → (指标标签, 用户可读文案)。"""
    for exc_type, kind, message in _LLM_ERRORS:
        if isinstance(exc, exc_type):
            if kind == "auth":
                # 配置错误（Key 失效）属于服务端问题，对用户话术与普通故障一致，
                # 但日志要打 CRITICAL 提醒运维，这是"必须马上修"的故障
                logger.critical("LLM 认证失败，请检查 LLM_API_KEY 配置")
            return kind, message
    return _LLM_ERROR_FALLBACK

def record_usage(output) -> None:
    """记录一次模型调用的 token 用量。

    usage_metadata 来自厂商响应，不一定存在（部分兼容接口不返回）——
    缺失时静默跳过，不能让指标采集影响对话本身。
    cache_read 单独计：它反映前缀缓存命中量，是成本优化的关键观测点。
    """
    usage = getattr(output, "usage_metadata", None)
    if not usage:
        return
    metrics.LLM_TOKENS.inc(usage.get("input_tokens", 0) or 0, "input")
    metrics.LLM_TOKENS.inc(usage.get("output_tokens", 0) or 0, "output")
    details = usage.get("input_token_details") or {}
    cache_read = details.get("cache_read", 0) or 0
    if cache_read:
        metrics.LLM_CACHE_READ_TOKENS.inc(cache_read)

def _agent_config(thread_id: str, user_id: int | None):
    """构造图执行配置（含 Langfuse trace 透传）。"""
    return tracing.build_config(
        thread_id,
        user_id=user_id,
        trace_name="chat",
        base={"configurable": {"thread_id": thread_id}},
    )

def _raise_agent_error(exc: Exception, thread_id: str, stage: str) -> None:
    """记录完整异常并抛出已翻译的 AgentError（不泄露内部细节）。"""
    kind, message = classify_llm_error(exc)
    metrics.LLM_CALLS.inc(1, kind)
    logger.exception("chat %s 失败 thread=%s", stage, thread_id)
    raise AgentError(message, kind) from exc

async def invoke(thread_id: str, message: str, user_id: int | None = None) -> str:
    """执行 Agent 图并一次性取回纯文本回复（REST 入口用）。

    WS 入口走 `stream`；这里保留非流式实现是因为 REST 响应体
    本来就要一次给完整文本，用流式反而多绕一圈。
    """
    config = _agent_config(thread_id, user_id)
    try:
        graph = await get_graph()
        with metrics.timer(metrics.LLM_DURATION):
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content=message)]},
                config,
            )
        metrics.LLM_CALLS.inc(1, "ok")
    # 这里必须兜住所有异常并统一翻译：上层（REST/WS）只认 AgentError，
    # 厂商 SDK 抛什么类型不该泄漏到业务层
    except Exception as exc:  # noqa: BLE001
        _raise_agent_error(exc, thread_id, "执行")

    reply = result["messages"][-1]
    # content 可能是非字符串形态（分段 content / 空 tool_calls 消息），
    # 统一走 .text 取纯文本；空回复给兜底话术，避免前端渲染出空白气泡
    text = reply.text if isinstance(reply, AIMessage) else str(reply.content)
    if not text.strip():
        logger.warning("chat 生成空回复 thread=%s", thread_id)
        return EMPTY_REPLY
    return text

async def stream(
    thread_id: str,
    message: str,
    user_id: int | None,
    on_delta: Callable[[str], None],
) -> str:
    """流式执行 Agent 图：每生成一段文本就回调 `on_delta`，返回完整文本。

    为什么用 astream_events 而不是 astream：
    `astream` 只给出**节点级**的输出（agent 节点跑完才拿到整段 AIMessage），
    拿不到 token。`astream_events` 能捕获 `on_chat_model_stream`，
    才是真正的逐 token 流。

    **必须过滤空 content**：Agent 是 tool-calling 结构，一轮对话至少两次
    模型调用（先决定调工具、再根据结果作答）。实测第一轮 100% 是
    空 content + tool_calls，只有最后一轮才有正文。若不过滤，前端会收到
    大量空 delta，且可能把工具调用的中间文本当成回答推给用户。
    """
    config = _agent_config(thread_id, user_id)
    parts: list[str] = []
    first_token_at: float | None = None
    model_started_at: float | None = None
    started_at = time.monotonic()
    try:
        graph = await get_graph()
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content=message)]}, config, version="v2"
        ):
            kind = event["event"]
            # LLM 调用指标按事件成对计时，而不是用整个流的总耗时。
            # 为什么必须这样：一轮对话至少调两次模型（工具轮 + 最终轮），
            # 用总耗时会把"检索 + 两次模型"混成一个数，既不能反映单次
            # 调用质量，也会把调用次数算成 1。
            if kind == "on_chat_model_start":
                model_started_at = time.monotonic()
                continue
            if kind == "on_chat_model_end":
                if model_started_at is not None:
                    metrics.LLM_DURATION.observe(time.monotonic() - model_started_at)
                    model_started_at = None
                # token 用量只在 on_chat_model_end 里：流式 chunk 本身不带 usage。
                # 一轮对话可能调用多次模型，必须逐次累加
                record_usage(event["data"].get("output"))
                metrics.LLM_CALLS.inc(1, "ok")
                continue
            if kind != "on_chat_model_stream":
                continue
            chunk = event["data"]["chunk"]
            # 工具调用轮次的 chunk 没有正文（content 为空），跳过；
            # 这是区分"中间轮次"和"最终回答"最简单可靠的口径
            text = getattr(chunk, "content", "") or ""
            if not text:
                continue
            if first_token_at is None:
                # TTFT 从收到用户消息算起，而不是从模型开始生成算起——
                # 用户感知的是"我发出去多久后看到第一个字"，含检索时间
                first_token_at = time.monotonic()
                metrics.CHAT_TTFT.observe(first_token_at - started_at)
            parts.append(text)
            on_delta(text)
    except Exception as exc:  # noqa: BLE001
        _raise_agent_error(exc, thread_id, "流式执行")

    full = "".join(parts)
    if not full.strip():
        logger.warning("chat 生成空回复 thread=%s", thread_id)
        return EMPTY_REPLY
    return full
