import { useState, useRef, useEffect } from "react";
import {
  API_BASE, AUTH_ROLE, ROLE, STORAGE_KEY, POLL_INTERVAL_MS, isLiveStatus,
  getAuth, clearAuth, authFetch, errorMessage,
} from "../constants";
import { MessageBubble } from "./MessageBubble";
import LoginView from "./LoginView";

/**
 * 用户端聊天视图。
 *
 * 与后端的协议（见 app/api/schemas.py）：
 * - POST /chat { message, thread_id? } → { message, thread_id, transferred }
 * - GET  /chat/{thread_id}/history → { thread_id, status, messages: [{role, content}] }
 *
 * 会话连续性设计：
 * - thread_id 存 localStorage，跨天/刷新后重进自动恢复历史消息与转接状态
 * - 转人工后输入框不再禁用：用户补充消息直达坐席工作台（后端落会话记录表）
 * - 前端轮询 history：用户能看到坐席的回复（模拟长轮询，SSE 属后续迭代）
 */

// 快捷提问：降低新用户输入门槛，也顺带引导到知识库覆盖率高的问题
const QUICK_QUESTIONS = ["怎么退货？", "退款多久到账？", "会员积分怎么用？"];

/**
 * 外层组件：只负责登录门卫。
 * 未登录渲染登录页；登录后挂载真正的聊天组件。
 * 必须拆两层：hooks 不能出现在条件 return 之后（Rules of Hooks），
 * 门卫放在无 hooks 的外层即可安全切换登录态
 */
function UserChat() {
  const [auth, setAuth] = useState(() => getAuth(AUTH_ROLE.USER));
  if (!auth) {
    return <LoginView role={AUTH_ROLE.USER} onLogin={setAuth} />;
  }
  return <UserChatInner auth={auth} onLogout={() => setAuth(null)} />;
}

