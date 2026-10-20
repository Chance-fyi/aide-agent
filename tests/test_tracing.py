"""Langfuse 接入测试（不依赖真实 Langfuse 服务）。

测的重点不是"能不能上报"（那需要真服务），而是**三条硬约束**：

1. 未配置时完全 no-op —— config 原样返回，与接入前零行为差异
2. 初始化失败不影响主流程 —— 只降级为"无观测"，不能抛
3. metadata 键名正确 —— SDK 靠 `langfuse_*` 约定取上下文，写错就静默丢数据
"""

import pytest

from app.core import tracing
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def _reset_tracing():
    """每个测试重置模块级单例，避免测试间互相污染。

    tracing 用模块级缓存记录"是否已初始化"，这是生产上正确的设计
    （避免每请求重试），但测试必须能重置。
    """
    def _reset():
        tracing._handler = None
        tracing._initialized = False
        tracing._enabled = False

    _reset()
    yield
    _reset()

@pytest.fixture
def no_keys(monkeypatch):
    """无 Key 环境（本地开发默认）。"""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

@pytest.fixture
def with_keys(monkeypatch):
    """有 Key 环境（但不指向真实服务）。"""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:1")  # 不可达，验证不阻塞
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

# ============================================================
# 约束 1：未配置 = 零行为差异
# ============================================================

def test_disabled_when_keys_missing(no_keys):
    assert tracing.init() is False
    assert tracing.is_enabled() is False
    assert tracing.get_handler() is None

def test_build_config_passthrough_when_disabled(no_keys):
    """未配置时 config 必须**原样**返回。

    这是最关键的一条：接入可观测性不应改变任何运行时行为。
    如果这里多塞了 callbacks 或 metadata，等于给未配置的部署
    也加了一层开销和潜在失败点。
    """
    base = {"configurable": {"thread_id": "t-1"}}
    got = tracing.build_config("t-1", user_id=42, base=base)
    assert got == base
    assert got is not base          # 不返回同一个对象，避免调用方误改
    assert "callbacks" not in got
    assert "metadata" not in got

def test_master_switch_disables_even_with_keys(monkeypatch, with_keys):
    """LANGFUSE_ENABLED=false 时即使有 Key 也不接入（临时关闭用）。"""
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    get_settings.cache_clear()
    assert tracing.init() is False

def test_flush_and_shutdown_are_noop_when_disabled(no_keys):
    """未启用时 flush/shutdown 必须安全且不抛（lifespan 会无条件调用）。"""
    tracing.flush()
    tracing.shutdown()
    assert tracing.is_enabled() is False

# ============================================================
# 约束 2：初始化失败只降级，不抛
# ============================================================

def test_init_failure_degrades_silently(monkeypatch, with_keys):
    """SDK 初始化抛异常时，init() 返回 False 而不是向上传播。

    线上 Langfuse 不可达/Key 失效，客服对话必须照常工作。
    """
    import langfuse

    def boom(*args, **kwargs):
        raise RuntimeError("Langfuse 服务不可达")

    monkeypatch.setattr(langfuse, "Langfuse", boom)
    assert tracing.init() is False
    assert tracing.get_handler() is None
    # 降级后 build_config 仍然可用（退化为 passthrough）
    base = {"configurable": {"thread_id": "t"}}
    assert tracing.build_config("t", base=base) == base

def test_init_is_idempotent_and_cached(monkeypatch, with_keys):
    """重复 init() 不重复执行初始化（避免每请求重试注定失败的连接）。"""
    import langfuse

    calls = []
    real = langfuse.Langfuse

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(langfuse, "Langfuse", counting)
    tracing.init()
    tracing.init()
    tracing.init()
    assert len(calls) == 1

# ============================================================
# 约束 3：metadata 键名与结构
# ============================================================

def test_build_config_metadata_keys(monkeypatch):
    """启用时写入 SDK 约定的 langfuse_* 键。

    这些键名是 SDK 的硬约定（`langfuse_session_id` 等），
    写错不会报错，只会静默丢失聚合维度——所以必须用测试锁住。
    """
    # 直接注入假 handler，绕过真实 SDK 初始化
    tracing._initialized = True
    tracing._enabled = True
    tracing._handler = object()

    cfg = tracing.build_config(
        "thread-abc",
        user_id=7,
        trace_name="chat",
        tags=["prod", "vip"],
        base={"configurable": {"thread_id": "thread-abc"}},
    )
    md = cfg["metadata"]
    assert md["langfuse_session_id"] == "thread-abc"
    assert md["langfuse_user_id"] == "7"          # SDK 要字符串
    assert md["langfuse_trace_name"] == "chat"
    assert md["langfuse_tags"] == ["prod", "vip"]
    # callbacks 被追加，且原 configurable 未被破坏
    assert cfg["callbacks"] == [tracing._handler]
    assert cfg["configurable"] == {"thread_id": "thread-abc"}

def test_build_config_omits_user_id_when_anonymous():
    """匿名会话不写 langfuse_user_id（否则 UI 里会多出一个空用户）。"""
    tracing._initialized = True
    tracing._enabled = True
    tracing._handler = object()

    cfg = tracing.build_config("t", user_id=None, base={})
    assert "langfuse_user_id" not in cfg["metadata"]
    assert cfg["metadata"]["langfuse_session_id"] == "t"

def test_build_config_appends_to_existing_callbacks():
    """调用方已有回调时追加而非覆盖（否则会静默丢掉别人的观测）。"""
    tracing._initialized = True
    tracing._enabled = True
    tracing._handler = object()

    existing = object()
    cfg = tracing.build_config("t", base={"callbacks": [existing]})
    assert cfg["callbacks"] == [existing, tracing._handler]

    # 单个 handler 对象也要能正确包装
    cfg2 = tracing.build_config("t", base={"callbacks": existing})
    assert cfg2["callbacks"] == [existing, tracing._handler]

def test_build_config_does_not_mutate_input():
    """不修改调用方传入的 config（LangGraph 会复用 config 对象）。"""
    tracing._initialized = True
    tracing._enabled = True
    tracing._handler = object()

    base = {"configurable": {"thread_id": "t"}}
    snapshot = {"configurable": {"thread_id": "t"}}
    tracing.build_config("t", base=base)
    assert base == snapshot
