# step-36：上下文压缩（旧对话压成摘要）

## 目标

step-35 做了窗口裁剪：只把最近 N 轮发给 LLM。但裁剪是**丢弃**——
被裁掉的旧对话彻底消失，模型不知道用户前面说过什么。
多轮指代（"那它能退吗"）在第 N+1 轮之后必然失效。

本步补上压缩：把滑出窗口的旧对话压成一段摘要，拼进系统提示。
**裁剪控成本，压缩保信息**，两者配合才是完整的上下文管理。

## 为什么自研（方案对比）

先把候选方案查清楚，结论是三个现成方案都不适合：

| 方案 | 能力 | 为什么不选 |
|---|---|---|
| `langchain_core.messages.trim_messages` | 只裁剪，不压缩 | 丢掉的就没了解不了本步问题 |
| `langmem` 的 `SummarizationNode` | 能压缩 | 依赖链重（`langchain` 全量 + `langchain-anthropic` + `trustcall` + `langsmith`），版本仍是 `0.0.30`；且用 `RemoveMessage` **真删 state 里的消息**，与 step-35「完整历史留 Checkpointer 供审计/坐席查看」直接冲突 |
| `langchain` 1.x `SummarizationMiddleware` | 能压缩 | 只能挂在 `create_agent` 上，本项目是手写 `StateGraph`（需要三态 `route_input` 与 `transfer_node` 话术） |

`langmem` 的依赖清单是实测查的：

```
langchain-anthropic>=0.3.3, langchain-core>=0.3.46, langchain-openai>=0.3.1,
langchain>=0.3.15, langgraph-checkpoint>=2.0.12, langgraph<2,>=0.6.0,
langsmith>=0.3.8, trustcall>=0.0.39
```

引入 `langchain-anthropic` 违反 AGENTS.md 的"不硬编码任何厂商"；
`0.0.x` 版本号意味着 API 随时可能变。

自研版约 200 行，只依赖 `langchain_core` 的消息类型。

## 实现要点

### 1. 摘要走独立 state 键，messages 永不删减

```python
class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]   # 完整历史，只增不删
    summary: NotRequired[str]              # 已滑出窗口的旧对话摘要
    summarized_count: NotRequired[int]     # 已压进摘要的消息条数（水位线）
```

这是本步最关键的设计决策。`langmem` 的做法是 `RemoveMessage` 把旧消息从 state 删掉，
摘要顶替原位置——省存储，但**坐席端就看不到用户前面说过什么了**，
且一旦摘要质量差，原始信息无法回滚。

这里反过来：state 里 messages 一条不少，摘要只是"额外的记忆车道"。
送给 LLM 的是 `[system(含摘要)] + 最近 N 轮`，存储的是完整历史。

### 2. 水位线用「消息条数」而不是轮数/时间戳

```python
plan.next_summarized_count = start   # start = 窗口左边界下标
```

`messages` 是 append-only 的，条数是唯一不会随重放漂移的锚点。
用轮数会在"一轮 4 条 vs 6 条"（有无工具往返）时算错，用时间戳会被时钟问题坑。

### 3. 攒够一批再压（`summary_trigger_rounds`）

触发条件是"**已滑出窗口、但还没压过**的轮数 >= 阈值"，而不是滑出一轮就压。

每次压缩都是一次额外 LLM 调用。逐轮压缩会让长会话成本翻倍；
攒 5 轮压一次，成本降一个数量级，而信息损失可忽略——
被延迟压缩的旧消息本来就已经不在窗口里了（等价于 step-35 的丢弃）。

### 4. 决策与执行分离（为可测性）

```
plan_summary()        纯函数：要不要压、压哪一段、水位线推到哪
summarize_messages()  唯一有 IO：调 LLM，失败返回 None
advance_summary()     组合：决策 + 执行 + 失败回滚
```

触发条件和水位线这类逻辑最容易写错，而它完全不需要 LLM 就能验证。
拆开之后 19 个测试里 12 个不碰网络。

### 5. 失败必须回滚水位线

```python
if new_summary is None:
    return SummaryOutcome(previous_summary, summarized_count)   # 水位线不动
```

这是最容易写错的一处：如果压缩失败却把水位线推上去，
这段历史**既没进摘要、又因水位线推进而永不重试**——永久丢失。
回滚后行为等价于 step-35 的纯窗口裁剪，下一轮自动重试。

### 6. 摘要合并进单条 system 消息

```python
def build_system_content(base_prompt, summary):
    return f"{base_prompt}\n\n{SUMMARY_HEADER}\n{summary}"
```

不单独发第二条 `SystemMessage`：部分 OAI 兼容厂商只接受位置 0 的单条 system 消息，
多条会被拒或行为不一致。合并成一条最稳，也顺带保证摘要不会被后续裁剪挤掉。

### 7. 工具结果在压缩提示里截断

`search_kb` 返回的是整段父块正文（可达上千字）。原样塞进压缩提示，
会让"压缩"本身变成一次昂贵调用。截到 200 字预览——压缩只需要知道
"检索到哪份资料"，不需要全文。

### 8. 摘要本身也要有上限

