# step-45：流式输出

## 目标

用户发一条消息后不再干等整段生成完，而是**边生成边显示**。

## 问题

改动前：用户提问 → 等 8~30 秒（`LLM timeout=30`）→ 整段回答一次性出现。
用户看到的是一段长时间的"正在输入…"，无法判断系统是在思考还是卡死了。

## 实测数据（这才是关键）

先量了一下现状，因为"感觉慢"不能作为优化依据：

```
首次请求（冷启动）：
  检索耗时          19.51s   ← 加载 BGE 模型权重
  首个正文 token    6.13s
  整段完成          29.49s

第二次请求（热态）：
  检索耗时          0.02s
  整段完成           ~7s
```

**19 秒的检索耗时根本不是检索**，是 embedding 模型首次加载 torch 权重。
这意味着：**第一个用户要为所有人付这笔冷启动成本**。

所以本步做了两件事，第二件比第一件影响更大：

1. 流式输出（让等待可感知）
2. **启动预热 embedding**（把 19 秒从用户身上挪到启动阶段）

## 实现

### 后端：`astream_events` 而不是 `astream`

`astream` 只给**节点级**输出——agent 节点跑完才拿到整段 `AIMessage`，
拿不到 token。必须用 `astream_events` 捕获 `on_chat_model_stream`：

```python
async for event in graph.astream_events(input, config, version="v2"):
    if event["event"] != "on_chat_model_stream":
        continue
    chunk = event["data"]["chunk"]
    text = getattr(chunk, "content", "") or ""
    if not text:
        continue          # ← 关键
    parts.append(text)
    on_delta(text)
```

**必须过滤空 content**。实测第一轮（工具调用轮）100% 是
空 content + `tool_calls`，只有最后一轮才有正文：

```
tags=['seq:step:1'] node='agent' content=''      tool_calls=True    ← 工具轮
tags=['seq:step:1'] node='agent' content=''      tool_calls=True
...
tags=['seq:step:1'] node='agent' content='核实'   tool_calls=False   ← 最终回答
tags=['seq:step:1'] node='agent' content='。'     tool_calls=False
```

不过滤的话前端会收到大量空 delta，还可能把中间轮次的内容当成回答推给用户。

### 协议：三个新事件

| 事件 | 含义 |
|---|---|
| `reply_start` | 一条流式回复开始（前端插入占位气泡） |
| `reply_delta` | 一段增量文本（`delta` + 递增 `seq`） |
| `reply_end` | 收尾，带服务端权威 `message{id, role, content, ts}` |

为什么要 `reply_start` 而不能"收到第一个 delta 就当开始"：
前端需要明确边界来创建占位气泡。没有它就无法区分"新回复"与"上一条的补充"。

为什么 `reply_end` 要带 `id`：占位气泡在生成期间**没有数据库 id**。
收尾时用服务端 id 替换，之后重连/刷新才能通过 `mergeMessage` 按 id 对齐
（否则同一条会出现两次——这是 step-40.1 修过的同一类问题）。

### 哪些分支不流式

| 分支 | 是否流式 | 原因 |
|---|---|---|
| 正常自助问答 | ✅ | 有真实的 token 生成过程 |
| 安检拦截 | ❌ | 瞬时结果，没有可流的内容 |
| 已转人工 | ❌ | 人与人对话，没有机器人生成 |
| LLM 异常 | ❌ | 瞬时错误 |

不流式的分支仍走普通 `message` 事件，前端逻辑不变。

**流式分支不能再次整体推送**：内容已经边生成边推过了，
落库后再推一次会让用户看到两条相同内容（id 不同，去重拦不住）。

### 前端：占位气泡 + id 转正

```
reply_start → 插入 {role:'agent', content:'', streaming:true}
reply_delta → 追加到最后一条 streaming 消息上
reply_end   → 用服务端权威 message 替换（streaming:false，带上 id）
```

三个细节：

1. **空内容的占位气泡也要渲染**。否则用户看到"光标没了、气泡也没出现"
   的空窗期，直到第一个 delta 才反应
2. **迟到的 delta 要丢弃**。若 `reply_end` 已到达，后续 delta 直接忽略
   （`if (!last.streaming) return prev`）
3. **光标是纯装饰**：`aria-hidden="true"`，读屏靠外层 `aria-live` 播报整段

## 验证

### 后端测试（`tests/test_ws.py` +5 例）

| 测试 | 锁住什么 |
|---|---|
| `test_ws_streams_reply_in_deltas` | 事件顺序 start→delta*→end，收尾带 id/ts |
| `test_stream_delta_seq_is_monotonic` | `seq` 递增（便于排障判断丢包） |
| `test_streamed_reply_is_persisted_once` | **只落库一次**且内容完整（不是一堆碎片） |
| `test_guarded_reply_does_not_stream` | 拦截分支不走流式，但仍送达话术 |
| `test_live_reply_does_not_stream` | 已转人工不走流式 |

