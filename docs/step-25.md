# step-25：用户端与坐席端拆成两个独立页面

## 目标

登录页与工作台原先在同一个 SPA 里用 tab 内部状态切换，
改为两个真实 URL 页面：`/` 用户端、`/agent` 坐席端。

## 实现要点

- **`web/src/App.jsx`**：
  - tab 内部状态切换改为路径路由，原生 History API 实现
    （项目只有两个路径，不值得引入 react-router）
  - 路径 → 页面用 `ROUTES` 映射表（未来加页面只改这张表），
    未知路径回退用户端
  - 切换按钮走 `pushState`；`popstate` 事件同步浏览器前进/后退
    （注意 `pushState` 不触发 popstate，切换时需手动 setState）
- 深链支持：直接访问 `/agent`（书签/输入 URL）也能打开坐席端，
  Vite dev server 自带 SPA history fallback

## 验证方法

- `cd web && npm run build` 通过
- 手工验证：`/` 与 `/agent` 深链直达、刷新停原页、前进/后退正确切换

## 遗留问题

- 未来页面增多（>3 个）建议引入 react-router 统一管理嵌套路由与守卫

## step-25 补充：修复错误提示显示 [object Object]

- 根因：FastAPI 422 参数校验错误的 `detail` 是对象数组（`[{loc, msg, type}]`），
  前端当字符串拼接后显示为 `[object Object]`
- 修复：`constants.js` 新增共享的 `errorMessage(data, fallback)`——
  字符串直接用；数组取第一条校验错误，拼上字段名（如 `password: String should
  have at least 6 characters`）；其余用兜底文案
- `LoginView` / `UserChat` / `AgentConsole` 四处错误展示统一改走该函数

## step-25.3：坐席端用用户名替代 thread_id 展示

- `handoff.list_live_threads` 响应新增 `user_name` 字段（会话归属账号，
  step-24 已落库）；匿名会话为 null
- 坐席端队列列表与详情标题改为显示用户名，匿名会话回退 `游客 {thread_id 前 8 位}`；
  `openThread` 把队列项整包存入 selected，详情标题才能拿到 user_name