function UserChatInner({ auth, onLogout }) {
  const [messages, setMessages] = useState([]);       // { role: 'user'|'agent'|'human', content }
  const [input, setInput] = useState("");
  const [threadId, setThreadId] = useState(null);     // 会话 ID（多轮记忆的关键）
  const [loading, setLoading] = useState(false);      // 等待后端回复中
  const [transferred, setTransferred] = useState(false); // 是否已转人工
  const [restored, setRestored] = useState(false);    // 历史恢复是否完成（避免恢复期间闪欢迎页）
  const listRef = useRef(null);                       // 自动滚到底部用
  const inputRef = useRef(null);                      // 发送完成后焦点回输入框
  const loadingRef = useRef(false);                   // 轮询跳过发送中的请求（闭包里的 loading 会过期）

  loadingRef.current = loading;

  const onAuthExpired = onLogout;   // Token 失效：清登录态回登录页

  // 挂载时恢复上次会话：单会话延续模式，有存档且后端有数据则续接
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) {
      setRestored(true);
      return;
    }
    (async () => {
      try {
        const resp = await authFetch(
          `${API_BASE}/chat/${saved}/history`, {}, AUTH_ROLE.USER, onAuthExpired,
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        setThreadId(saved);
        setMessages(data.messages ?? []);
        // 恢复转接状态：排队中/处理中都算"已转接"
        setTransferred(isLiveStatus(data.status));
      } catch {
        // 会话过期/后端清库：静默清掉存档，按新用户处理
        localStorage.removeItem(STORAGE_KEY);
      } finally {
        setRestored(true);
      }
    })();
  }, []);

  // 轮询拉取会话记录：主要为了收到人工坐席的回复。
  // 发送中跳过本轮，避免轮询结果覆盖本地乐观消息
  useEffect(() => {
    if (!threadId) return;
    const timer = setInterval(async () => {
      if (loadingRef.current) return;
      try {
        const resp = await authFetch(
          `${API_BASE}/chat/${threadId}/history`, {}, AUTH_ROLE.USER, onAuthExpired,
        );
        if (!resp.ok) return;
        const data = await resp.json();
        setMessages(data.messages ?? []);
        setTransferred(isLiveStatus(data.status));
      } catch {
        // 轮询失败静默：下一轮自动重试，不打扰用户
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [threadId]);

  // 消息变化时滚动到底部
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, loading]);

  async function send(textArg) {
    const text = (textArg ?? input).trim();
    if (!text || loading) return;   // 转人工后仍可发消息，只拦"发送中"

    // 乐观更新：立刻显示用户消息
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInput("");
    setLoading(true);

    try {
      const resp = await authFetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          thread_id: threadId,  // 首轮为 null，后端会生成
        }),
      }, AUTH_ROLE.USER, onAuthExpired);
      if (!resp.ok) {
        // 后端 503 等会带 detail 文案，优先展示给用户而非笼统的"网络异常"
        const data = await resp.json().catch(() => null);
        throw new Error(errorMessage(data, `网络异常（HTTP ${resp.status}），请稍后重试`));
      }
      const data = await resp.json();

      setThreadId(data.thread_id);          // 记住会话 ID，后续多轮携带
      localStorage.setItem(STORAGE_KEY, data.thread_id);  // 跨天恢复的凭据
      setTransferred(data.transferred);     // 转接后切换状态 UI（输入不再禁用）
      // 坐席已接入后后端返回空 message（不再刷机器人应答），前端不渲染空气泡
      if (data.message) {
        setMessages((prev) => [...prev, { role: ROLE.AGENT, content: data.message }]);
      }
    } catch (err) {
      // 网络失败/后端 503：友好提示而非白屏；detail 里是后端给的可操作文案
      setMessages((prev) => [
        ...prev,
        { role: ROLE.AGENT, content: err.message || "网络异常，请稍后重试。" },
      ]);
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }

  return (
    <div className="chat-panel">
      <header className="header">
        <div className="header-title">
          <span className="avatar bot">智</span>
          <div>
            <h1>舒心购智能客服</h1>
            <span className="subtitle">
              {transferred
                ? "人工客服会话中"
                : threadId
                  ? `会话 ${threadId.slice(0, 8)}`
                  : "在线 · 有什么可以帮您？"}
            </span>
          </div>
        </div>
        {/* 当前登录用户与退出登录：双端都可见，明确"我是谁" */}
        <span className="login-user">{auth.display_name || auth.username}</span>
        <button
          className="ghost-btn"
          onClick={() => {
            clearAuth(AUTH_ROLE.USER);
            localStorage.removeItem(STORAGE_KEY);
            onLogout();
          }}
        >
          退出
        </button>
      </header>

      {/* 转接状态横幅：比只在标题里换文案更醒目，用户不会错过 */}
      {transferred && (
        <div className="transfer-banner">
          <span className="dot pulse" />
          已转接人工，您可以继续补充问题，客服会尽快回复
        </div>
      )}

      <div className="msg-list" ref={listRef}>
        {restored && messages.length === 0 && (
          <div className="welcome">
            {/* SVG 图标替代 emoji：跨平台渲染一致，且可用 token 控制颜色 */}
            <div className="welcome-icon">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
              </svg>
            </div>
            <p className="welcome-title">您好，我是舒心购智能助手</p>
            <p className="welcome-sub">可以点击下面的问题快速开始，也可以直接输入</p>
            <div className="chips">
              {QUICK_QUESTIONS.map((q) => (
                <button key={q} className="chip" onClick={() => send(q)}>{q}</button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div
            key={i}
            /* 用户端视角：用户靠右，智能客服/人工客服都靠左 */
            className={`msg-row ${m.role === ROLE.USER ? "user" : "assistant"}`}
          >
            <MessageBubble
              message={m}
              bubbleClass={m.role === ROLE.USER ? "user" : m.role === ROLE.HUMAN ? "human" : "agent-view"}
              withAvatar={m.role !== ROLE.USER}
              showTag={m.role !== ROLE.USER}
            />
          </div>
        ))}
        {loading && (
          <div className="msg-row assistant">
            <span className="avatar bot bot2">智</span>
            <div className="msg assistant typing"><i /><i /><i /></div>
          </div>
        )}
      </div>

      <footer className="input-bar">
        <input
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder={transferred ? "补充问题，人工客服会看到" : "输入消息，回车发送"}
          disabled={loading}
          autoFocus
        />
        <button
          className="send-btn"
          onClick={() => send()}
          disabled={loading || !input.trim()}
          aria-label="发送消息"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <line x1="22" y1="2" x2="11" y2="13" />
            <polygon points="22 2 15 22 11 13 2 9 22 2" />
          </svg>
        </button>
      </footer>
    </div>
  );
}

export default UserChat;
