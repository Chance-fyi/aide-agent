"""跑分编排：把黄金集喂给检索层 / 完整 Agent 链路，收集逐条结果。

**为什么分两层跑**

- 检索层（`run_retrieval`）：只调 `store.search`，不碰 LLM。
  毫秒级、零成本、结果确定 → 可以进 CI，每次改切分/embedding/阈值都跑。
- 生成层（`run_full`）：跑完整 LangGraph 图，含 tool-calling。
  慢、花 LLM 费用、模型有随机性 → 发版前、调提示词后手动跑。

只跑生成层而不分层时，"答案不对"到底是"没检索到"还是"检索到了但没答好"
永远说不清。拆开之后，检索命中率高而答案通过率低 = 提示词/生成问题；
两者一起低 = 知识库/切分问题。这是分层最直接的价值。
"""

import asyncio
import logging
import uuid
from collections.abc import Callable

from langchain_core.messages import AIMessage, HumanMessage

from app.agents import handoff
from app.agents.runtime import get_graph
from app.core.config import get_settings
from app.eval.dataset import GoldenCase
from app.eval.metrics import CaseResult, score_answer, score_retrieval
from app.rag import store

logger = logging.getLogger(__name__)

# 评估专用 thread_id 前缀：与真实会话隔离，也便于事后按前缀清理残留
EVAL_THREAD_PREFIX = "eval-"

# 进度回调：(已完成条数, 总条数, 该条结果)
ProgressHook = Callable[[int, int, CaseResult], None]

def _notify(hook: ProgressHook | None, done: int, total: int, result: CaseResult) -> None:
    if hook is not None:
        hook(done, total, result)

# ============================================================
# 检索层
# ============================================================

def run_retrieval(
    cases: list[GoldenCase],
    top_k: int | None = None,
    on_progress: ProgressHook | None = None,
) -> list[CaseResult]:
    """只跑检索层：拿问题去向量库检索，看期望来源是否命中。

    检索口径与生产一致（同样的 top_k、同样的低分阈值），
    否则测出来的分数不代表线上表现。
    不校验检索的用例（转人工/超范围）也照样检索一次并记录命中，
    但不计入分母——顺便观察"本不该命中知识库的问题命中了什么"。
    """
    k = top_k or get_settings().retrieval_top_k
    results: list[CaseResult] = []

    for i, case in enumerate(cases, start=1):
        try:
            hits = store.search(case.question, top_k=k)
            sources = [h["source"] for h in hits]
        except Exception:
            # 单条检索失败不中断整轮评估：记为"未命中"，跑完再统一看
            logger.exception("检索失败 case=%s", case.id)
            sources = []
        r = CaseResult(case=case, retrieval=score_retrieval(case, sources))
        results.append(r)
        _notify(on_progress, i, len(cases), r)
    return results

# ============================================================
# 生成层（完整链路）
# ============================================================

def _extract_answer(result: dict) -> str | None:
    """从图执行结果里取纯文本回复（与 /chat 的口径保持一致）。

    空回复按"没答上来"处理（返回 None），而不是当成空字符串通过要点校验——
    空回复本来就是失败，不能因为 expect_points 为空而蒙混过关。
    """
    messages = result.get("messages") or []
    if not messages:
        return None
    last = messages[-1]
    text = last.text if isinstance(last, AIMessage) else str(getattr(last, "content", ""))
    return text.strip() or None

async def _cleanup(thread_ids: list[str], checkpointer) -> None:
    """清理评估产生的会话痕迹。

    为什么必须清：转接工具会写 conversation_status（真表！），
    Checkpointer 也会留快照。不清的话评估数据会混进坐席端队列
    （坐席看到一堆"评估用例"待接工单），且表会随评估次数无限增长。
    """
    if not thread_ids:
        return
    try:
        from app.agents.handoff import _get_conn

        with _get_conn() as conn:
            conn.execute(
                "DELETE FROM conversation_status WHERE thread_id = ANY(%s)", (thread_ids,)
            )
    except Exception:
        logger.warning("评估会话状态清理失败（不影响跑分结论）", exc_info=True)

    if checkpointer is None:
        return
    try:
        for tid in thread_ids:
            await checkpointer.adelete_thread(tid)
    except Exception:
        logger.warning("评估 Checkpointer 快照清理失败（不影响跑分结论）", exc_info=True)

