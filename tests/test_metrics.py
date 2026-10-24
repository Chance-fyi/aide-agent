"""指标模块测试。

重点验证三件事：
1. **分位数估算正确**：P95 是运维判断"慢不慢"的依据，算错会误导扩容决策
2. **标签基数安全**：路径必须归一化，否则几万个会话能造出几万条时间序列
3. **导出格式合法**：/metrics 要是能被 Prometheus 解析的格式
"""

import math

import pytest

from app.core import metrics
from app.core.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsMiddleware,
    normalize_path,
    render_prometheus,
    snapshot,
)

# ============================================================
# Counter / Gauge
# ============================================================

def test_counter_accumulates_per_label_set():
    c = Counter("t_counter", "test", ("a", "b"))
    c.inc(1, "x", "y")
    c.inc(2, "x", "y")
    c.inc(5, "x", "z")
    assert dict(c.snapshot()) == {("x", "y"): 3.0, ("x", "z"): 5.0}


def test_counter_rejects_wrong_label_count():
    """标签个数写错必须立刻报错，而不是静默记到错误的分组里。

    静默容忍的话，指标看起来"有数"但维度全错，比没有指标更糟。
    """
    c = Counter("t_counter2", "test", ("a", "b"))
    with pytest.raises(ValueError, match="需要 2 个标签"):
        c.inc(1, "only-one")


def test_gauge_inc_dec_set():
    g = Gauge("t_gauge", "test", ("side",))
    g.inc(1, "user")
    g.inc(1, "user")
    g.dec(1, "user")
    assert dict(g.snapshot()) == {("user",): 1.0}
    g.set(10, "user")
    assert dict(g.snapshot()) == {("user",): 10.0}


# ============================================================
# Histogram 与分位数
# ============================================================

def test_histogram_counts_are_cumulative():
    """桶计数必须是累计值（Prometheus 语义），否则 histogram_quantile 算不对。"""
    h = Histogram("t_hist", "test", (1, 2, 3, math.inf))
    h.observe(0.5)
    h.observe(1.5)
    h.observe(2.5)
    (_, data), = h.snapshot()
    assert data["count"] == 3
    # <=1 有 1 个；<=2 有 2 个（累计）；<=3 有 3 个（累计）
    assert data["buckets"][1] == 1
    assert data["buckets"][2] == 2
    assert data["buckets"][3] == 3
    assert data["buckets"][math.inf] == 3


def test_histogram_inf_bucket_is_always_last():
    """+Inf 桶必须存在且最后：缺了它 Prometheus 无法算总量。"""
    h = Histogram("t_hist2", "test", (1, 2))   # 故意不传 inf
    assert h.buckets[-1] == math.inf


def test_quantile_estimate_is_reasonable():
    """均匀分布下，分位数估算应接近真实值（允许桶粒度误差）。"""
    h = Histogram("t_hist3", "test", (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, math.inf))
    for i in range(1, 101):          # 0.01 .. 1.00
        h.observe(i / 100)
    p50 = h.quantile(0.5)
    p95 = h.quantile(0.95)
    assert 0.45 <= p50 <= 0.55, f"P50 偏差过大: {p50}"
    assert 0.85 <= p95 <= 1.0, f"P95 偏差过大: {p95}"


def test_quantile_none_when_empty():
    """没有样本时返回 None，而不是 0——0 会被误读成"很快"。"""
    h = Histogram("t_hist4", "test", (1, math.inf))
    assert h.quantile(0.95) is None


def test_quantile_separates_label_sets():
    """不同标签的分位数必须独立计算（快/慢请求不能混在一起）。"""
    h = Histogram("t_hist5", "test", (0.1, 1, 10, math.inf), ("route",))
    for _ in range(10):
        h.observe(0.05, "fast")
    for _ in range(10):
        h.observe(5.0, "slow")
    assert h.quantile(0.5, "fast") < 0.2
    assert h.quantile(0.5, "slow") > 1


# ============================================================
# 标签基数安全
# ============================================================