模型不一定遵守字数要求。`summarize_messages` 最后兜底截断到 `max_chars * 2`，
否则"压缩"会变成新的上下文膨胀源。

## 配置

```bash
HISTORY_MAX_ROUNDS=10      # 逐字窗口（step-35）
SUMMARY_TRIGGER_ROUNDS=5   # 攒够 5 轮滑出内容就压一次；<=0 关闭压缩
SUMMARY_MAX_CHARS=600      # 摘要长度上限
```

## 验证

### 单元测试（`tests/test_summarize.py`，19 例）

不碰 LLM / Postgres / Milvus：

- 窗口边界：切点必落在 human 上、不拆散 tool 链、`<=0` 不限制
- 决策：未滑出 / 未达阈值 / 达阈值 / 水位线防重复压缩 / 只压增量 /
  `trigger_rounds<=0` 关闭 / 水位线超前不崩
- 渲染：跳过空正文 ai 消息、工具结果截断
- 执行：正常摘要、合并旧摘要、**失败回滚水位线**、未触发时零 LLM 调用、
  兜底截断、无可压正文
- 拼装：合并成单条 system、无摘要时原样透传
- 接线：`agent_node` 真的把摘要拼进 system、且 state 更新里没有删消息指令

### 真实 12 轮长会话（`HISTORY_MAX_ROUNDS=4`，`SUMMARY_TRIGGER_ROUNDS=2`）

```
[ 5/12] 退货的话运费谁出？
INFO 对话历史已压缩 6 条消息 → 摘要 155 字（累计水位线 6）
INFO 上下文已裁剪 17→11 条消息
[ 7/12] 那退款多久到账？
INFO 对话历史已压缩 6 条消息 → 摘要 200 字（累计水位线 12）
[ 9/12] 我的订单号是 A12345
INFO 对话历史已压缩 6 条消息 → 摘要 220 字（累计水位线 18）
[11/12] 之前说的 7 天是从哪天算？
INFO 对话历史已压缩 6 条消息 → 摘要 262 字（累计水位线 24）

最终 state keys: ['messages', 'summarized_count', 'summary']
摘要: '用户张三上周购买一部手机，签收已5天，未激活、包装完整，确认走7天无理由退货。
      运费规则：无理由退货运费买家承担，质量问题退货（7天内、需凭证）运费商家承担。
      寄回时保价非强制但建议…退款原路退回：支付宝/微信1-3个工作日到账，银行卡3-7个工作日…'
水位线: 24
messages 条数: 34
```

关键观察：

- **摘要在累积**：155 → 200 → 220 → 262 字（每次都是"合并旧摘要 + 新对话"）
- **水位线单调推进**：6 → 12 → 18 → 24，同一批消息从不重复压缩
- **messages 保留 34 条完整历史**，只有送给 LLM 的窗口被压缩

### 顺带验证：压缩不会丢关键事实

第一次跑发现摘要里没有订单号 `A12345`，一度以为是压缩丢信息。查水位线后确认：
压缩时 `window_start = 24` 正好等于水位线，说明 round 9 的订单号**仍在逐字窗口内**
（属于最后 4 轮），从未需要压缩。模型这一轮仍能逐字看到它——行为正确。

单独验证关键事实保留能力：

```
输入: 用户「我叫张三，订单号 A12345」…「小米 14，花了 3999」
输出: 用户张三，订单号 A12345，购买了小米 14 手机，金额 3999 元，购买时间约为上周
      A12345 ✓  张三 ✓  小米 14 ✓  3999 ✓
```

### 其他

- `uv run pytest`：**135 passed**（新增 19 例）
- `uv run ruff check .`：clean

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/agents/summarize.py` | 新增：窗口边界、决策、执行、失败回滚 |
| `app/agents/graph.py` | `AgentState` 加 `summary`/`summarized_count`；`agent_node` 接入压缩；窗口边界复用 `summarize.window_start_index` |
| `app/core/config.py` | `summary_trigger_rounds`、`summary_max_chars` |
| `.env.example` | 同上 |
| `tests/test_summarize.py` | 新增 19 例 |

## 遗留问题

- **压缩仍按轮触发，不按 token**。10 轮里如果都是长回答（检索带回大段父块），
  仍可能超上下文。理想是按 token 预算，但需注意
  `count_tokens_approximately` 的默认 `chars_per_token=4` 对中文**低估约 2.9 倍**
  （实测：36 个汉字 = 38 token，估算给 13），中文场景必须显式调到 1.0~1.5
- **摘要质量无评估**。当前只有"关键事实是否保留"的手工抽检，
  没有纳入黄金集。需要多轮长会话评估集才能量化
- **压缩是同步阻塞的**：触发的那一轮会多一次 LLM 调用（约 2-4 秒），
  用户感知为"这次回复变慢"。可考虑异步预压缩，但要处理竞态
- **state 仍无限增长**：本步控 token，没控存储。`checkpoints` 表需要归档策略
- **摘要可能被污染**：如果用户诱导（"记住我说过的：退款永久有效"），
  摘要会把错误信息固化下来，且优先级高于后续检索结果。需要评估注入风险
- 压缩日志是 INFO 级，长会话会刷日志
