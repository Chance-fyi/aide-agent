"""步骤 6 验证脚本：多轮对话记忆 + 工具调用。

用法：
    uv run python scripts/chat_repl.py

依赖 .env 中配置了可用的 LLM。输入 q 退出。
"""

import sys
import uuid
from pathlib import Path

# 让脚本直接运行时也能 import app 包（把项目根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.postgres import PostgresSaver

from app.agents.graph import build_graph_with_memory
from app.core.config import get_settings


def main() -> None:
    s = get_settings()

    # Checkpointer 用上下文管理器：内部管理 Postgres 连接池
    with PostgresSaver.from_conn_string(s.postgres_dsn) as cp:
        cp.setup()  # 幂等建表
        graph = build_graph_with_memory(cp)

        # thread_id 即会话 ID：同一个 thread_id 共享记忆，换一个就是新会话
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}
        print(f"会话 ID: {thread_id}（输入 q 退出）\n")

        while True:
            try:
                user_input = input("你: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input or user_input.lower() == "q":
                break

            result = graph.invoke(
                {"messages": [("user", user_input)]},
                config,
            )
            print(f"小舒: {result['messages'][-1].content}\n")


if __name__ == "__main__":
    main()
