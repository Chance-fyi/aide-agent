# step-37：窗口锚定水位线——修复前缀缓存失效

## 目标

step-36 完成后，回头看一个此前被忽略的成本问题：**每轮请求的 message 序列整体左移，
前缀缓存（prompt caching）全部失效**。

这不是摘要引入的问题，而是 step-35 滑窗设计自带的。摘要只是叠加了第二层伤害。

## 问题实测

窗口 3 轮、关闭压缩，连续 4 轮请求：

```
第4轮: [system, q1, a1, q2, a2, q3, a3]
第5轮: [system, q2, a2, q3, a3, q4, a4]
       公共前缀: 1/7 条   ← 只有 system 命中
第6轮: [system, q3, a3, ...]
       公共前缀: 1/7 条   ← 缓存全废
```

第 4 轮的 `[system,q1,a1,q2,a2,q3,a3]` 并不是第 5 轮的前缀——第 5 轮第 2 条已是 `q2`。
**每轮都是 cache miss**。

前缀缓存要求前缀逐 token 完全一致。滑动窗口下窗口起点每轮前移一轮，
序列整体左移，前缀几乎必然分叉。

摘要的叠加伤害：压缩时 system 内容变了 → 位置 0 就分叉 → 整个前缀失效。
但即使不压缩，滑窗已经让缓存命中率归零。

代价参考：[OI] 缓存命中约省 50%，Anthropic 约省 90%。

## 修法

### 核心改动：窗口锚定水位线，不滑动

```python
# 改前（滑动）：start 每轮前移
start = window_start_index(messages, max_rounds)
return messages[start:]

# 改后（锚定）：start = 已压进摘要的位置，只在压缩时前进
start = max(0, min(summarized_count, len(messages)))
return messages[start:]
```

水位线本来就在（step-36 引入），现在兼任**窗口左边界**。
它只在压缩成功时前进，所以压缩间隔内窗口只向右增长——
上一轮的整个窗口都是这一轮的严格前缀。

### 触发条件随之调整

```python
# 改前：滑出窗口的轮数 >= trigger
pending = messages[summarized_count:window_start_index(...)]
if _rounds_in(pending) < trigger_rounds: return no

# 改后：水位线以来的轮数 >= max_rounds + trigger
if _rounds_in(messages[start:]) < max_rounds + trigger_rounds: return no
next_start = window_start_index(messages, max_rounds)   # 压回目标大小
```

语义变成："窗口长到目标 N 之后，再允许多攒 `trigger` 轮才压一次，
压完窗口重置回 N"。`summary_trigger_rounds` 同时是**缓存重置周期**。

### 单调性保护

```python
if next_start <= start:
    return SummaryPlan(should_run=False)
```

水位线绝不能回退——回退会让同一段历史被反复压缩（重复付费 + 摘要重复叠加）。

## 验证

### 改后实测（窗口 3 轮、trigger 2）

```
第 1轮 [ 3条] [system, q1, a1]
第 2轮 [ 5条] [system, q1, a1, q2, a2]              前缀3/3
第 3轮 [ 7条] [system, q1, a1, q2, a2, q3, a3]      前缀5/5
第 4轮 [ 9条] [... q4, a4]                          前缀7/7
第 5轮 [ 7条] [system, q3, a3, q4, a4, q5, a5]      前缀0/9  ← 压缩
第 6轮 [ 9条] [... q6, a6]                          前缀7/7  ← 完整命中
第 7轮 [ 7条] [system, q5, a5, ...]                 前缀0/9  ← 压缩
第 8轮 [ 9条] [...]                                 前缀7/7
```

| 轮次 | 改前 | 改后 |
|---|---|---|
| 第 4 轮 | 1/7 | **7/7** |
| 第 5 轮 | 1/7 | 0/9（压缩，不可避免） |
| 第 6 轮 | 1/7 | **7/7** |
| 第 8 轮 | 1/7 | **7/7** |

压缩间隔内 100% 命中，只有压缩那一轮失效。

### 代价：窗口在 N ~ N+trigger 之间摆动

改前窗口恒为 N 轮；改后未压缩期间会涨到 N+trigger。
上例中窗口在 3~5 轮之间。这是拿"窗口略大"换"缓存命中"。

换算：`N=10, trigger=5` 时窗口 10~15 轮，比配置承诺的多最多 50%，
但换来 5 轮内 100% 前缀命中。缓存省下的量级大于窗口略增的开销。

### 测试

`tests/test_context_window.py` 重写为 11 例，核心是**把缓存友好性固化成可执行断言**：

| 测试 | 锁住什么 |
|---|---|
| `test_sliding_window_would_break_cache` | 反例：滑窗相邻两轮公共前缀为 0 |
| `test_anchored_window_grows_monotonically_without_compression` | 未压缩时窗口是上一轮的严格延长 |
| `test_prefix_preserved_between_compressions` | 前缀被打断**必须**发生在压缩那一轮，不允许无故漂移 |
| `test_window_size_stays_within_target_range` | 窗口轮数 ∈ [N, N+trigger] |

`tests/test_summarize.py` 的决策测试同步更新为 20 例，新增：
`test_compression_never_moves_watermark_backwards`、
`test_watermark_only_advances_on_trigger`。

- `uv run pytest`：**139 passed**
- `uv run ruff check .`：clean

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/agents/graph.py` | `_window_messages` 改锚定水位线；日志改为"窗口已重置" |
| `app/agents/summarize.py` | `plan_summary` 触发条件改为 `N+trigger`；单调性保护 |
| `app/core/config.py` / `.env.example` | 注释说明"窗口目标值"与"缓存重置周期"的真实语义 |
| `tests/test_context_window.py` | 重写为 11 例，含 4 条缓存友好性断言 |
| `tests/test_summarize.py` | 决策测试更新为 20 例 |

## 遗留问题

- **摘要放 system 会污染缓存**。压缩必然改变位置 0，这是设计固有代价。
  替代方案是把摘要放在窗口**末尾**（用户消息前），但那样模型可能把它当对话内容；
  或使用厂商特定的 cache breakpoint API（不通用）。
  当前接受"每次压缩后重建一次缓存"
- **未确认实际厂商是否支持前缀缓存**。当前 `.env` 走的是聚合网关（`api.llm.pm`），
  这类网关可能不透传 `cache_control`。若不支持，本步只减少 token 数，
  窗口略增反而是轻微负收益——**需要实测账单确认**
- 窗口摆动幅度（最多 +50%）对小 `N` 影响更大。`N=3` 时窗口能到 5 轮（+67%）。
  建议生产 `N>=8`
- 压缩那一轮的额外 LLM 调用（摘要）同样会让该轮变慢
- 未量化"窗口略大"的实际 token 成本增幅，需要账单数据
