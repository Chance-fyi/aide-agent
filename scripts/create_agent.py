"""创建坐席账号的运维脚本（坐席账号不开放自助注册）。

用法：
    uv run python scripts/create_agent.py <username> <password> [display_name]

示例：
    uv run python scripts/create_agent.py agent1 agent123 坐席小王
"""

import sys

from app.core.security import ROLE_AGENT
from app.core.users import AuthError, create_user, ensure_table


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    username, password = sys.argv[1], sys.argv[2]
    display_name = sys.argv[3] if len(sys.argv) > 3 else None
    ensure_table()
    try:
        user = create_user(username, password, ROLE_AGENT, display_name)
        print(f"坐席账号已创建: {user['username']}（昵称: {user['display_name']}）")
    except AuthError as e:
        print(f"创建失败: {e.message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
