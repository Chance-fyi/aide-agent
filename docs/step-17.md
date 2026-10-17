# Step 17 — 分割策略可配置 + 效果对比工具

## 目标

切分策略的选择不能靠直觉：header/paragraph/fixed 各有适用场景，
需要用真实查询的检索效果说话，并能低成本切换生产口径。

## 实现

### 1. 三种策略（app/rag/splitter.py）

| 策略 | 逻辑 | 适用 |
|---|---|---|
| header（默认） | 按 #/## 标题切，标题进元数据 | 结构化文档（本语料） |
| paragraph | 无视标题，空行分段聚合为小节 | 结构混乱/伪标题文档 |
| fixed | 固定窗口滑切，块自为父块 | 对照基准 |

- `split_markdown(text, source, strategy)` / `load_and_split_all(dir, strategy)`
  均支持策略参数；None 时读 `KB_SPLIT_STRATEGY` 配置（生产口径单点）
- 非法策略显式 ValueError，不静默回落

### 2. 对比工具（/debug/kb/split_compare）

- 参数：`strategies`（默认三种全比）、`q`（对比查询）、`top_k`
- 输出：各策略切块统计（数量/均长/最长）+ 同一查询的内存检索 top_k
- 不带 q 只看统计（不触发 embedding，秒回）；带 q 走真实向量检索
- 判读：目标知识应排第一且分数明显甩开第三名

### 3. 切换生产口径的流程

```
/debug/kb/split_compare?q=... 看数据
→ .env 改 KB_SPLIT_STRATEGY + bump KB_SCHEMA_VERSION
→ POST /debug/kb/rebuild（蓝绿：冒烟通过才切换）
```

策略与 schema 版本联动：不同策略切出的块口径不同，
必须换集合，蓝绿机制（step-15）保证平滑。

## 实测结论（本语料，4 组查询）

- **header 最优**：31 块粒度细，top1 与 top3 区分度大
  （如"退款多久到账"：0.73 vs 0.65，第三名已是不相关内容）
- paragraph/fixed 在本语料上退化为"整文件一块"
  （文件只有几百字，段落聚合/固定窗口都聚到文件级），
  top3 区分度差（0.75 vs 0.60 都来自同一文件的模糊信号）
- 结论：结构化 markdown 语料坚持 header；将来混入长文档再重新对比

## 验证

```bash
uv run pytest          # 60 passed
uv run ruff check .    # 0 issues
```

新增 4 用例：三策略口径断言、非法策略拒绝、
split_compare 统计模式（无 q 不触发 embedding）、未知策略 422。

## 遗留问题

- 对比工具用内存检索（与 Milvus 链路口径一致性已在 step-5 验证过）
- 策略评估仍是"人工看 top_k"，评估集落地后可做成自动打分（recall@k）
