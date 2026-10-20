"""黄金集跑分脚本。

用法：
    # 只跑检索层（不花 LLM 费用，可进 CI）
    uv run python scripts/run_eval.py

    # 跑完整链路（检索 + 生成 + 转人工判断）
    uv run python scripts/run_eval.py --full

    # 抽样：只跑部分用例，调 prompt 时快速迭代
    uv run python scripts/run_eval.py --full --category 退货
    uv run python scripts/run_eval.py --full --ids returns-1,refund-1 --verbose

    # 落盘结果，用于两次跑分对比
    uv run python scripts/run_eval.py --full --json /tmp/eval-1.json

退出码：有未通过用例时为 1（便于 CI / 发版门禁拦截）。
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# 直接 `uv run scripts/run_eval.py` 时 sys.path 是 scripts/，补项目根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval.dataset import DEFAULT_DATASET, DEFAULT_KB_DIR, DatasetError, load_cases
from app.eval.metrics import CaseResult, format_summary, summarize
from app.eval.runner import run_full, run_retrieval


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="黄金问答集跑分")
    p.add_argument("--full", action="store_true",
                   help="跑完整链路（含 LLM 生成）；默认只跑检索层")
    p.add_argument("--dataset", default=DEFAULT_DATASET, help="黄金集路径")
    p.add_argument("--kb-dir", default=DEFAULT_KB_DIR, help="知识库目录（校验来源文件用）")
    p.add_argument("--ids", default="", help="只跑指定用例（逗号分隔）")
    p.add_argument("--category", default="", help="只跑指定分类")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    p.add_argument("--top-k", type=int, default=0, help="检索 top_k（默认取配置）")
    p.add_argument("--delay", type=float, default=0.0,
                   help="用例间隔秒数（带限流的 LLM 厂商下建议设 6~7，避免成片 429）")
    p.add_argument("--json", default="", help="结果落盘路径（JSON）")
    p.add_argument("--verbose", action="store_true", help="打印每条用例的答案全文")
    p.add_argument("--purge", action="store_true",
                   help="只清理中断跑分留下的 eval-* 残留（状态表 + Checkpointer 快照），不跑分")
    return p.parse_args()

def _print_case(done: int, total: int, r: CaseResult, verbose: bool) -> None:
    """逐条输出：进度 + 判定 + 失败原因（原因要能直接指向该改什么）。"""
    mark = {True: "✓", False: "✗", None: "-"}[r.passed]
    parts = [f"[{done:>2}/{total}] {mark} {r.case.id:<14}"]

    if r.retrieval.checked:
        if r.retrieval.ok:
            parts.append(f"检索:{','.join(r.retrieval.hit)}")
        else:
            parts.append(f"检索缺:{','.join(r.retrieval.missed)}")

    if r.answer is not None:
        a = r.answer
        if a.answer is None:
            parts.append(f"无回复（{r.error}）" if r.error else "无回复")
        else:
            if a.missing:
                parts.append(f"缺点:{','.join(a.missing)}")
            if a.any_ok is False:
                parts.append(f"未命中任一:{'|'.join(r.case.expect_any)}")
            if a.transfer_ok is False:
                parts.append("误转人工" if a.transferred else "该转未转")
    print("  ".join(parts))

    if verbose and r.answer is not None and r.answer.answer:
        for line in r.answer.answer.splitlines():
            print(f"        │ {line}")

async def _run(args, cases):
    """跑分主流程：所有异步资源在同一事件循环里创建与释放。

    为什么把 close() 放在这里：runtime 的连接池绑定在创建它的事件循环上，
    分成两次 asyncio.run 会在不同循环里关池，触发跨循环错误。
    """
    from app.agents import runtime

    try:
        if args.full:
            return await run_full(
                cases,
                on_progress=lambda *a: _print_case(*a, args.verbose),
                delay=args.delay,
            )
        return run_retrieval(
            cases,
            top_k=args.top_k or None,
            on_progress=lambda *a: _print_case(*a, args.verbose),
        )
    finally:
        if args.full:
            await runtime.close()

def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    args = _parse_args()

    if args.purge:
        from app.agents import runtime
        from app.eval.runner import purge_residue

        async def _purge() -> int:
            try:
                return await purge_residue()
            finally:
                await runtime.close()

        n = asyncio.run(_purge())
        print(f"已清理 {n} 个评估残留会话")
        sys.exit(0)

    try:
        cases = load_cases(args.dataset, args.kb_dir)
    except DatasetError as e:
        # 黄金集腐化是"必须先修尺子"的问题，直接失败而不是跑出一堆假分数
        print(f"黄金集校验失败: {e}", file=sys.stderr)
        sys.exit(2)

    # 过滤顺序：ids > category > limit（ids 最具体，优先级最高）
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        cases = [c for c in cases if c.id in wanted]
        unknown = wanted - {c.id for c in cases}
        if unknown:
            print(f"未知用例 id: {sorted(unknown)}", file=sys.stderr)
            sys.exit(2)
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("没有匹配的用例", file=sys.stderr)
        sys.exit(2)

    mode = "完整链路（检索 + 生成 + 转接）" if args.full else "仅检索层"
    print(f"黄金集: {args.dataset} | 用例 {len(cases)} 条 | 模式: {mode}\n")

    results = asyncio.run(_run(args, cases))
    summary = summarize(results)
    print(format_summary(summary))

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"summary": summary.as_dict(),
                 "cases": [
                     {"id": r.case.id,
                      "passed": r.passed,
                      "error": r.error,
                      "retrieval": {"ok": r.retrieval.ok,
                                    "hit": list(r.retrieval.hit),
                                    "missed": list(r.retrieval.missed)},
                      "answer": None if r.answer is None else {
                          "ok": r.answer.ok,
                          "missing": list(r.answer.missing),
                          "any_ok": r.answer.any_ok,
                          "transferred": r.answer.transferred,
                          "transfer_ok": r.answer.transfer_ok,
                      }}
                     for r in results
                 ]},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"结果已写入 {args.json}")

    sys.exit(0 if not summary.failed_ids else 1)

if __name__ == "__main__":
    main()