async def run_full(
    cases: list[GoldenCase],
    on_progress: ProgressHook | None = None,
    cleanup: bool = True,
    delay: float = 0.0,
) -> list[CaseResult]:
    """跑完整链路：检索层 + 生成层，一条用例一个新会话。

    每条用例用独立 thread_id：
    - 避免上一条的 Checkpointer 记忆影响下一条（评估必须可复现）
    - 让"多轮指代"这类问题不被误判成通过
    代价是每条都从冷启动开始，与真实首次提问的场景一致。

    delay：用例间间隔（秒）。带限流的 LLM 厂商（如 10 次/分钟）下
    不加间隔会成片报 429，把限流噪声混进质量指标。
    """
    graph = await get_graph()
    results: list[CaseResult] = []
    thread_ids: list[str] = []

    try:
        for i, case in enumerate(cases, start=1):
            if delay and i > 1:
                await asyncio.sleep(delay)

            tid = f"{EVAL_THREAD_PREFIX}{case.id}-{uuid.uuid4().hex[:8]}"
            thread_ids.append(tid)

            # 检索层先跑、单独 try：LLM 挂了不该把"检索没命中"也算进去，
            # 否则两层分开统计的归因能力就失效了
            try:
                sources = [h["source"] for h in store.search(case.question)]
            except Exception:
                logger.exception("检索失败 case=%s", case.id)
                sources = []

            answer: str | None = None
            error: str | None = None
            try:
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content=case.question)]},
                    {"configurable": {"thread_id": tid}},
                )
                answer = _extract_answer(result)
            except Exception as exc:
                # 记录异常类型与首行信息：限流/超时这类环境问题
                # 一眼就能和"答得不对"区分开
                logger.exception("用例执行失败 case=%s", case.id)
                error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"

            status = handoff.get_status(tid)
            r = CaseResult(
                case=case,
                retrieval=score_retrieval(case, sources),
                answer=score_answer(case, answer, handoff.is_live_status(status)),
                error=error,
            )
            results.append(r)
            _notify(on_progress, i, len(cases), r)
    finally:
        if cleanup:
            # 从图实例取 checkpointer：runtime 不再提供独立访问器，
            # 图就是它唯一的使用者，多一层访问器只会多一条会失效的路径
            await _cleanup(thread_ids, getattr(graph, "checkpointer", None))

    return results

async def purge_residue() -> int:
    """清理历史评估残留（跑分被 Ctrl-C / 超时中断时会留下）。

    为何需要：清理写在 run_full 的 finally 里，但进程被 SIGTERM/SIGKILL 时
    finally 不一定执行。残留的 conversation_status 行会让坐席端队列
    出现一批“评估用例”待接工单，checkpoints 则会持续膨胀。

    这里只按 EVAL_THREAD_PREFIX 前缀删，不碰任何真实会话。
    返回清理的 thread 数。
    """
    from app.agents.handoff import _get_conn

    # 两个来源都可能残留：状态表行（转接过）与 Checkpointer 快照（执行过）
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT thread_id FROM conversation_status WHERE thread_id LIKE %s
            UNION
            SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE %s
            """,
            (f"{EVAL_THREAD_PREFIX}%", f"{EVAL_THREAD_PREFIX}%"),
        ).fetchall()
    thread_ids = [r[0] for r in rows]
    if not thread_ids:
        return 0

    graph = await get_graph()
    await _cleanup(thread_ids, getattr(graph, "checkpointer", None))
    return len(thread_ids)
