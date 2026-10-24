"""聚合指标：计数器 / 仪表 / 直方图。

**解决什么问题**

Langfuse 是 **trace 级**——能查"这一次对话发生了什么"，但回答不了
"今天 QPS 多少 / P95 延迟多少 / 转接率多少 / token 花了多少"。
这些聚合数字才是运维和成本核算真正要看的。

**为什么自研而不是 prometheus-client**

项目一贯的风格是"避免为单一功能引入重依赖"（见 `security.py` 用标准库实现
PBKDF2、`tracing.py` 强调未配置时零侵入）。而且 prometheus-client 在
**多 worker 下需要 multiprocess mode**（`PROMETHEUS_MULTIPROC_DIR` 环境变量 +
共享目录 + 启动时清理），配置繁琐且容易踩坑。

关键认知：**Prometheus 本身就是"每个实例各自暴露、由服务端聚合"的模型**。
所以每个 worker 独立暴露自己的 `/metrics`，Prometheus 分别抓取，
查询时用 `sum()` / `histogram_quantile()` 聚合即可——根本不需要
multiprocess mode。这一点让"自研 + 输出标准格式"成为更简单的选择。

**输出两种格式**

- `/metrics` → Prometheus text format（给 Prometheus/Grafana 抓取）
- `/stats`   → JSON（给人看、给前端管理页看）

**标签基数纪律（必须遵守）**

标签值必须是**有限的枚举**（状态码、错误类型、是否转接）。
绝不能把 `thread_id` / `user_id` / 查询文本当标签——每个不同的值都会
创建一条新时间序列，几万个用户就能把内存和抓取压垮。
"""

import logging
import math
import threading
import time
from collections.abc import Iterable
from typing import Self

logger = logging.getLogger(__name__)

# ---- 预设桶（按用途区分，不要用一个桶套所有场景）----

# LLM 调用：正常 1-8 秒，超时重试会到 30-90 秒
BUCKETS_LLM = (0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60, 90, math.inf)
# 快操作：检索、DB、缓存。热态毫秒级，冷启动可能到 20 秒
BUCKETS_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, math.inf)
# HTTP 请求：覆盖流式长连接（一条流可能持续 30 秒以上）
BUCKETS_HTTP = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, math.inf)
# 首字延迟（TTFT）：流式体验的核心指标，用户对 1 秒内才觉得"有反应"
BUCKETS_TTFT = (0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 8, 15, 30, math.inf)


class _Metric:
    """所有指标的公共部分：名字、说明、标签维度。"""

    kind = ""

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labels = labels
        self._lock = threading.Lock()

    def _key(self, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(self.labels):
            raise ValueError(
                f"指标 {self.name} 需要 {len(self.labels)} 个标签，收到 {len(values)} 个"
            )
        return values

    def _series(self, key: tuple[str, ...]) -> str:
        """渲染标签串：`{method="POST",status="200"}`。"""
        if not self.labels:
            return ""
        pairs = ",".join(f'{k}="{v}"' for k, v in zip(self.labels, key, strict=True))
        return "{" + pairs + "}"


class Counter(_Metric):
    """只增不减的计数（请求数、错误数、token 数）。"""

    kind = "counter"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def snapshot(self) -> list[tuple[tuple[str, ...], float]]:
        with self._lock:
            return sorted(self._values.items())

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        for key, value in self.snapshot():
            lines.append(f"{self.name}{self._series(key)} {_fmt(value)}")
        return lines


class Gauge(_Metric):
    """可增可减的当前值（在线连接数、当前并发）。"""

    kind = "gauge"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, *label_values: str) -> None:
        self.inc(-amount, *label_values)

    def snapshot(self) -> list[tuple[tuple[str, ...], float]]:
        with self._lock:
            return sorted(self._values.items())

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        for key, value in self.snapshot():
            lines.append(f"{self.name}{self._series(key)} {_fmt(value)}")
        return lines


