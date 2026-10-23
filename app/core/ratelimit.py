"""限流与并发保护。

**为什么需要**

每条用户消息都会触发一次 LLM 调用（`timeout=30` + `max_retries=2`，最坏占用
约 90 秒）。而 LangGraph 的连接池 `max_size=10`。没有任何限制时：

1. **成本**：任何人写个脚本就能把 LLM 账单刷到任意额度
2. **可用性（更致命）**：10 个并发请求就能占满连接池，之后**所有用户**的
   对话都在排队——一个人可以拖垮整个服务，这比超支更难接受

所以这里做两件事，它们防的不是同一类问题：

| 机制 | 防什么 | 粒度 |
|---|---|---|
| 滑动窗口限流 | 持续刷（成本、DB 写入量） | 按调用者身份，单位时间次数 |
| 并发闸门 | 瞬时打满（拖垮所有人的响应时间） | 全局，同时在跑的 LLM 调用数 |

**部署约束（必须知道）**

计数器和信号量都在**进程内存**里，因此只在单进程部署下精确。
多 worker 时实际限额 = 配置值 × worker 数。要跨进程精确限流需要 Redis
（INCR + EXPIRE 或 Lua 脚本），本步不做——和 `core/hub.py` 的连接表是同一个约束。

**为什么不用 slowapi/limits**

这类库围绕 HTTP 中间件设计（按 path + IP），而本项目的限流要按**登录账号**做
（匿名才退化成 IP），且 WebSocket 消息不是 HTTP 请求、中间件管不到。
业务逻辑又统一收在 `process_message` 里（见 AGENTS.md 约束 7），
在那一处做限流比给两条通道各配一套中间件更不容易走偏。
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

from app.core.config import get_settings

logger = logging.getLogger(__name__)

class RateLimitExceeded(Exception):
    """触发限流。`retry_after` 为建议的重试等待秒数，用于给用户可操作提示。"""

    def __init__(self, retry_after: int):
        super().__init__("rate limited")
        self.retry_after = retry_after

class BusyError(Exception):
    """并发闸门拿不到令牌（系统繁忙）。

    与限流区分开：限流是"你发得太快"，这里是"系统现在忙不过来"。
    对用户的文案与处置都不同，所以用两个异常。
    """

class SlidingWindowLimiter:
    """滑动窗口计数器：统计"最近 window 秒内发生了几次"。

    为什么用滑动窗口而不是固定窗口：固定窗口在边界处允许 2 倍突发
    （59 秒发 20 条 + 61 秒再发 20 条 = 2 秒内 40 条）。
    为什么不用令牌桶：滑动窗口的语义更贴近用户直觉（"每分钟最多 N 条"），
    也更好写测试断言。
    """

    def __init__(
        self, max_events: int, window_sec: float, clock: Callable[[], float] | None = None
    ) -> None:
        self.max_events = max_events
        self.window_sec = window_sec
        # 时钟可注入：测试里换成假时钟，避免 monkeypatch 全局 time.monotonic
        # （asyncio 的事件循环时钟就是 time.monotonic，冻结它会让 wait_for 永不超时）
        self._clock = clock or time.monotonic
        # key -> 该 key 最近的事件时间戳（递增，左侧最旧）
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> tuple[bool, int]:
        """尝试放行一次。返回 (是否放行, 建议重试等待秒数)。"""
        if self.max_events <= 0:
            return True, 0

        now = self._clock()
        cutoff = now - self.window_sec
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()

        # 丢掉窗口外的记录（deque 左端最旧，逐个 popleft 是 O(超出量)）
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.max_events:
            # 最早那条记录滑出窗口时才有名额，据此算出等待时间
            retry_after = max(1, int(hits[0] - cutoff) + 1)
            return False, retry_after

        hits.append(now)
        return True, 0

    def reset(self) -> None:
        """清空全部计数（测试与运维用）。"""
        self._hits.clear()

    def prune(self, max_keys: int = 10_000) -> None:
        """清理已空/过期的 key，防止内存随历史用户数无限增长。

        只在调用量达到阈值时触发，避免每次都全表扫描。
        """
        if len(self._hits) <= max_keys:
            return
        cutoff = self._clock() - self.window_sec
        stale = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
        for k in stale:
            self._hits.pop(k, None)
        logger.debug("限流器清理 %d 个过期 key，剩余 %d", len(stale), len(self._hits))

class ConcurrencyGuard:
    """并发闸门：限制同时在跑的昂贵操作数量。

    为什么必须**带超时地等待**而不是直接拒绝：
    瞬时并发高一点点是正常的（几个用户同时提问），立刻拒绝体验很差。
    但也绝不能无限等——排队时间超过阈值就说明系统真的扛不住了，
    这时候快速失败并给出"稍后再试"比让用户干等 90 秒更好。
    """

    def __init__(self, max_concurrent: int, wait_sec: float = 5.0) -> None:
        self.max_concurrent = max_concurrent
        self.wait_sec = wait_sec
        self._sem: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        # 懒创建：信号量要绑定到当前事件循环。虽然 3.10+ 的 Semaphore
        # 在 acquire 时才取 loop，但懒创建让测试里 reset() 更干净
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrent)
        return self._sem

    async def acquire(self) -> None:
        """占用一个并发名额；超时抛 BusyError。"""
        if self.max_concurrent <= 0:
            return   # 0 = 关闭闸门
        try:
            await asyncio.wait_for(self._semaphore().acquire(), timeout=self.wait_sec)
        except TimeoutError:
            raise BusyError() from None

    def release(self) -> None:
        if self.max_concurrent > 0 and self._sem is not None:
            self._sem.release()

    def reset(self) -> None:
        self._sem = None

# 进程级单例（配置驱动，见 Settings.rate_limit_per_minute / llm_max_concurrency）
_limiter: SlidingWindowLimiter | None = None
_guard: ConcurrencyGuard | None = None

def get_limiter() -> SlidingWindowLimiter:
    global _limiter
    if _limiter is None:
        s = get_settings()
        _limiter = SlidingWindowLimiter(s.rate_limit_per_minute, 60.0)
    return _limiter

def get_guard() -> ConcurrencyGuard:
    global _guard
    if _guard is None:
        s = get_settings()
        _guard = ConcurrencyGuard(s.llm_max_concurrency)
    return _guard

def reset() -> None:
    """重置单例（测试用：让改过的配置生效）。"""
    global _limiter, _guard
    _limiter = None
    _guard = None

def check_rate(key: str) -> None:
    """按 key 检查限流，超限抛 RateLimitExceeded。"""
    limiter = get_limiter()
    allowed, retry_after = limiter.allow(key)
    if not allowed:
        logger.warning("触发限流 key=%s retry_after=%ds", key, retry_after)
        raise RateLimitExceeded(retry_after)
    limiter.prune()