`test_streamed_reply_is_persisted_once` 是最重要的一条：流式最容易犯的错是
"边生成边落库"，数据库里会变成一堆碎片消息，刷新后历史全乱。

### 真浏览器逐帧采样

用 CDP 每 250ms 采样一次气泡长度，确认是"逐步增长"而不是"一次性出现"：

```
采样 28 帧，其中内容非空 4 帧
前几帧:
   0.27s  气泡=2 长度=   0 光标=True  typing=False
   1.53s  气泡=2 长度=   0 光标=True  typing=False
后几帧:
   6.58s  气泡=2 长度=  65 光标=True
   6.83s  气泡=2 长度= 180 光标=True
   7.08s  气泡=2 长度= 220 光标=False

长度序列是否单调增长: True  （首=23 末=220）
观察到中间态（长度在 0 与最终值之间）: True

刷新后: {"bubbles":2,"caret":false,"lastLen":220}
```

要点：长度 0 → 23 → 65 → 180 → 220 单调增长；光标在有内容时显示、
收尾时消失；**刷新后仍是 220 字且只有 2 个气泡**（id 对齐没重复）。

### 真实 WS 事件时序

```
reply_start @ 27.72s   ← 冷启动（预热前）
reply_delta 首字 @ 27.72s
reply_end @ 29.49s
delta 数: 142
拼接长度: 224 字 == 服务端 content 长度 ✓
```

预热之后：

```
首字延迟: 7.47s   总耗时: 7.87s     ← 从 27.7s 降到 7.5s
delta 数: 135
拼接长度: 211 字 == 服务端 content 长度 ✓
```

### 全量

`uv run pytest`：**219 passed**；`ruff` clean；`oxlint` / `vite build` 通过。

## 变更文件

| 文件 | 变更 |
|---|---|
| `app/api/chat.py` | 新增 `_stream_agent`；`process_message` 加 `on_delta`；`ChatOutcome` 加 `reply_id`/`reply_ts` |
| `app/api/ws.py` | `reply_start` / `reply_delta` / `reply_end` 事件 |
| `app/rag/embedder.py` | 新增 `warmup()` |
| `app/main.py` | 启动时后台预热 embedding |
| `web/src/components/UserChat.jsx` | 流式三事件处理、占位气泡、id 转正 |
| `web/src/components/MessageBubble.jsx` | 支持 `streaming` 光标；空内容占位也渲染 |
| `tests/test_ws.py` | +5 例流式测试；2 例旧测试改为断言流式协议 |
| `tests/test_ratelimit.py` | 打桩目标从 `_invoke_agent` 改为 `_stream_agent` |

## 踩的坑

### 1. 全量测试卡死（最值得记的一条）

跑全量时卡在 `test_ratelimit.py` 的 WS 用例，5 分钟超时。

原因：该用例打桩的是 `_invoke_agent`，但 WS 自助问答改成走 `_stream_agent`
了。于是测试**真的去调 LLM 和 Milvus**——不是失败，是"卡住不返回"。

教训：**改执行路径时要全局搜打桩点**。`monkeypatch` 打的是具体函数名，
路径一变，打桩静默失效，测试就变成"跑真实依赖"。

### 2. 两个旧测试断言了已废弃的行为

`test_ws_chat_ready_then_reply` 断言"收到一条 `message` 事件"，
流式后收到的是 `reply_end`。已改为断言新协议。

### 3. 首字延迟 27 秒的真相不在流式

一开始以为是流式实现慢，量了才发现是 embedding 冷启动 19 秒。
**先测量再优化**——否则会去调流式的 buffer 大小之类，方向完全错。

## 遗留问题

- **`reply_end` 之后仍会收到 `sent` 事件**。前端能处理（清 pending），
  但顺序上"回复已完成才收到发送确认"略反直觉。可以合并进 `reply_end`，
  代价是收尾事件承担两个语义。
- **没有 `[DONE]` 之类的终止语义用于错误中断**。若 LLM 在流式**中途**失败，
  当前实现会抛异常 → `error` 事件，但已推出去的半截内容留在气泡里。
  理想是推一个 `reply_abort` 让前端把气泡标记为"生成中断"。
- **前端没有"重新生成"按钮**。流式中断后用户只能重发一条。
- **检索仍是阻塞的**：`search_kb` 工具内部是同步调用，占住事件循环线程池。
  热态 0.02s 无所谓，但语料规模上来后要改异步。
- **预热只覆盖 embedding**。Milvus gRPC 连接、LangGraph 图构建、
  Checkpointer 连接池都在首次请求时才初始化，这些也值得预热。
- **没有 token 用量统计**。流式天然能统计输出 token，顺手就能做，
  但本步没做。
- **多 worker 下每个 worker 都要预热一次**（各自加载模型，内存 ×N）。
  小模型（约 100MB）可接受，换大模型时要考虑独立 embedding 服务。
