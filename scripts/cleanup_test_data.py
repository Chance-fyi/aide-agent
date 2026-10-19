"""清理测试产生的会话与消息（用户/坐席端联调后的数据复位）。

按规则删除，避免误删真实数据：
- 会话：thread_id 匹配测试前缀（tc-/unit-/ctx-/e2e-/test-），或
  归属用户名匹配测试账号模式（tc_test / bindtest* / user_hex8 位随机名）
- 测试账号：app_user 里用户名匹配上述模式的 user 角色（agent 账号不动）

用法：
    uv run python scripts/cleanup_test_data.py            # 预览将删除的内容
    uv run python scripts/cleanup_test_data.py --apply    # 实际删除
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
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        # 测试会话 ID 集合
        rows = conn.execute(
            "SELECT thread_id FROM conversation_status WHERE "
            + " OR ".join("thread_id LIKE %s" for _ in TEST_THREAD_PREFIXES),
            tuple(p + "%" for p in TEST_THREAD_PREFIXES),
        ).fetchall()
        thread_ids = [r[0] for r in rows]

        # 测试账号
        users = conn.execute(
            "SELECT username FROM app_user WHERE role = 'user' AND ("
            + " OR ".join("username LIKE %s" for _ in TEST_USER_LIKE)
            + ")",
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
            # 归属引用的测试会话已在上面清掉；这里解引用再删账号
            conn.execute(
                "UPDATE conversation_status SET user_id = NULL WHERE user_id = %s", (u,)
            )
            conn.execute("DELETE FROM app_user WHERE username = %s", (u,))
        print("已清理。")


if __name__ == "__main__":
    main()
