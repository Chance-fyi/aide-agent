# Step 14 — 转接状态机完善：排队、坐席接入、超时回退

## 目标

旧状态机只有 active/pending_human 两态：一旦转接，会话永远卡在
"坐席接入中"死话术——没有排队的概念，没有坐席接入入口，也没有超时出路。

## 状态机设计

```
active ──触发转接──> queued ──坐席接入──> in_service ──处理结束──> closed
   ▲                    │                                        │
   └── 排队超时自动回退 ─┘          closed 会话新消息视为新问题 ────┘
```

- `queued`：排队等坐席；超时（`TRANSFER_QUEUE_TIMEOUT_MIN`，默认 30 分钟）自动回退
- `in_service`：坐席接入中；**只能由坐席动作产生**（agent_accept），
  节点/Agent 无权推进——否则"排队"名存实亡
- `closed`：处理完成；不自动回 active，而是用户发新消息时惰性复位
- 遗留 `pending_human`（v1 数据）：视同 queued 参与守卫/回退/关闭，存量工单不丢

## 实现要点

### 1. handoff.py（服务层）

- `agent_accept(tid, agent_name)`：`UPDATE ... WHERE status='queued' RETURNING`，
  原子防双坐席；非排队态返回 False
- `agent_close(tid, resolution)`：queued/in_service/pending_human → closed
- `sweep_timed_out()`：幂等 UPDATE 回退超时排队；**只回退 queued**，
  in_service 是坐席正在处理，不能自动打断
- `reset_active(tid)`：closed 会话新消息时惰性复位
- 幂等迁移：`ADD COLUMN IF NOT EXISTS agent_name/resolution`
  （CREATE TABLE IF NOT EXISTS 不会给存量表加列——测试踩出来的）

### 2. 接口层

- `POST /agent/threads/{tid}/accept` / `/close`：坐席端数据入口。
  冲突用 409（已被接走/超时回退/不存在，对坐席是同一个含义：刷新工单列表）
- transfer_node 话术分流：排队中 vs 坐席服务中给不同回复
- ChatResponse.transferred：`is_live_status()`（queued/in_service 都算已转接），
  前端无需改协议

### 3. 超时回退的执行方式

lifespan 起后台 asyncio 任务周期调 `sweep_timed_out()`（`to_thread` 包同步 DB 调用）：
- 不引独立 worker/DB 定时器，单进程自足
- 多 worker 重复执行无害（幂等 UPDATE）
- 巡检循环吃掉 DB 异常继续跑，DB 抖动不杀任务；关闭时 cancel 干净退出

## 验证

```bash
uv run pytest          # 46 passed
uv run ruff check .    # 0 issues
```

- test_handoff：完整生命周期、防双坐席、queued 直关、老数据兼容、
  超时回退只碰"过期排队"（改 updated_at 模拟等待）
- test_api：转接→接入→关闭全流程、accept/close 409

## 遗留问题

- /agent 接口暂无鉴权（坐席端迭代时统一接入）
- 排队列表接口（坐席端"工单列表"）留到模拟坐席端步骤
- 状态查询走同步短连接，统一连接池在后项优化
