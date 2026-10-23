import { useState } from "react";
import { API_BASE, AUTH_ROLE, errorMessage, setAuth } from "../constants";

/**
 * 登录/注册视图（用户端与坐席端共用一个组件，按 role 区分行为）。
 *
 * - 登录：/auth/login 对所有角色开放，角色由账号本身决定
 * - 注册：仅用户端开放入口（后端 /auth/register 只发 user 角色）；
 *   坐席账号由管理员用 scripts/create_agent.py 创建，不开放自助注册
 */

// 标识：几何线条方框，中性且不依赖字体渲染。
// 刻意不用"舒/席"汉字圆角块——那种做法既是 AI 生成界面最典型的图标，
// 也会在中文字体缺失时变成一个方框
function Mark() {
  return (
    <svg className="login-mark" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <rect x="1.5" y="1.5" width="17" height="17" rx="2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M6 13.5V9.5M10 13.5V6.5M14 13.5v-2.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="square" />
    </svg>
  );
}

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
        throw new Error(errorMessage(data, "登录失败，请稍后重试"));
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
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <Mark />
        <h1 className="login-title">{isAgent ? "坐席工作台" : "登录舒心购"}</h1>
        <p className="login-sub">
          {isAgent ? "使用管理员分配的坐席账号" : "登录后会话与账号绑定，换设备也能接上"}
        </p>

        {/* 字段标签用等宽小写字母，读起来像表单编号而不是营销文案 */}
        <label className="field">
          <span>用户名</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="用户名"
            autoComplete="username"
            required
          />
        </label>
        <label className="field">
          <span>密码</span>
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
          <label className="field">
            <span>昵称（可选）</span>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="客服里展示的名字"
            />
          </label>
        )}

        {/* role="alert"：提交失败时读屏立即播报，不需要用户自己找错误在哪 */}
        {error && <div className="form-error" role="alert">{error}</div>}

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
