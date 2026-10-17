# Step 15 — Milvus 版本化集合 + 蓝绿重建 + 换模型处理

## 目标

解决两个生产痛点：
1. **全量重建期间检索不可用/重建失败污染线上数据**：
   旧实现直接 drop 集合重建，中途失败 = 知识库清零
2. **换 embedding 模型没有安全路径**：新旧向量维度/空间不同，
   直接灌进同一集合必然检索错乱

## 设计：别名 + 版本化集合（蓝绿部署）

```
kb_chunks (别名，永远指向线上版本)
   └──> kb_chunks_v1_3f2a9c1b        ← 当前版本
        kb_chunks_v2_8b71e0d4_building ← 重建中的 staging
```

- **集合名指纹** = sha1(embedding模型 | 维度 | schema版本) 取 8 位：
  - 换 embedding 模型 → 新指纹 → 新集合（向量空间天然隔离）
  - 切分逻辑/表结构变化 → 手动 bump `KB_SCHEMA_VERSION` → 新集合
- **检索永远走别名**（`_resolve_collection`），调用方无感知版本切换
- **重建流程**：
  1. 语料 → 向量化 → 写入 staging（`_building` 后缀）
  2. **冒烟测试**：预设业务查询（`REBUILD_SMOKE_QUERIES`，默认退货/退款/发货）
     必须命中且分数 ≥ `RETRIEVAL_MIN_SCORE`
  3. 清旧集合 → staging 改正名 → **创建别名（= 上线时刻）**
  4. 任一步失败：删 staging、报错，旧版本原样在线
- 别名不能与现存集合同名——所以清理在前、建别名在后（踩出来的 Milvus 约束）
- v0 遗留裸集合 `kb_chunks`：`_resolve_collection` 兼容回落，首次 rebuild 后归位

## 换 embedding 模型的操作手册

```bash
# 1. .env 改三项（维度必须与模型匹配，否则冒烟测试直接拦下）
EMBEDDING_MODEL=BAAI/bge-base-zh-v1.5
EMBEDDING_DIM=768
# 2. 重建：新指纹 → 全新集合，旧版本在线直到新版本通过冒烟测试
curl -X POST http://localhost:8000/debug/kb/rebuild
# 3. 冒烟通过 → 别名自动切到新集合，旧集合自动清理
```

维度配置错误时 embedding 会直接报错，即使侥幸入库，
冒烟测试的分数阈值也会拦下"向量空间错乱"的版本。

## 实现要点

- `rebuild_index` 抛错前必须清 staging（finally 语义），
  上次中断残留的 staging 开头即作废
- 同版本重复重建：先 drop 无别名指向的旧 target 再 rename
- 语料为空直接拒绝重建（防手滑清空线上）

## 验证

```bash
uv run pytest          # 49 passed
uv run ruff check .    # 0 issues
```

- `test_target_name_stable_and_model_sensitive`：同配置稳定 / 换模型必换集合
- `test_rebuild_switches_alias_and_cleans_old`：别名指向新版本、
  旧集合清理干净、切换后检索可用（真实 Milvus 全流程）
- `test_rebuild_failure_keeps_old_version`：模拟写入故障 → 旧版本可用、
  staging 清理干净（蓝绿承诺的核心用例）

## 遗留问题

- 重建是同步阻塞（当前 ~几十块语料秒级完成），语料大了改后台任务 + 进度查询
- 冒烟查询集固定在配置里，更完善的做法是从评估集抽样
- 切换瞬间（drop_alias ~ create_alias 之间）检索会短暂 404，
  语料量大后可改为 staging 直接挂别名 + rename 的双别名方案
