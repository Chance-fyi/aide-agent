# step-46：聚合指标

## 目标

Langfuse 能查"**这一次**对话发生了什么"，但回答不了运维真正要看的数字：

- 今天 QPS 多少？P95 延迟多少？
- 转接率多少？限流触发了几次？
- token 花了多少？其中多少是缓存命中省下来的？

本步补上这层聚合指标。

## 为什么自研而不是 prometheus-client

项目一贯风格是"避免为单一功能引入重依赖"（`security.py` 用标准库实现
PBKDF2+HMAC，`tracing.py` 强调未配置时零侵入）。而 prometheus-client 在
多 worker 下要开 `PROMETHEUS_MULTIPROC_DIR` + 共享目录 + 启动清理，配置繁琐。

但真正让我决定自研的是一个**实测发现**（见下节）——它改变了整个设计。

## 关键发现：单端口多 worker 会让指标失真

先按最朴素的方式实现（每进程内存计数 + `/metrics` 直接输出），
然后验证多 worker：

```
$ uvicorn --workers 2
$ for i in 1..10; do curl /metrics | grep 请求数; done
8, 9, 10, 1, 11        ← 中间那次换了 worker，计数从头开始
```

`uvicorn --workers N` 是**单端口多进程**，Prometheus 抓 `/metrics` 时
只能落到**随机一个 worker**。也就是说：**每个数字都只有真实值的 1/N**。

这比没有指标更危险——看到 QPS=10 实际是 40，扩容决策直接错。

所以本步的核心不是"加几个计数器"，而是**跨进程聚合**。

### 方案：文件快照 + 合并

| 方案 | 问题 |
|---|---|
| prometheus_client multiprocess mode | 要引依赖 + 环境变量 + 共享目录 + 启动清理 |
| Redis 计数 | 引新中间件，重 |
| **文件快照 + 合并** | 只需标准库；同一进程组的 worker 天然共享文件系统 |

选择文件方案。语义与 multiprocess mode 等价：

```
每个 worker:  定时把自己的快照写入 <METRICS_DIR>/<pid>.json
/metrics:     先落盘自己 → 读目录下所有快照 → 合并 → 渲染
```

**分位数为什么可以跨进程合并**：分位数本身不可加（两个 P95 平均 ≠ 总体 P95），
但**桶计数可加**。合并各 worker 的桶计数后重新算分位，等价于"把样本合在一起算"。

## 一个必须修的第二层问题

第一版只在 `/metrics` 被请求时落盘。验证时发现增量不准：

```
跑 3 个对话 → 计数只 +1     （下次抓取才补上 +4）
```

原因：**处理了对话、但之后没被抓取过的 worker，其计数不在文件里**。

修法：加后台定时落盘（`flush_loop`，10 秒一次）。10 秒对应 Prometheus
默认 15 秒抓取周期，抓取时读到的其他 worker 快照最多滞后 10 秒——
对监控场景完全够用（指标是趋势，不是实时账本）。

修完后：

```
跑 3 个对话 → 计数 +3 ✓
LLM 调用 6 次（3 对话 × 2 轮模型调用）✓
```

## 指标设计

命名遵循 Prometheus 惯例 `<namespace>_<subsystem>_<name>_<unit>`：

| 指标 | 类型 | 用途 |
|---|---|---|
| `aide_http_requests_total{method,path,status}` | counter | QPS、错误率 |
| `aide_http_request_duration_seconds{method,path}` | histogram | 接口 P95 |
| `aide_chat_messages_total{channel,outcome}` | counter | 对话量、转接率、拦截率 |
| `aide_chat_duration_seconds{channel}` | histogram | **用户感知的等待时间** |
| `aide_chat_ttft_seconds` | histogram | **首字延迟**（流式体验核心） |
| `aide_llm_calls_total{outcome}` | counter | 调用量、失败分类 |
| `aide_llm_duration_seconds` | histogram | 单次模型调用耗时 |
| `aide_llm_tokens_total{direction}` | counter | 成本核算 |
| `aide_llm_cache_read_tokens_total` | counter | 前缀缓存省了多少 |
| `aide_retrieval_duration_seconds` | histogram | 检索耗时 |
| `aide_retrieval_results_total{bucket}` | counter | 空结果率（检索质量信号） |
| `aide_transfers_total{trigger}` | counter | 转接次数（LLM 判断 vs 关键词兜底） |
| `aide_ratelimit_hits_total{kind}` | counter | 限流触发（窗口 vs 并发闸门） |
| `aide_ws_connections{side}` | gauge | 在线连接数 |
| `aide_ws_events_total{type}` | counter | WS 事件量 |

### 标签基数纪律

**绝不把 `thread_id` / `user_id` / 查询文本当标签**。每个不同的值都会创建
一条新时间序列，几万个会话就能把内存和 Prometheus 抓取压垮。

所以 HTTP 中间件对路径做归一化：

```python
/chat/9f1c2e3d-aaaa-.../history  →  /chat/{thread_id}/history
```

有参数化测试锁住这条（100 个随机 UUID 归一化后只有 1 种形态）。

### 为什么用固定桶而不是记录所有样本

保留全部样本会让内存随请求量线性增长（一天几十万请求 = 几十万浮点数）。
分位数只需要近似值。桶按用途分设（LLM 0.25~90s、快操作 5ms~30s、
TTFT 0.1~30s），桶边界贴近实际分布才有意义。

## 输出两个端点

| 端点 | 格式 | 鉴权 | 用途 |
|---|---|---|---|
| `GET /metrics` | Prometheus text | 无 | 给 Prometheus 抓取 |
| `GET /stats` | JSON（含 p50/p95/p99） | 坐席登录 | 给人看、管理台展示 |