class Histogram(_Metric):
    """固定桶直方图：统计延迟分布，可估算 P50/P95/P99。

    为什么用固定桶而不是记录所有样本：
    保留全部样本会让内存随请求量线性增长（一天几十万请求就是几十万浮点数），
    而分位数只需要近似值。Prometheus 的 histogram 也是这个做法。

    为什么不用 Prometheus 的 Summary（滑动窗口精确分位）：
    Summary 无法跨实例聚合（每个实例的分位数不能相加），
    而本项目是多 worker，必须靠 Prometheus 服务端聚合。
    """

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: Iterable[float] = BUCKETS_FAST,
        labels: tuple[str, ...] = (),
    ) -> None:
        super().__init__(name, help_text, labels)
        # inf 必须存在且是最后一个：否则 +Inf 桶缺失，Prometheus 无法算总量
        self.buckets = tuple(sorted(set(buckets)))
        if self.buckets[-1] != math.inf:
            self.buckets = (*self.buckets, math.inf)
        # key -> [每个桶的累计计数, 总和, 总次数]
        self._data: dict[tuple[str, ...], list] = {}

    def observe(self, value: float, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                entry = self._data[key] = [[0] * len(self.buckets), 0.0, 0]
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    entry[0][i] += 1
            entry[1] += value
            entry[2] += 1

    def snapshot(self) -> list[tuple[tuple[str, ...], dict]]:
        with self._lock:
            out = []
            for key, (counts, total, count) in sorted(self._data.items()):
                out.append((key, {
                    "buckets": dict(zip(self.buckets, counts, strict=True)),
                    "sum": total,
                    "count": count,
                }))
            return out

    def quantile(self, q: float, *label_values: str) -> float | None:
        """从桶估算分位数（Prometheus histogram_quantile 同款线性插值）。

        这是**估算**：桶内部分布未知，假设均匀。所以桶的边界要贴近
        实际分布才有意义——这就是为什么上面按用途预设了不同桶。
        """
        key = self._key(label_values)
        with self._lock:
            entry = self._data.get(key)
            if entry is None or entry[2] == 0:
                return None
            counts, _, total = entry
            rank = q * total
            prev_count = 0
            prev_bound = 0.0
            for bound, cum in zip(self.buckets, counts, strict=True):
                if cum >= rank:
                    if bound == math.inf:
                        return prev_bound
                    # 在该桶内按比例插值
                    span = cum - prev_count
                    if span <= 0:
                        return bound
                    return prev_bound + (bound - prev_bound) * ((rank - prev_count) / span)
                prev_count, prev_bound = cum, bound
            return prev_bound

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        for key, data in self.snapshot():
            for bound, cum in data["buckets"].items():
                bound_str = "+Inf" if bound == math.inf else _fmt(bound)
                series = self._series_extra(key, 'le="' + bound_str + '"')
                lines.append(f"{self.name}_bucket{series} {cum}")
            lines.append(f"{self.name}_sum{self._series(key)} {_fmt(data['sum'])}")
            lines.append(f"{self.name}_count{self._series(key)} {data['count']}")
        return lines

    def _series_extra(self, key: tuple[str, ...], extra: str) -> str:
        """histogram 的 bucket 行需要额外带 `le` 标签。"""
        base = self._series(key)
        if not base:
            return "{" + extra + "}"
        return base[:-1] + "," + extra + "}"


def _fmt(value: float) -> str:
    """数字格式化：整数不显示小数点，浮点保留合理精度。"""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6g}"


# ============================================================
# 指标定义
#
# 命名遵循 Prometheus 惯例：<namespace>_<subsystem>_<name>_<unit>
# 标签只用有限枚举，绝不带 thread_id / user_id（见模块文档）
# ============================================================

# ---- HTTP ----
HTTP_REQUESTS = Counter(
    "aide_http_requests_total", "HTTP 请求总数", ("method", "path", "status")
)
HTTP_DURATION = Histogram(
    "aide_http_request_duration_seconds", "HTTP 请求耗时", BUCKETS_HTTP, ("method", "path")
)

# ---- 对话 ----
CHAT_MESSAGES = Counter(
    "aide_chat_messages_total", "对话消息总数", ("channel", "outcome")
)
CHAT_DURATION = Histogram(
    "aide_chat_duration_seconds", "一轮对话处理耗时", BUCKETS_LLM, ("channel",)
)
# 首字延迟：流式体验的核心指标（用户多久才看到第一个字）
CHAT_TTFT = Histogram(
    "aide_chat_ttft_seconds", "流式回复首字延迟", BUCKETS_TTFT
)

