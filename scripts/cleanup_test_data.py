"""清理测试产生的会话与消息（用户/坐席端联调后的数据复位）。

三种模式：
    uv run python scripts/cleanup_test_data.py             # 预览按规则将删除的内容
    uv run python scripts/cleanup_test_data.py --apply     # 按规则删除
    uv run python scripts/cleanup_test_data.py --all --apply  # 全量清空会话数据（保留正式账号）

按规则模式（默认）：
- 会话：thread_id 匹配测试前缀（tc-/unit-/ctx-/e2e-/test-）
- 测试账号：用户名匹配测试模式（含 test_agent_*——pytest 每次运行
  都会新建随机坐席账号，不清理会持续堆积）
全量模式（--all）：
- 清空 conversation_status / chat_message 全部数据、删除全部测试账号，
  仅保留"正式账号"（不在测试模式里的账号，如 user1/agent1）
开发期联调产生的会话多是接口生成的 UUID，不带测试前缀，
只能用全量模式清理；生产环境严禁使用 --all。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from app.core.config import get_settings

# 测试 thread_id 前缀（历次测试脚本用的约定前缀）
TEST_THREAD_PREFIXES = ("tc-", "unit-", "ctx-", "e2e-", "test-")
# 测试账号用户名模式（SQL LIKE）
TEST_USER_LIKE = ["tc\\_test", "bindtest%", "test\\_agent_%", "user\\_%"]


def main() -> None:
    apply = "--apply" in sys.argv
    wipe_all = "--all" in sys.argv
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        if wipe_all:
            # 全量模式：不枚举，直接按表清（含孤儿消息——只有消息没有状态行的会话）
            n_msg = conn.execute("SELECT count(*) FROM chat_message").fetchone()[0]
            n_cs = conn.execute("SELECT count(*) FROM conversation_status").fetchone()[0]
            # 测试账号含 agent（test_agent_* 是 pytest 动态建的），保留正式账号
            users = conn.execute(
                "SELECT username FROM app_user WHERE "
                + " OR ".join("username LIKE %s" for _ in TEST_USER_LIKE),
                tuple(TEST_USER_LIKE),
            ).fetchall()
            usernames = [u[0] for u in users]
            print(f"[全量] 将清空 {n_msg} 条消息、{n_cs} 个会话状态，删除 {len(usernames)} 个测试账号")
            for u in usernames[:10]:
                print(f"  账号 {u}")
            if not apply:
                print("预览模式，未删除。加 --apply 执行。")
                return
            conn.execute("DELETE FROM chat_message")
            conn.execute("DELETE FROM conversation_status")
            for u in usernames:
                conn.execute("DELETE FROM app_user WHERE username = %s", (u,))
            print("已全量清理。")
            return

        # ---- 按规则模式 ----
        rows = conn.execute(
            "SELECT thread_id FROM conversation_status WHERE "
            + " OR ".join("thread_id LIKE %s" for _ in TEST_THREAD_PREFIXES),
            tuple(p + "%" for p in TEST_THREAD_PREFIXES),
        ).fetchall()
        thread_ids = [r[0] for r in rows]

        # 测试账号（含 agent 的 test_agent_*）
        users = conn.execute(
            "SELECT username FROM app_user WHERE "
            + " OR ".join("username LIKE %s" for _ in TEST_USER_LIKE),
            tuple(TEST_USER_LIKE),
        ).fetchall()
        usernames = [u[0] for u in users]

        print(f"将删除 {len(thread_ids)} 个测试会话、{len(usernames)} 个测试账号")
        for tid in thread_ids[:10]:
            print(f"  会话 {tid[:16]}…")
        for u in usernames[:10]:
            print(f"  账号 {u}")

        if not apply:
            print("预览模式，未删除。加 --apply 执行。")
            return

        for tid in thread_ids:
            conn.execute("DELETE FROM chat_message WHERE thread_id = %s", (tid,))
            conn.execute("DELETE FROM conversation_status WHERE thread_id = %s", (tid,))
        for u in usernames:
            conn.execute(
                "UPDATE conversation_status SET user_id = NULL WHERE user_id = %s", (u,)
            )
            conn.execute("DELETE FROM app_user WHERE username = %s", (u,))
        print("已清理。")


if __name__ == "__main__":
    main()
