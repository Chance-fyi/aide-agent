# Step 2 — podman 中间件（Postgres + Milvus）

## 目标

用 `podman compose` 一键拉起中间件：Postgres（会话记忆 + 业务数据）、
etcd + Milvus（知识库向量检索）。

## 最终架构

```
aide-postgres       pgvector/pgvector:pg17        -> localhost:5432
aide-milvus-etcd    quay.io/coreos/etcd:v3.5.18   （Milvus 元数据）
aide-milvus         milvusdb/milvus:v2.5.4        -> localhost:19530 / 9091
                    COMMON_STORAGETYPE=local      （本地文件存储，无需对象存储）
```

## 踩坑记录（排障过程，真实且有价值）

### 坑 1：内嵌 etcd 模式在 rootless podman 下段错误

最初尝试单容器方案（`ETCD_USE_EMBED=true`），Milvus 在 `InitEtcdServer`
阶段稳定 SIGSEGV。内嵌 etcd 的 mmap/文件锁操作与 rootless 沙箱不兼容，
调整 `advertise-client-urls`、清理数据卷均无效。
**结论：rootless podman 下放弃内嵌模式，改用独立 etcd 容器。**

### 坑 2：MinIO 社区镜像已全面下架

标准三容器编排还需要对象存储，但踩到了连环坑：

| 尝试 | 结果 |
|---|---|
| `docker.io/minio/minio:RELEASE.*` | 官方已从 Docker Hub 移除社区镜像 |
| `quay.io/minio/aistor/minio` | 能跑，但 `mc ls` 报 "No license is installed"，社区版需付费 license |
| `docker.io/bitnami/minio` | Bitnami 已停产该镜像 |

**结论：直接去掉 MinIO。** Milvus 2.4+ 官方支持本地存储模式
（`COMMON_STORAGETYPE=local`），开发环境完全不需要对象存储。

### 坑 3：新版 MinIO 镜像的环境变量改名

排查过程中还发现 aistor 镜像使用 `MINIO_ROOT_USER/PASSWORD`，
旧的 `MINIO_ACCESS_KEY/SECRET_KEY` 静默失效（凭据错误只会表现为
Milvus 侧 "Access Denied"，具有一定的迷惑性）。

## 关键配置说明

- Postgres 用 `pgvector/pgvector:pg17`：官方预装 pgvector 扩展，
  一个容器承担"会话记忆（LangGraph Checkpointer）+ 业务数据"
- Milvus healthcheck 用 `curl http://localhost:9091/healthz`，
  `start_period: 40s` 给足启动时间
- 数据全部落在命名卷（`aide_pgdata` / `aide_milvus` / aide_etcd），
  容器重建不丢数据

## 验证

```bash
podman compose up -d
podman ps                                    # 三个容器均 Up/healthy
curl http://localhost:9091/healthz           # Milvus 返回 OK
uv run python -c "
from pymilvus import MilvusClient
print(MilvusClient(uri='http://localhost:19530').list_collections())  # []
"
podman exec aide-postgres pg_isready -U aide -d aide_agent
```
