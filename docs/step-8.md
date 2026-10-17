# Step 8 — /chat 接口串联全链路

## 目标

RAG 检索 → Agent → 状态机 → 会话历史，全部通过 HTTP 暴露。

## 接口清单

| 接口 | 说明 |
|---|---|
| `POST /chat` | 核心对话：`{message, thread_id?}` → `{message, thread_id, transferred}` |
| `GET /chat/{thread_id}/history` | 会话历史 + 转接状态（坐席端/调试用） |
| `POST /debug/kb/search` 等 | 调试路由（DEBUG=false 不注册） |

`thread_id` 语义：不传则新建并随响应返回；前端首轮存下后原样携带，
Checkpointer 自动恢复上下文——**多轮协议只需要这一个字段**。

## 关键实现决策

1. **连接池与图全局单例**（`app/agents/runtime.py`，懒加载 + 锁）：
   Postgres 连接池建立要上百毫秒，不能每请求重来
2. **必须用异步链路**：`AsyncConnectionPool` + `AsyncPostgresSaver` +
   `await graph.ainvoke()`。同步 PostgresSaver 搭 ainvoke 会在
   `aget_tuple` 直接 NotImplementedError——官方给它只实现了同步方法
3. **池启动等待**：`open(wait=True)` 阻塞到连接就绪，Postgres 未起时
   报"连接池无法就绪"而非模糊超时

## 踩坑记录

| 现象 | 根因 |
|---|---|
| `'...ContextManager' object has no attribute 'setup'` | `from_conn_string` 是 context manager 用法，单例场景应直接 `PostgresSaver(pool)` 构造 |
| `NotImplementedError` | 同步 Saver 被异步调用 |
| `'_thread.lock' does not support async` | 异步函数里用了 `threading.Lock`，必须换 `asyncio.Lock` |
| `bound to a different event loop` | TestClient 每请求新开 event loop 所限（测试环境假象），真实 uvicorn 单 loop 无此问题——**用真实 uvicorn + curl 验证而非 TestClient** |

## 已验证（真实 uvicorn 进程）

- 关键词转接：`transferred: true`，话术正确
- 状态守卫：转接后第二轮仍返回等待话术
- 历史：消息正确含 user/assistant 序列，`status: pending_human`
- 404：不存在会话正确返回

## 遗留

- agent 路径（真实 LLM 回答）待 .env 配置后端到端验证
