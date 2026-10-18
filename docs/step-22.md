# step-22：登录功能——双端账号体系（用户端 + 坐席端）

## 目标

用户端与坐席端各自由独立账号登录，区分不同用户与坐席：
- 用户登录后会话与账号绑定，别人看不到别人的会话
- 坐席接口全部要求坐席账号登录，坐席身份取自登录态而非请求体

## 实现要点

### 后端

- **`app/core/security.py`**（新增）：
  - 密码哈希用标准库 `pbkdf2_hmac`（PBKDF2-SHA256，60 万轮），恒定时间比较防时序侧信道
  - Token 为 HMAC-SHA256 自签名的自包含串（payload 明文 + 签名，仿 JWT 形态），
    无需服务端存会话，与"单进程自足"架构一致；`AUTH_SECRET` 走 `.env`
- **`app/core/users.py`**（新增）：`app_user` 表（username 主键 / password_hash / role / display_name），
  注册、登录（不存在与密码错误同一文案，不泄露账号是否存在）
- **`app/api/auth.py`**（新增）：`POST /auth/register`（仅发 user 角色）、
  `POST /auth/login`（用户与坐席共用入口，角色由账号决定）、`GET /auth/me`
- **`app/api/deps.py`**（新增）：三级鉴权依赖 `get_optional_user` / `get_current_user` / `require_agent`，
  用 `Annotated` 类型别名声明（兼容 ruff B008）
- **`app/api/routes.py`**：坐席接口全部加 `require_agent`；`agent_name` 改取登录态
  `display_name`，不再信任请求体
- **`app/api/chat.py`**：`/chat` 与 history 改可选认证——登录后把会话绑定到账号
  （`conversation_status.user_name` 新列，幂等迁移），已绑定的会话他人（非坐席）
  访问 403；未登录仍可匿名自助问答（兼容存量行为）
- **`scripts/create_agent.py`**（新增）：坐席账号不开放自助注册，管理员用此脚本创建

### 前端

- **`web/src/constants.js`**：用户端/坐席端登录态分键存储（`AUTH_KEY`）；
  `authFetch` 统一带 `Authorization` 头，401 自动清登录态回登录页
- **`web/src/components/LoginView.jsx`**（新增）：双端共用登录卡；用户端有注册入口，
  坐席端只有登录
- **`UserChat.jsx` / `AgentConsole.jsx`**：各拆"门卫外层 + 内层组件"两层
  （满足 Rules of Hooks：hooks 不能出现在条件 return 之后）；
  顶栏显示当前登录人 + 退出按钮；坐席操作不再传 `agent_name`

## 角色模型

`app_user.role`：`user`（用户端）/ `agent`（坐席端）。
一个登录入口，角色由账号本身决定——杜绝"角色选错"，也避免暴露坐席注册面。

## 验证方法

- `uv run ruff check app tests scripts` 通过；`uv run pytest` 62 passed
  （新增：未登录访问坐席接口 401、注册/登录/me 全链路、重复注册 400、密码错误 401）
- `cd web && npm run build` 通过
- 手工链路：`uv run python scripts/create_agent.py agent1 agent123 坐席小王`
  → 坐席端登录 → 接入/回复；用户端注册 → 对话 → 转人工

## 遗留问题

- Token 有效期固定 7 天，无刷新机制（过期重登即可，当前规模可接受）
- 未登录匿名会话仍可自助问答（产品上有意保留）；如需强制登录，
  把 `/chat` 的 `get_optional_user` 换成 `get_current_user` 一行即可
- 登录态存 localStorage（XSS 风险面与 sessionStorage 相当），后续可上 HttpOnly Cookie