# ---- LLM ----
LLM_CALLS = Counter("aide_llm_calls_total", "LLM 调用次数", ("outcome",))
LLM_DURATION = Histogram(
    "aide_llm_duration_seconds", "LLM 单次调用耗时", BUCKETS_LLM
)
# token 用量：成本核算的直接依据。direction 区分输入/输出
LLM_TOKENS = Counter("aide_llm_tokens_total", "LLM token 用量", ("direction",))
# 前缀缓存命中量：命中越多越省钱，值得单独看趋势
LLM_CACHE_READ_TOKENS = Counter(
    "aide_llm_cache_read_tokens_total", "LLM 前缀缓存命中的 token 数"
)

# ---- 检索 ----
RETRIEVAL_DURATION = Histogram(
    "aide_retrieval_duration_seconds", "知识库检索耗时", BUCKETS_FAST
)
RETRIEVAL_RESULTS = Counter(
    "aide_retrieval_results_total", "检索结果条数分布", ("bucket",)
)

# ---- 业务 ----
TRANSFERS = Counter("aide_transfers_total", "转人工次数", ("trigger",))
RATELIMIT_HITS = Counter("aide_ratelimit_hits_total", "限流触发次数", ("kind",))

# ---- 连接 ----
WS_CONNECTIONS = Gauge("aide_ws_connections", "当前 WebSocket 连接数", ("side",))
WS_EVENTS = Counter("aide_ws_events_total", "WebSocket 事件数", ("type",))

ALL_METRICS: tuple[_Metric, ...] = (
    HTTP_REQUESTS, HTTP_DURATION,
    CHAT_MESSAGES, CHAT_DURATION, CHAT_TTFT,
    LLM_CALLS, LLM_DURATION, LLM_TOKENS, LLM_CACHE_READ_TOKENS,
    RETRIEVAL_DURATION, RETRIEVAL_RESULTS,
    TRANSFERS, RATELIMIT_HITS,
    WS_CONNECTIONS, WS_EVENTS,
)


# ============================================================
# 导出
# ============================================================

def render_prometheus() -> str:
    """渲染成 Prometheus text format（/metrics 端点用）。"""
    lines: list[str] = []
    for metric in ALL_METRICS:
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"


def snapshot() -> dict:
    """结构化快照（JSON 给人看 / 管理页展示用）。"""
    out: dict = {}
    for metric in ALL_METRICS:
        if isinstance(metric, Histogram):
            series = []
            for key, data in metric.snapshot():
                labels = dict(zip(metric.labels, key, strict=True))
                series.append({
                    "labels": labels,
                    "count": data["count"],
                    "sum": round(data["sum"], 3),
                    "avg": round(data["sum"] / data["count"], 3) if data["count"] else None,
                    "p50": _round(metric.quantile(0.5, *key)),
                    "p95": _round(metric.quantile(0.95, *key)),
                    "p99": _round(metric.quantile(0.99, *key)),
                })
            out[metric.name] = {"help": metric.help_text, "type": "histogram", "series": series}
        else:
            series = [
                {"labels": dict(zip(metric.labels, key, strict=True)), "value": value}
                for key, value in metric.snapshot()
            ]
            out[metric.name] = {
                "help": metric.help_text,
                "type": metric.kind,
                "series": series,
            }
    return out


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


class timer:
    """上下文管理器：测量代码块耗时并记录到直方图。

    用法：
        with metrics.timer(metrics.LLM_DURATION):
            ...
    """

    __slots__ = ("_hist", "_labels", "_start")

    def __init__(self, hist: Histogram, *labels: str) -> None:
        self._hist = hist
        self._labels = labels

    def __enter__(self) -> Self:
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc_info) -> None:
        self._hist.observe(time.monotonic() - self._start, *self._labels)


# ============================================================
# HTTP 中间件
# ============================================================

# 路径归一化规则：把路径里的动态段替换成占位符。
#
# 为什么必须归一化：`/chat/{uuid}/history` 每换一个会话就是一个新标签值，
# 几万个会话就能造出几万条时间序列，把内存和 Prometheus 抓取压垮。
# 只保留"路由形态"，基数才是可控的。
_PATH_PATTERNS = (
    ("/chat/", ("history",), "/chat/{thread_id}/history"),
    ("/agent/threads/", ("accept", "close", "reply", "user_history"), "/agent/threads/{thread_id}/*"),
)


