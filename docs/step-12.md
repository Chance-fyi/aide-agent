# Step 12 — LLM 异常分级、友好错误与 /chat 链路健壮性

## 目标

LLM 是整条链路最脆弱的外部依赖（网络抖动/限流/Key 失效超预期），
本步让失败"可区分、可读、可恢复"，并把 /chat 链路上其余裸抛点补齐。

## 实现要点

### 1. 异常分级映射（app/api/chat.py `_friendly_llm_error`）

| 异常 | 文案 |
|---|---|
| `APITimeoutError` | "AI 服务响应超时" |
| `APIConnectionError` | "暂时无法连接 AI 服务" |
| `RateLimitError` | "当前咨询人数较多" |
| `AuthenticationError` | 通用话术 + 日志 CRITICAL（配置事故，运维必须马上处理） |
| 其他 | 通用"服务暂时不可用" |

- 全部 503：失败已重试过（max_retries=2），客户端可择机重试
- 注意 `APITimeoutError` 是 `APIConnectionError` 的**子类**，判断顺序不能反
- 对外绝不泄露堆栈/base_url/模型名；细节全在 logger.exception 里

### 2. 空回复兜底

LLM 偶发只回 tool_calls 不产文本，`reply.text()` 为空时前端会渲染空白气泡，
现兜底"回复没有生成成功，请再试一次"。

### 3. 状态查询降级（三处）

handoff 状态查询（同步 psycopg）失败都不再拖垮主流程：
- chat.py 尾部查询 → 降级 transferred=False
- graph.route_input 守卫 → 按 active 放行（**测试发现的漏网之鱼**）
- history 接口整体包 try/except → 503

原则：状态查询失败的代价（极端情况多转一次人工/少一次 UI 提示）
远小于整条对话报废。

### 4. history 接口加固

- thread_id 路径参数加 `pattern=^[A-Za-z0-9_\-]{1,64}$`（纵深防御 + 异形 ID 不进 Checkpointer）
- 业务 404 与基础设施故障分开：404 透传，其他异常 503

### 5. 顺手修复

- main.py 的 print 换成 logging + basicConfig（分级日志的前提）
- **tests 撞出隐藏 bug**：runtime 的 asyncio.Lock/连接池绑定事件循环，
  每测试新循环会 `bound to a different event loop`。
  pytest-asyncio 1.4 支持 session 级循环，配 `asyncio_default_*_loop_scope = "session"`，
  于生产"单事件循环"语义一致

## 验证

```bash
uv run pytest          # 25 passed
uv run ruff check .    # 0 issues
```

新增 5 用例：超时/限流映射、状态查询降级、非法 thread_id 422。

## 遗留问题

- handoff 同步短连接仍是热路径隐患（统一连接池排在 step-15+）
- langchain-openai 后续版本可能改变异常包装方式，升级依赖时需回归本组用例
