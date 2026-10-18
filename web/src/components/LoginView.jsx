import { useState } from "react";
import { API_BASE, AUTH_ROLE, setAuth } from "../constants";

/**
 * 登录/注册视图（用户端与坐席端共用一个组件，按 role 区分行为）。
 *
 * - 登录：/auth/login 对所有角色开放，角色由账号本身决定
 * - 注册：仅用户端开放入口（后端 /auth/register 只发 user 角色）；
 *   坐席账号由管理员用 scripts/create_agent.py 创建，不开放自助注册
 */

export default function LoginView({ role, onLogin }) {
  const isAgent = role === AUTH_ROLE.AGENT;
  const [mode, setMode] = useState("login");        // login | register（仅用户端有 register）
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const url = mode === "login" ? "/auth/login" : "/auth/register";
      const body =
        mode === "login"
          ? { username, password }
          : { username, password, display_name: displayName || undefined };
      const resp = await fetch(`${API_BASE}${url}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) {
        throw new Error(data?.detail || "登录失败，请稍后重试");
      }
      setAuth(role, data);       // {token, username, role, display_name} 整包入库
      onLogin(data);
    } catch (err) {
      setError(err.message || "网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="login-logo" aria-hidden="true">
          {isAgent ? "席" : "舒"}
        </div>
        <h1>{isAgent ? "坐席工作台登录" : "登录舒心购"}</h1>
        <p className="login-sub">
          {isAgent ? "请使用管理员分配的坐席账号" : "登录后您的会话将与账号绑定"}
        </p>

        <label>
          用户名
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="用户名"
            autoComplete="username"
            required
          />
        </label>
        <label>
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="密码"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
          />
        </label>
        {mode === "register" && (
          <label>
            昵称（可选）
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="客服里展示的名字"
            />
          </label>
        )}

        {error && <div className="login-error">{error}</div>}

        <button className="login-btn" type="submit" disabled={busy}>
          {busy ? "请稍候…" : mode === "login" ? "登录" : "注册并登录"}
        </button>

        {!isAgent && (
          <button
            type="button"
            className="login-switch"
            onClick={() => {
              setMode(mode === "login" ? "register" : "login");
              setError("");
            }}
          >
            {mode === "login" ? "没有账号？注册一个" : "已有账号？去登录"}
          </button>
        )}
      </form>
    </div>
  );
}