def normalize_path(path: str) -> str:
    """把具体路径归一化成有限的几种形态。"""
    for prefix, _suffixes, normalized in _PATH_PATTERNS:
        if path.startswith(prefix):
            return normalized
    return path


class MetricsMiddleware:
    """纯 ASGI 中间件：统计 HTTP 请求数与耗时。

    为什么不用 Starlette 的 BaseHTTPMiddleware：它会包装响应流，
    对**流式响应**（SSE / 分块）有已知的缓冲副作用。本项目虽然流式走
    WebSocket，但没必要埋这个隐患；纯 ASGI 只包一层 send，零副作用。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        # 只统计 HTTP；WebSocket 有独立指标（连接数/事件数）
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = normalize_path(scope.get("path", "?"))
        status_holder = {"status": "0"}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = str(message["status"])
            await send(message)

        started = time.monotonic()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # finally 里记录：客户端中途断开时也要算一次请求（它确实消耗了资源）
            HTTP_REQUESTS.inc(1, method, path, status_holder["status"])
            HTTP_DURATION.observe(time.monotonic() - started, method, path)


# ============================================================
# 跨进程聚合
#
# **为什么必须做**：`uvicorn --workers N` 是单端口多进程，Prometheus 抓
# `/metrics` 时只能落到**随机一个 worker**。实测连续抓 5 次：
#     8, 9, 10, 1, 11        ← 中间那次换了 worker，计数从头开始
# 也就是说多 worker 下每个数字都只有真实值的 1/N。这比没有指标更危险：
# 看到 QPS=10 实际是 40，扩容决策直接错。
#
# **为什么用文件而不是 prometheus_client 的 multiprocess mode**：
# 那需要引入依赖 + 设置 PROMETHEUS_MULTIPROC_DIR + 启动时清理目录。
# 而"每个 worker 把自己快照写到一个共享目录、读取时合并"只需要标准库，
# 语义完全一样（同一台机器上的 worker 天然共享文件系统）。
#
# **为什么分位数可以跨进程合并**：分位数本身不可加，但**桶计数可加**。
# 合并各 worker 的桶计数后重新算分位，结果与"把样本合在一起算"等价。
# ============================================================

def _metrics_dir():
    """聚合目录。单 worker 时只有自己一个文件，行为与纯内存一致。"""
    import os
    from pathlib import Path

    raw = os.environ.get("METRICS_DIR", "/tmp/aide_metrics")
    path = Path(raw)
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        # 只读文件系统等：降级为单进程模式，不能让 /metrics 直接 500
        return None


def _dump_own() -> None:
    """把当前进程的指标快照写入 `<dir>/<pid>.json`（原子替换）。"""
    import json
    import os

    path = _metrics_dir()
    if path is None:
        return
    target = path / f"{os.getpid()}.json"
    tmp = path / f".{os.getpid()}.tmp"
    try:
        data = {
            m.name: [
                {"labels": list(k), "data": _raw(m, k)}
                for k in (m._data if isinstance(m, Histogram) else m._values)
            ]
            for m in ALL_METRICS
        }
        tmp.write_text(json.dumps(data), encoding="utf-8")
        # 原子替换：读端可能正在读，绝不能让它看到半个文件
        tmp.replace(target)
    except Exception:
        # 聚合是增强能力，失败只影响跨进程合并，不能让 /metrics 挂掉
        logger.warning("指标快照写入失败", exc_info=True)


def _raw(metric: _Metric, key: tuple[str, ...]) -> dict:
    """取某条序列的原始数据（供序列化）。"""
    if isinstance(metric, Histogram):
        counts, total, count = metric._data[key]
        return {
            "buckets": [list(pair) for pair in zip(metric.buckets, counts, strict=True)],
            "sum": total,
            "count": count,
        }
    return {"value": metric._values[key]}


def _load_others() -> list[dict]:
    """读取目录下**其他存活进程**的快照（含自己刚写的那个）。

    用 mtime 判断存活：worker 每被抓取一次才更新文件，
    所以超过 `STALE_SEC` 未更新的文件视为已死进程，跳过并清理。
    """
    import json
    import time

    path = _metrics_dir()
    if path is None:
        return []
    stale_after = 300   # 5 分钟没更新视为已死（抓取间隔通常 15-60 秒）
    now = time.time()
    out = []
    for f in path.glob("*.json"):
        try:
            if now - f.stat().st_mtime > stale_after:
                f.unlink(missing_ok=True)   # 顺手清理，避免目录无限增长
                continue
            out.append(json.loads(f.read_text(encoding="utf-8")))
        # 单个文件损坏/半写不影响整体合并，跳过即可；
        # 这里必须吞掉所有异常，否则一个坏文件就让 /metrics 整个 500
        except Exception:
            # 单个文件损坏/半写不影响整体合并，跳过即可；
            # 这里必须吞掉所有异常，否则一个坏文件就让 /metrics 整个 500
            logger.debug("跳过无法解析的指标快照 %s", f, exc_info=True)
    return out


def render_prometheus_aggregated() -> str:
    """/metrics 的实际实现：先落盘自己，再合并所有 worker 的快照。

    单进程部署时目录里只有自己一个文件，输出与 `render_prometheus()` 相同。
    """
    _dump_own()
    snaps = _load_others()
    if len(snaps) <= 1:
        # 只有一个进程（或目录不可用）：走纯内存路径，避免无谓的合并开销
        return render_prometheus()

    merged = _merge(snaps)
    lines: list[str] = []
    for metric in ALL_METRICS:
        name = metric.name
        lines.append(f"# HELP {name} {metric.help_text}")
        lines.append(f"# TYPE {name} {metric.kind}")
        for labels, data in sorted(merged.get(name, {}).items()):
            label_pairs = dict(zip(metric.labels, labels, strict=True))
            if isinstance(metric, Histogram):
                for bound, cum in data["buckets"].items():
                    bound_str = "+Inf" if bound == math.inf else _fmt(bound)
                    pairs = {**label_pairs, "le": bound_str}
                    lines.append(f"{name}_bucket{_render_labels(pairs)} {int(cum)}")
                lines.append(f"{name}_sum{_render_labels(label_pairs)} {_fmt(data['sum'])}")
                lines.append(f"{name}_count{_render_labels(label_pairs)} {int(data['count'])}")
            else:
                lines.append(f"{name}{_render_labels(label_pairs)} {_fmt(data['value'])}")
    return "\n".join(lines) + "\n"


def _render_labels(pairs: dict) -> str:
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in pairs.items()) + "}"


def _merge(snaps: list[dict]) -> dict:
    """合并多份快照：counter/gauge 相加，histogram 桶相加。

    为什么 gauge 也相加：每个 worker 的"当前连接数"是独立的，
    总和才是集群真实值。
    """
    merged: dict = {}
    for snap in snaps:
        for name, series in snap.items():
            target = merged.setdefault(name, {})
            for item in series:
                key = tuple(item["labels"])
                data = item["data"]
                if "buckets" in data:      # histogram
                    entry = target.get(key)
                    if entry is None:
                        target[key] = {
                            "buckets": {b: c for b, c in data["buckets"]},
                            "sum": data["sum"],
                            "count": data["count"],
                        }
                    else:
                        for b, c in data["buckets"]:
                            entry["buckets"][b] = entry["buckets"].get(b, 0) + c
                        entry["sum"] += data["sum"]
                        entry["count"] += data["count"]
                else:                       # counter / gauge
                    entry = target.setdefault(key, {"value": 0.0})
                    entry["value"] += data["value"]
    return merged


async def flush_loop(interval: float = 10.0) -> None:
    """后台定时把本进程指标落盘（跨进程聚合的**关键前提**）。

    为什么必须有它：快照若只在 `/metrics` 被请求时刷新，那么
    「处理了对话、但之后没被抓取过」的 worker，其计数不会出现在别人的
    合并结果里。实测表现是增量不准（跑 3 个对话只看到 +1，下次抓取才补上）。

    10 秒间隔对应 Prometheus 默认 15 秒抓取周期：抓取时读到的其他 worker
    快照最多滞后 10 秒，对监控场景完全够用（指标本来就是趋势而非实时账本）。
    """
    import asyncio

    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(_dump_own)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 落盘失败只影响跨进程合并，不能让后台任务死掉
            logger.warning("指标定时落盘失败", exc_info=True)