@pytest.mark.parametrize(
    "path,expected",
    [
        ("/health", "/health"),
        ("/chat", "/chat"),
        ("/chat/9f1c2e3d-aaaa-bbbb-cccc-ddddeeeeffff/history", "/chat/{thread_id}/history"),
        ("/agent/threads/9f1c2e3d-aaaa-bbbb-cccc-ddddeeeeffff/accept", "/agent/threads/{thread_id}/*"),
        ("/kb/overview", "/kb/overview"),
    ],
)
def test_normalize_path_collapses_dynamic_segments(path, expected):
    """动态段必须归一化：否则每个会话 ID 都是一条新时间序列。

    这是指标系统最容易踩的坑——几万个会话就能把内存和抓取压垮。
    """
    assert normalize_path(path) == expected


def test_normalized_paths_are_finite():
    """任意 UUID 都归一化到同一个形态（基数不随会话数增长）。"""
    import uuid

    paths = {normalize_path(f"/chat/{uuid.uuid4()}/history") for _ in range(100)}
    assert len(paths) == 1, f"归一化后仍有多态: {paths}"


# ============================================================
# 导出格式
# ============================================================

def test_render_prometheus_has_required_sections():
    """导出文本必须含 HELP/TYPE 行与 +Inf 桶（Prometheus 解析的前提）。

    注意：直方图**没有样本时不输出任何 bucket 行**——这是 Prometheus 的标准
    行为（没有样本就没有时间序列），Grafana 上会显示 no data 而不是 0。
    所以这里先产生一个样本再断言。
    """
    metrics.LLM_DURATION.observe(0.5)   # 产生样本，否则直方图不渲染
    text = render_prometheus()
    assert "# HELP aide_http_requests_total" in text
    assert "# TYPE aide_http_requests_total counter" in text
    assert "# TYPE aide_llm_duration_seconds histogram" in text
    assert 'le="+Inf"' in text
    assert "_sum" in text and "_count" in text


def test_render_prometheus_labels_are_quoted():
    """标签值必须带引号，否则 Prometheus 解析失败。"""
    metrics.HTTP_REQUESTS.inc(1, "GET", "/health", "200")
    text = render_prometheus()
    assert 'method="GET"' in text


def test_snapshot_is_json_serializable():
    """JSON 快照必须能被序列化（/stats 端点直接用）。"""
    import json

    metrics.LLM_TOKENS.inc(100, "input")
    with metrics.timer(metrics.RETRIEVAL_DURATION):
        pass
    json.dumps(snapshot())   # 不抛异常即通过


def test_snapshot_reports_quantiles_for_histogram():
    """快照里直方图要给出 p50/p95/p99（人看的核心字段）。"""
    metrics.CHAT_TTFT.observe(0.8)
    metrics.CHAT_TTFT.observe(1.2)
    snap = snapshot()
    series = snap["aide_chat_ttft_seconds"]["series"]
    assert series, "应有样本"
    assert {"p50", "p95", "p99", "avg", "count"} <= set(series[0])


# ============================================================
# 中间件
# ============================================================

async def test_middleware_counts_request_and_status():
    """中间件要记录请求数与状态码，且路径已归一化。"""
    async def fake_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 418, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = MetricsMiddleware(fake_app)
    scope = {"type": "http", "method": "POST", "path": "/chat/abc-123/history"}
    messages = []

    async def send(msg):
        messages.append(msg)

    async def receive():
        return {"type": "http.request"}

    before = dict(metrics.HTTP_REQUESTS.snapshot())
    await mw(scope, receive, send)
    after = dict(metrics.HTTP_REQUESTS.snapshot())

    key = ("POST", "/chat/{thread_id}/history", "418")
    assert after.get(key, 0) == before.get(key, 0) + 1


async def test_middleware_records_on_exception():
    """处理过程抛异常也要记一次请求——它确实消耗了资源。

    漏记会让"错误率"永远偏低，故障时看不到真实流量。
    """
    async def boom_app(scope, receive, send):
        raise RuntimeError("handler 炸了")

    mw = MetricsMiddleware(boom_app)
    scope = {"type": "http", "method": "GET", "path": "/boom"}

    async def send(msg):
        pass

    async def receive():
        return {"type": "http.request"}

    with pytest.raises(RuntimeError):
        await mw(scope, receive, send)

    # 异常时状态码未知，记为 "0"（不是假装成功）
    assert dict(metrics.HTTP_REQUESTS.snapshot()).get(("GET", "/boom", "0"), 0) >= 1