`/metrics` 不鉴权：只暴露聚合计数，标签是有限枚举，**不含任何用户数据**。
生产应由反向代理限制来源 IP，而不是应用层 Token（否则 Prometheus 要配轮换）。

## 实现中的两个语义修正

### 1. `LLM_DURATION` 不能记总耗时

第一版在 `_stream_agent` 末尾记一次总耗时。但一轮对话至少调两次模型
（工具轮 + 最终轮），这样记会把"检索 + 两次模型"混成一个数，调用次数也只算 1。

改为按 `on_chat_model_start` / `on_chat_model_end` **成对计时**：

```
修正前: aide_llm_calls_total{ok} 1    aide_llm_duration_count 1
修正后: aide_llm_calls_total{ok} 2    aide_llm_duration_count 2   ← 符合实际
```

### 2. 异常也要计入请求数

中间件在 `finally` 里记录：客户端中途断开、handler 抛异常都算一次请求
（它确实消耗了资源）。漏记会让"错误率"永远偏低，故障时看不到真实流量。

## 验证

### 单元测试（`tests/test_metrics.py`，27 例）

| 测试 | 锁住什么 |
|---|---|
| `test_counter_accumulates_per_label_set` | 按标签分组累加 |
| `test_counter_rejects_wrong_label_count` | 标签个数错立刻报错（静默容忍比没指标更糟） |
| `test_histogram_counts_are_cumulative` | 桶计数是累计值（Prometheus 语义） |
| `test_histogram_inf_bucket_is_always_last` | +Inf 桶必须存在且最后 |
| `test_quantile_estimate_is_reasonable` | 均匀分布下 P50/P95 接近真实值 |
| `test_quantile_none_when_empty` | 空返回 None（0 会被误读成"很快"） |
| `test_quantile_separates_label_sets` | 快慢路由分位数独立 |
| `test_normalize_path_collapses_dynamic_segments` ×5 | 路径归一化 |
| `test_normalized_paths_are_finite` | 100 个 UUID → 1 种形态（基数安全） |
| `test_render_prometheus_has_required_sections` | HELP/TYPE/+Inf |
| `test_snapshot_is_json_serializable` | JSON 快照可序列化 |
| `test_middleware_counts_request_and_status` | 中间件记录请求 + 状态码 |
| `test_middleware_records_on_exception` | 异常也计入 |
| `test_middleware_ignores_websocket_scope` | WS 不重复计数 |
| **`test_merge_sums_counters_across_processes`** | 跨进程计数相加 |
| **`test_merge_sums_gauges`** | gauge 相加 |
| **`test_merge_adds_histogram_buckets`** | 合并桶计数而非分位数 |
| `test_merge_keeps_label_sets_separate` | 不同标签不混 |
| `test_aggregated_render_falls_back_when_dir_unavailable` | 目录不可用降级不 500 |
| `test_stale_snapshot_files_are_ignored_and_cleaned` | 死进程快照跳过并清理 |

### 真实环境：单 worker

```
指标行数: 118
连续 3 次 /metrics 自身计数: 4 → 5 → 6   （单调递增）
```

### 真实环境：多 worker（关键验证）

```
$ uvicorn --workers 2
落盘文件: 2333104.json  2333105.json     ← 两个 worker 各自落盘

发起 3 个对话 → answered 计数 +3 ✓
LLM 调用 6 次（3 对话 × 2 轮模型调用）✓
token 累计: input 14512 / output 1110
```

修复前后对比：

```
修复前: 8, 9, 10, 1, 11      ← 换 worker 后回退
修复后: 1, 1, 2, 3, 4, 5...  ← 单调递增
```

### 全量

`uv run pytest`：**246 passed**（219 + 27）；`ruff` clean。

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/core/metrics.py` | **新增**：Counter/Gauge/Histogram、指标定义、HTTP 中间件、跨进程聚合 |
| `app/api/routes.py` | 新增 `GET /metrics`（无鉴权）、`GET /stats`（坐席鉴权） |
| `app/main.py` | 注册中间件、启动指标落盘任务 |
| `app/api/chat.py` | LLM 调用/TTFT/token/对话量埋点；`_error_kind`、`_record_usage` |
| `app/agents/graph.py` | 检索耗时、空结果率、转接来源埋点 |
| `app/api/ws.py` | WS 连接数埋点 |
| `app/core/ratelimit.py` | 限流触发埋点（区分窗口/并发闸门） |
| `tests/test_metrics.py` | **新增**：27 例 |

## 遗留问题

- **聚合目录必须"同进程组共享、不同实例隔离"**。`uvicorn --workers` 天然共享
  `/tmp`；容器多实例各自独立 `/tmp` 也天然隔离。但如果同机跑多个独立实例
  且共享同一 `METRICS_DIR`，会错误合并（文档需注明）。
- **`/stats` 只反映当前进程**（响应里有 `pid_note` 提示）。多 worker 下要看
  全量请用 `/metrics`。
- **gauge 合并语义是"相加"**。对"当前连接数"正确；但如果将来加
  "配置版本号"这类 gauge，相加就错了，需要区分处理。
- **没有告警规则**。指标有了，但"P95 超过 10 秒告警"这类规则要写在
  Prometheus/Alertmanager 侧，本步不做。
- **没有管理台可视化**。`/stats` 是 JSON，没接到前端页面上。
- **token 统计依赖厂商返回 usage**。部分兼容接口不返回，缺失时静默跳过
  （指标会偏低但不报错）。
- **聚合有最长 10 秒延迟**（后台落盘周期）。监控场景可接受，
  但不能用来做实时计费。
- **指标不持久化**：进程重启后归零。这是 Prometheus 模型的正常行为
  （它自己存时序），但如果有人想直接查 `/metrics` 算历史，会拿不到。
