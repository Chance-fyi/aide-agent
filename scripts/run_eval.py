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
    p.add_argument("--sweep", default="",
                   help="多档 top_k 对比（如 1,3,5），仅检索层；用来看排序质量的真实水平")
    p.add_argument("--delay", type=float, default=0.0,
                   help="用例间隔秒数（带限流的 LLM 厂商下建议设 6~7，避免成片 429）")
    p.add_argument("--json", default="", help="结果落盘路径（JSON）")
    p.add_argument("--verbose", action="store_true", help="打印每条用例的答案全文")
    p.add_argument("--purge", action="store_true",
                   help="只清理中断跑分留下的 eval-* 残留（状态表 + Checkpointer 快照），不跑分")
    p.add_argument("--audit", action="store_true",
                   help="审计「知识库外」用例是否已腐化（语料里其实已有相关内容）")
    return p.parse_args()

def _print_case(done: int, total: int, r: CaseResult, verbose: bool) -> None:
    """逐条输出：进度 + 判定 + 失败原因（原因要能直接指向该改什么）。"""
    mark = {True: "✓", False: "✗", None: "-"}[r.passed]
    parts = [f"[{done:>2}/{total}] {mark} {r.case.id:<14}"]

    if r.retrieval.checked:
        if r.retrieval.ok:
            parts.append(f"检索:{','.join(r.retrieval.hit) or '可接受来源'}")
            # 排序质量：命中但首条不对时标出来，这是 rerank 要优化的目标
            parts.append("首条✓" if r.retrieval.top1_ok else f"MRR{r.retrieval.mrr:.2f}")
        elif r.retrieval.missed:
            parts.append(f"检索缺:{','.join(r.retrieval.missed)}")
        else:
            parts.append("可接受来源全未命中")

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

def _run_sweep(cases, ks: list[int]) -> None:
    """多档 top_k 对比表。

    为何需要：语料小的时候 recall 很快饱和（top_k=3 就已经 100%），
    于是“命中率”这个指标对 rerank / query 改写完全不敏感。
    扫一遍 top_k 才能看出真实排序水平：
    top_k=1 的命中率就是“检索器首推多准”，这才是优化要推动的数。
    """
    rows = []
    for k in ks:
        results = run_retrieval(cases, top_k=k)
        rows.append((k, summarize(results)))

    print(f"\n{'top_k':>6} {'命中率':>10} {'首条命中':>10} {'MRR':>8}")
    print("-" * 38)
    for k, s in rows:
        hit = "n/a" if s.retrieval_rate is None else f"{s.retrieval_rate * 100:.1f}%"
        top1 = "n/a" if s.top1_rate is None else f"{s.top1_rate * 100:.1f}%"
        mrr = "n/a" if s.mrr is None else f"{s.mrr:.3f}"
        print(f"{k:>6} {hit:>10} {top1:>10} {mrr:>8}")
    print("-" * 38)
    # 首条命中率不随 top_k 变化：它就是 top_k=1 的命中率，
    # 若两者相等，说明排序前几名对 rerank 已无优化空间
    print("提示：首条命中率与 top_k 无关（它就是 top_k=1 的命中率）。")
    print("      若 top_k=1 命中率明显低于 top_k=3，说明召回得回来但排不准——")
    print("      这正是 rerank / query 改写能发挥的空间。")

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
            # 脚本跑完就退出，不 flush 会把缓冲区里的 span 全丢掉
            from app.core import tracing

            tracing.shutdown()

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

    if args.audit:
        from app.eval.runner import audit_cases

        suspects = audit_cases(cases)
        total = sum(1 for c in cases if c.category == "知识库外")
        if not suspects:
            print(f"审计通过：「知识库外」用例 {total} 条，均未发现语料中已有相关内容")
            sys.exit(0)
        print(f"发现 {len(suspects)}/{total} 条「知识库外」用例可疑：")
        for s in suspects:
            print(f"  {s['id']:<10} {s['score']:.3f}  {s['source']} / {s['h1']}")
            print(f"            问题: {s['question']}")
        print("\n这些用例的问题在语料里能检到像样的内容，很可能已腐化：")
        print("若新语料确实回答了该问题，应把它改成正常业务问答并给出 expect_sources_any。")
        sys.exit(1)

    mode = "完整链路（检索 + 生成 + 转接）" if args.full else "仅检索层"
    print(f"黄金集: {args.dataset} | 用例 {len(cases)} 条 | 模式: {mode}")

    if args.sweep:
        if args.full:
            print("--sweep 仅支持检索层（不要与 --full 同时用）", file=sys.stderr)
            sys.exit(2)
        try:
            ks = sorted({int(x) for x in args.sweep.split(",") if x.strip()})
        except ValueError:
            print(f"--sweep 格式错误: {args.sweep}（示例: 1,3,5）", file=sys.stderr)
            sys.exit(2)
        if not ks or any(k < 1 for k in ks):
            print("--sweep 必须是正整数列表", file=sys.stderr)
            sys.exit(2)
        _run_sweep(cases, ks)
        sys.exit(0)

    print()

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
                                    "missed": list(r.retrieval.missed),
                                    "any_ok": r.retrieval.any_ok,
                                    "top1_ok": r.retrieval.top1_ok,
                                    "mrr": round(r.retrieval.mrr, 4)},
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
