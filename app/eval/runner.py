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
        probe = case.retrieval_probe
        sources: list[str] = []
        if probe is not None:
            try:
                sources = [h["source"] for h in store.search(probe, top_k=k)]
            except Exception:
                # 单条检索失败不中断整轮评估：记为"未命中"，跑完再统一看
                logger.exception("检索失败 case=%s", case.id)
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
            sources: list[str] = []
            probe = case.retrieval_probe
            if probe is not None:
                try:
                    sources = [h["source"] for h in store.search(probe)]
                except Exception:
                    logger.exception("检索失败 case=%s", case.id)

            answer: str | None = None
            error: str | None = None
            config = {"configurable": {"thread_id": tid}}
            try:
                # 多轮用例：先按序重放历史发言，让 Checkpointer 攒出上下文，
                # 最后一句才是被评估的问题
                for prior in case.history:
                    await graph.ainvoke(
                        {"messages": [HumanMessage(content=prior)]}, config
                    )
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content=case.question)]}, config
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

# ============================================================
# 黄金集审计
# ============================================================

# 「知识库外」用例的审计阈值：检索最高分超过它，说明语料里可能已有相关内容，
# 用例多半已腐化（应该改成正常业务问答）。
#
# 0.65 是按当前语料实测校准的：
#   真腐化（"可以分期付款吗" 命中 payment-installment.md）= 0.743
#   假阳性（"支持货到付款吗" 只命中支付方式列表）= 0.594
#   真·知识库外（0.477 / 0.530）
# 阈值取两者之间：过高漏报腐化，过低会把"话题相近但没答案"误报。
# 语料大幅变动后应重新校准（跑一次 --audit 看分布）。
AUDIT_SCORE_THRESHOLD = 0.65

# 声明了"知识库没有相关内容"的分类：语料一扩充就可能静默失效
AUDIT_CATEGORY = "知识库外"

def audit_cases(cases: list[GoldenCase]) -> list[dict]:
    """审计「知识库外」用例：语料里是不是已经有内容了。

    为何需要：扩语料时最容易发生的一种腐化——
    原本挂在「知识库外」的用例（期望"不支持/没有"），
    因为新加了一份相关文档而失效。它的表现不是报错，而是
    "模型答对了却被判错"，极难发现（本步真踩到：分期付款）。

    做法：拿问题去检索，看最高分。分数高 = 语料里有像样的内容 = 应人工复核。
    只提示不自动修改：是否真的腐化要人看语料才能定。
    """
    suspects: list[dict] = []
    for case in cases:
        if case.category != AUDIT_CATEGORY:
            continue
        try:
            hits = store.search(case.question, top_k=1)
        except Exception:
            logger.exception("审计检索失败 case=%s", case.id)
            continue
        if not hits:
            continue
        top = hits[0]
        if top["score"] >= AUDIT_SCORE_THRESHOLD:
            suspects.append(
                {
                    "id": case.id,
                    "question": case.question,
                    "score": round(top["score"], 3),
                    "source": top["source"],
                    "h1": top["h1"],
                }
            )
    return suspects