async def test_middleware_ignores_websocket_scope():
    """WebSocket 由独立指标统计，HTTP 中间件不能重复计数。"""
    called = {"n": 0}

    async def app(scope, receive, send):
        called["n"] += 1

    mw = MetricsMiddleware(app)
    await mw({"type": "websocket", "path": "/ws/chat"}, None, None)
    assert called["n"] == 1   # 直接透传，不干预


# ============================================================
# 跨进程聚合
# ============================================================

def test_merge_sums_counters_across_processes():
    """多 worker 的计数必须相加，否则 QPS 只有真实值的 1/N。

    这是本模块最要紧的一条：指标不准比没有指标更危险——
    看到 QPS=10 实际是 40，扩容决策直接错。
    """
    snaps = [
        {"aide_llm_calls_total": [{"labels": ["ok"], "data": {"value": 3.0}}]},
        {"aide_llm_calls_total": [{"labels": ["ok"], "data": {"value": 5.0}}]},
    ]
    merged = metrics._merge(snaps)
    assert merged["aide_llm_calls_total"][("ok",)]["value"] == 8.0


def test_merge_sums_gauges():
    """连接数这类 gauge 也要相加：每个 worker 的在线数是独立的。"""
    snaps = [
        {"aide_ws_connections": [{"labels": ["user"], "data": {"value": 2.0}}]},
        {"aide_ws_connections": [{"labels": ["user"], "data": {"value": 3.0}}]},
    ]
    merged = metrics._merge(snaps)
    assert merged["aide_ws_connections"][("user",)]["value"] == 5.0


def test_merge_adds_histogram_buckets():
    """直方图必须**合并桶计数**，不能合并分位数。

    分位数不可加（两个 P95 平均不等于总体 P95），
    但桶计数可加——合并后重新算分位才等价于"把样本合在一起算"。
    """
    import math

    snaps = [
        {"h": [{"labels": [], "data": {
            "buckets": [[1.0, 1], [math.inf, 2]], "sum": 1.5, "count": 2}}]},
        {"h": [{"labels": [], "data": {
            "buckets": [[1.0, 3], [math.inf, 4]], "sum": 3.5, "count": 4}}]},
    ]
    merged = metrics._merge(snaps)
    data = merged["h"][()]
    assert data["buckets"][1.0] == 4          # 累计桶相加
    assert data["buckets"][math.inf] == 6
    assert data["count"] == 6
    assert data["sum"] == 5.0


def test_merge_keeps_label_sets_separate():
    """不同标签组合不能混：快慢路由的延迟要分开看。"""
    snaps = [
        {"h": [{"labels": ["fast"], "data": {"buckets": [[1.0, 5], [math.inf, 5]], "sum": 1, "count": 5}}]},
        {"h": [{"labels": ["slow"], "data": {"buckets": [[1.0, 0], [math.inf, 5]], "sum": 20, "count": 5}}]},
    ]
    merged = metrics._merge(snaps)
    assert set(merged["h"]) == {("fast",), ("slow",)}


def test_aggregated_render_falls_back_when_dir_unavailable(monkeypatch):
    """聚合目录不可用（只读文件系统等）时降级为单进程输出，不能 500。"""
    monkeypatch.setattr(metrics, "_metrics_dir", lambda: None)
    text = metrics.render_prometheus_aggregated()
    assert "# HELP aide_http_requests_total" in text


def test_stale_snapshot_files_are_ignored_and_cleaned(tmp_path, monkeypatch):
    """已死进程留下的快照必须跳过并清理，否则会永久虚增计数。"""
    import json
    import os

    monkeypatch.setattr(metrics, "_metrics_dir", lambda: tmp_path)
    # 造一个 10 分钟前的快照（超过 STALE 阈值）
    old = tmp_path / "99999.json"
    old.write_text(json.dumps({"aide_llm_calls_total": [
        {"labels": ["ok"], "data": {"value": 100.0}}]}), encoding="utf-8")
    stale = 300 + 60
    os.utime(old, (old.stat().st_atime - stale, old.stat().st_mtime - stale))

    loaded = metrics._load_others()
    assert all("aide_llm_calls_total" not in s for s in loaded), "陈旧快照不应被计入"
    assert not old.exists(), "陈旧快照应被清理"
