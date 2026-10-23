"""知识库重建任务：后台执行 + 状态持久化。

**为什么是后台任务而不是同步接口**

实测全量重建（46 文件 / 199 块）耗时 **24 秒**。放在 HTTP 请求里会：
- 撞上反向代理/浏览器超时（常见 30s，边界极其危险）
- 前端只能转圈，无法告知"进行到哪了"
- 用户可能重复点击，触发并发重建

所以：`start()` 立刻返回任务 id，实际重建跑在后台，前端轮询状态。

**为什么状态存 Postgres 而不是进程内存**

多 worker 部署下，发起重建的请求和查询状态的请求可能落到**不同进程**。
进程内存里的状态会让另一个 worker 永远看到"空闲"，进而允许并发重建。
存 DB 后任何 worker 都能读到真实状态。

**并发互斥用部分唯一索引，不用应用层判断**

`WHERE status = 'running'` 的唯一索引把互斥交给数据库。
应用层"先查再插"在并发下有竞争窗口，两个请求可能都查到空闲然后都插入。

**为什么还要处理"卡住的 running"**

进程在重建中被 kill（部署、OOM）时，那条 running 记录会永远留着，
之后所有重建都被拒。所以超过 `STALE_MINUTES` 的 running 视为僵尸，
启动新任务时顺手标记为 failed。
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import psycopg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# running 超过这个时长视为僵尸（进程中途被杀留下的记录）。
# 取 10 分钟：远大于正常重建的 24 秒，留足慢机器/大语料的余量
STALE_MINUTES = 10

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

def _conn() -> psycopg.Connection:
    return psycopg.connect(get_settings().postgres_dsn, autocommit=True)

class RebuildBusyError(Exception):
    """已有重建在跑。路由层翻译成 409。"""

class RebuildJob:
    """重建任务的启动、执行与查询。

    实例在进程内只用于持有 asyncio 任务引用（防被 GC 回收），
    真实状态一律从 DB 读。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    # ---------- 查询 ----------

    def latest(self) -> dict | None:
        """最近一次重建记录；从未重建过返回 None。"""
        with _conn() as conn:
            row = conn.execute(
                "SELECT id, status, triggered_by, started_at, finished_at, chunks, error "
                "FROM kb_rebuild_run ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._to_dict(row) if row else None

    def snapshot(self) -> dict:
        """一次查询拿到「任务 + 是否在跑」，保证两个字段互相一致。

        为什么必须一次查：分成 `latest()` + `is_running()` 两次查时，
        后台任务可能在两次查询之间完成，响应就会出现
        `job.status='running'` 但 `running=false` 的自相矛盾结果。
        前端拿到这种响应会停止轮询却又显示"重建中"，是最难解释的那类 bug。
        """
        job = self.latest()
        running = bool(
            job and job["status"] == STATUS_RUNNING and not self._is_stale(job)
        )
        return {"job": job, "running": running}

    def is_running(self) -> bool:
        """是否有未超时的 running 任务（供前端决定是否允许点重建）。"""
        return self.snapshot()["running"]

    @staticmethod
    def _is_stale(job: dict) -> bool:
        started = datetime.fromisoformat(job["started_at"])
        return datetime.now(UTC) - started > timedelta(minutes=STALE_MINUTES)

    @staticmethod
    def _to_dict(row) -> dict:
        return {
            "id": row[0],
            "status": row[1],
            "triggered_by": row[2],
            "started_at": row[3].isoformat(),
            "finished_at": row[4].isoformat() if row[4] else None,
            "chunks": row[5],
            "error": row[6],
        }

    # ---------- 启动 ----------

    async def start(self, triggered_by: str) -> dict:
        """创建一个重建任务并交给后台执行；返回任务记录。

        抛 RebuildBusyError 表示已有任务在跑。
        """
        self._clear_stale()
        run_id = self._insert_running(triggered_by)
        # to_thread：重建是同步阻塞的（torch embedding + Milvus 写入），
        # 直接在事件循环里跑会卡住所有 WebSocket 推送
        self._task = asyncio.create_task(asyncio.to_thread(self._run, run_id))
        logger.info("知识库重建已排队 id=%s by=%s", run_id, triggered_by)
        job = self.latest()
        assert job is not None
        return job

    def _clear_stale(self) -> None:
        """把僵尸 running 标成 failed，避免永久阻塞后续重建。"""
        with _conn() as conn:
            rows = conn.execute(
                "UPDATE kb_rebuild_run SET status = %s, finished_at = now(), "
                "error = %s WHERE status = %s AND started_at < now() - (%s * interval '1 minute') "
                "RETURNING id",
                (
                    STATUS_FAILED,
                    f"超过 {STALE_MINUTES} 分钟未完成，判定为中断（进程可能已重启）",
                    STATUS_RUNNING,
                    STALE_MINUTES,
                ),
            ).fetchall()
        if rows:
            logger.warning("清理僵尸重建任务 %d 个: %s", len(rows), [r[0] for r in rows])

    def _insert_running(self, triggered_by: str) -> int:
        """插入 running 记录；部分唯一索引冲突说明已有任务在跑。"""
        try:
            with _conn() as conn:
                row = conn.execute(
                    "INSERT INTO kb_rebuild_run (status, triggered_by) VALUES (%s, %s) "
                    "RETURNING id",
                    (STATUS_RUNNING, triggered_by),
                ).fetchone()
            return row[0]
        except psycopg.errors.UniqueViolation:
            # 唯一索引 idx_kb_rebuild_running 命中：已有一个 running
            raise RebuildBusyError() from None

    # ---------- 执行 ----------

    def _run(self, run_id: int) -> None:
        """真正执行重建（在工作线程里跑）。失败只更新任务状态，不向外抛。"""
        from app.rag import kb_store, store
        from app.rag.splitter import load_and_split_all

        started = time.monotonic()
        try:
            kb_store.ensure_table()
            chunks = store.rebuild_index("data/knowledge_base")
            elapsed = round(time.monotonic() - started, 1)
            self._finish(run_id, STATUS_SUCCEEDED, chunks=chunks)
            logger.info("知识库重建完成 id=%s chunks=%d 耗时=%ss", run_id, chunks, elapsed)
        except Exception as exc:
            elapsed = round(time.monotonic() - started, 1)
            # 对外只给可操作的原因，堆栈进日志（与项目其他地方的异常口径一致）
            reason = _friendly_reason(exc)
            self._finish(run_id, STATUS_FAILED, error=reason)
            logger.exception("知识库重建失败 id=%s 耗时=%ss", run_id, elapsed)
            return
        finally:
            # 重建会改变切块结果，调试接口的缓存必须失效
            try:
                from app.api.debug_routes import clear_caches

                clear_caches()
            except Exception:
                logger.debug("清理调试缓存失败（debug 路由可能未注册）", exc_info=True)

        # 重建成功后语料与切块数可能变化，这里做一次静默校验：
        # 切块为 0 说明语料目录空了，虽然 rebuild_index 会自己抛错，
        # 但用 load_and_split_all 再确认一次文件可见性，便于排障定位
        try:
            files = load_and_split_all("data/knowledge_base")
            logger.debug("重建后语料文件数=%d", len({c.source for c in files}))
        except Exception:
            logger.debug("重建后校验失败", exc_info=True)

    def _finish(
        self, run_id: int, status: str, chunks: int | None = None, error: str | None = None
    ) -> None:
        try:
            with _conn() as conn:
                conn.execute(
                    "UPDATE kb_rebuild_run SET status = %s, finished_at = now(), "
                    "chunks = %s, error = %s WHERE id = %s",
                    (status, chunks, error, run_id),
                )
        except Exception:
            # 状态更新失败只能记日志：此时重建本身已经完成/失败，
            # 记录不准比让进程崩掉好；僵尸清理逻辑会在 10 分钟后兜底
            logger.exception("更新重建任务状态失败 id=%s", run_id)

def _friendly_reason(exc: Exception) -> str:
    """把重建异常翻译成用户能看懂的一句话（细节留在日志里）。"""
    text = str(exc)
    if "语料为空" in text:
        return "知识库语料目录为空，请先放入 Markdown 文档"
    if "Milvus" in text or "milvus" in text:
        return "向量库不可用，请检查 Milvus 是否启动"
    if "connection" in text.lower() or "connect" in text.lower():
        return "数据库连接失败，请稍后重试"
    if "smoke" in text.lower() or "冒烟" in text:
        return "重建后冒烟测试未通过（新数据检索不到核心业务问题），已回滚到旧版本"
    # 兜底：给异常类型名，够排障用又不泄露堆栈
    return f"重建失败（{type(exc).__name__}），详情见服务端日志"

# 进程级单例：只用于持有后台任务引用
job = RebuildJob()
