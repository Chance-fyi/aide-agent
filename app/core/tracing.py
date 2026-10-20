"""Langfuse 可观测性接入：把 Agent 执行链路（LLM 调用、工具、检索）上报到 Langfuse。

**三条硬约束**

1. **默认关闭、零侵入**：没配 Key 时完全 no-op，连 handler 都不创建。
   可观测性是"锦上添花"，绝不能成为启动前提或运行依赖——
   线上 Langfuse 挂了，客服对话必须照常工作。
2. **失败只记日志**：初始化/上报异常一律吞掉。上报失败丢的是观测数据，
   不能让它变成用户侧的 500。
3. **配置外置**：Key/host 全走 .env，代码不硬编码任何 Langfuse 地址。

**为什么用 CallbackHandler 而不是 OTel 手动埋点**

LangGraph 的 `graph.ainvoke(config=...)` 会把 callbacks 传给内部所有
LangChain 组件（LLM、ToolNode、Runnable），一处接线就能自动拿到
完整的父子 span 树。手动埋点要自己维护 span 层级与上下文传播，易错且冗余。

**为什么要在关闭时 flush**

Langfuse 是批量异步上报（攒批减少网络请求）。进程退出时缓冲区里的
span 会丢——本地脚本跑完就退、uvicorn 收到 SIGTERM 时都是这种情况。
所以 `flush()` 必须挂在 lifespan 关闭阶段。
"""

import logging

from langchain_core.callbacks import BaseCallbackHandler

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Langfuse 回调器单例。
# 为什么复用：CallbackHandler 自身无状态，但每次新建都会走一遍
# "从全局单例取 client"的路径；复用更省，也避免多处行为不一致
_handler: BaseCallbackHandler | None = None
# 是否已经尝试过初始化。区分"没试过"与"试过但失败"，
# 避免每次请求都重试一次注定失败的初始化（会刷日志）
_initialized = False
_enabled = False

def is_enabled() -> bool:
    """Langfuse 是否可用（配置齐全且初始化成功）。"""
    return _enabled

def init() -> bool:
    """初始化 Langfuse 客户端与回调器；返回是否可用。

    幂等：重复调用直接返回上次结果。
    任何异常都降级为"不可用"，不影响调用方。
    """
    global _handler, _initialized, _enabled

    if _initialized:
        return _enabled
    _initialized = True

    s = get_settings()
    if not s.langfuse_enabled:
        logger.info("Langfuse 已关闭（LANGFUSE_ENABLED=false）")
        return False
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        # 不告警：这是"没配"而非"配错了"，本地开发默认就应该是这样
        logger.info("Langfuse 未配置 Key，跳过接入")
        return False

    try:
        # 延迟导入：没启用 Langfuse 的部署不必为它付 import 开销
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        # 显式传参而不是依赖 SDK 读环境变量：
        # 本项目所有配置统一走 Settings，两套来源并存会导致"改了 .env 不生效"
        Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            host=s.langfuse_host,
            environment=s.langfuse_environment,
        )
        _handler = CallbackHandler()
        _enabled = True
        logger.info("Langfuse 已接入: host=%s env=%s", s.langfuse_host, s.langfuse_environment)
    except Exception:
        # 初始化失败（Key 错、host 不可达、SDK 版本不兼容）只记日志。
        # 宁可没有观测数据，也不能让服务起不来
        logger.exception("Langfuse 初始化失败，可观测性降级为关闭")
        _handler = None
        _enabled = False

    return _enabled

def get_handler() -> BaseCallbackHandler | None:
    """取回调器；未启用返回 None（调用方据此决定是否加 callbacks）。"""
    init()
    return _handler

def build_config(
    thread_id: str,
    user_id: int | str | None = None,
    trace_name: str = "chat",
    tags: list[str] | None = None,
    base: dict | None = None,
) -> dict:
    """在现有 LangGraph config 上附加追踪信息。

    未启用时**原样返回** base——调用方无需写 if 分支，
    这条路径必须与接入前完全等价（零行为差异）。

    metadata 里的 `langfuse_*` 键是 SDK 约定的入口：
    - langfuse_session_id：会话维度聚合（同一 thread 的多轮归到一个 session）
    - langfuse_user_id：用户维度聚合（定位"某个用户总遇到问题"）
    - langfuse_tags：标签，便于在 UI 里按场景筛选（如 chat / eval）
    """
    config = dict(base or {})

    handler = get_handler()
    if handler is None:
        return config

    # callbacks 用追加而非覆盖：调用方可能已经挂了别的回调（测试、其他观测）
    existing = config.get("callbacks")
    if existing is None:
        config["callbacks"] = [handler]
    elif isinstance(existing, list):
        config["callbacks"] = [*existing, handler]
    else:
        # 单个 handler 对象：包成列表
        config["callbacks"] = [existing, handler]

    # LangGraph 的 configurable 是"运行时参数"通道，metadata 是"观测元数据"通道，
    # 两者不能混——把 langfuse_* 放进 configurable 会被节点当业务参数读走
    metadata = dict(config.get("metadata") or {})
    metadata["langfuse_session_id"] = thread_id
    metadata["langfuse_trace_name"] = trace_name
    if user_id is not None:
        metadata["langfuse_user_id"] = str(user_id)
    if tags:
        metadata["langfuse_tags"] = tags
    config["metadata"] = metadata

    return config

def flush() -> None:
    """把缓冲区里的 span 推出去（应用关闭时调用）。

    只记日志不抛异常：关闭阶段的失败不能阻止进程退出。
    """
    if not _enabled:
        return
    try:
        from langfuse import Langfuse

        Langfuse().flush()
        logger.info("Langfuse 缓冲区已 flush")
    except Exception:
        logger.warning("Langfuse flush 失败（进程仍正常退出）", exc_info=True)

def shutdown() -> None:
    """应用关闭：先 flush 再释放。"""
    global _handler, _enabled
    if not _enabled:
        return
    flush()
    try:
        from langfuse import Langfuse

        Langfuse().shutdown()
    except Exception:
        logger.warning("Langfuse shutdown 异常（不影响退出）", exc_info=True)
    _handler = None
    _enabled = False
