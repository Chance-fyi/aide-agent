import { useState, useRef, useEffect, useCallback, useLayoutEffect } from "react";
import {
  AUTH_ROLE, ROLE, isLiveStatus, getAuth, clearAuth,
} from "../constants";
import { createSocket } from "../ws";
import { MessageBubble } from "./MessageBubble";
import LoginView from "./LoginView";

/**
 * 用户端聊天视图（WebSocket 实时通道）。
 *
 * 协议（见 app/api/ws.py）：
 *   发 → {"type":"message","content":"..."} / {"type":"ping"}
 *   收 → ready（历史 + 转接状态全量快照）/ message（客服或坐席回复）
 *        / status（转接状态变化）/ error（友好错误）/ pong
 *
 * 为什么把轮询换成 WS：轮询下坐席回复的可见延迟最多 4 秒（且空转请求不断），
 * 双端"收到消息"的体验都取决于客户端碰巧什么时候拉了一次。推送让延迟
 * 回到网络往返量级，同时 ready 快照保证断线重连不丢消息。
 *
 * 会话连续性：thread_id 由服务端按登录账号解析（"一个用户一条聊天记录"），
 * 前端不持有它——换设备/清缓存/退出重登都会自动接回同一条流。
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
  const [messages, setMessages] = useState([]);       // { role: 'user'|'agent'|'human', content, ts }
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);      // 等待回复中（"输入中"提示）
  const [transferred, setTransferred] = useState(false); // 是否已转人工
  const [restored, setRestored] = useState(false);    // 历史是否已就绪（避免恢复期间闪欢迎页）
  const [connected, setConnected] = useState(false);  // WS 是否已连上（断开时提示并阻止发送）
  const listRef = useRef(null);                       // 自动滚到底部用
  const inputRef = useRef(null);                      // 发送完成后焦点回输入框
  const socketRef = useRef(null);

  // 追加一条消息：本地乐观消息与服务端推送都走这里，保证格式与时间戳口径一致
  const appendMessage = useCallback((msg) => {
    setMessages((prev) => [
      ...prev,
      { ...msg, ts: msg.ts ?? new Date().toISOString() },
    ]);
  }, []);

  // 建立实时连接（登录态变化时重建；卸载时关闭）
  useEffect(() => {
    const socket = createSocket({
      path: "/ws/chat",
      token: auth.token,
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
      onAuthError: () => {
        // Token 失效：清登录态回登录页（ws.js 不会再重连，避免无效重试）
        clearAuth(AUTH_ROLE.USER);
        onLogout();
      },
      onEvent: (ev) => {
        switch (ev.type) {
          case "ready":
            // 全量快照：首次连接与每次重连都靠它对齐（历史 + 转接状态）。
            // 用它替换整个列表而不是追加，重连后才不会出现重复消息
            setMessages(ev.messages ?? []);
            setTransferred(isLiveStatus(ev.status));
            setRestored(true);
            setLoading(false);
            break;
          case "message":
            appendMessage(ev.message);
            // 客服/坐席的回复到了 → 收起"输入中"。
            // 用户自己的消息不会推回来（本地已乐观插入），所以这里必然是对方的
            if (ev.message?.role !== ROLE.USER) setLoading(false);
            break;
          case "status":
            setTransferred(isLiveStatus(ev.status));
            break;
          case "error":
            // 后端给的是可操作文案（校验失败/LLM 故障），直接展示，不吞成"网络异常"
            appendMessage({ role: ROLE.AGENT, content: ev.message });
            setLoading(false);
            break;
          default:
            break;   // pong 等心跳应答无需处理
        }
      },
    });
    socketRef.current = socket;
    return () => {
      socket.close();
      socketRef.current = null;
    };
    // 只依赖 token：登录态不变就不该重建连接（否则每次渲染都重连）
  }, [auth.token, appendMessage, onLogout]);

  // 消息列表始终钉在最新消息，滚动容器 CSS 禁用平滑滚动与浏览器锚定。
  useLayoutEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  });

  function send(textArg) {
    const text = (textArg ?? input).trim();
    if (!text || loading) return;   // 转人工后仍可发消息，只拦"发送中"

    const socket = socketRef.current;
    // 连接断开时明确告知而不是静默失败：用户以为发出去了、其实没发，
    // 是聊天类产品最伤体验的错觉之一
    if (!socket || !socket.send({ type: "message", content: text })) {
      appendMessage({ role: ROLE.AGENT, content: "连接已断开，正在重连，请稍后重新发送。" });
      return;
    }

    // 乐观更新：立刻显示用户消息（ts 用客户端时间；服务端不会把它推回来，
    // 所以不会重复。重连后由 ready 快照替换为服务端时间）
    appendMessage({ role: ROLE.USER, content: text });
    setInput("");
    setLoading(true);
    // preventScroll：聚焦时禁止浏览器把焦点元素滚进视野（可能引发布局跳动）
    inputRef.current?.focus({ preventScroll: true });
  }

  return (
    <div className="chat-panel">
      <header className="header">
        <div className="header-title">
          <span className="avatar bot">智</span>
          <div>
            <h1>舒心购智能客服</h1>
            <span className="subtitle">
              {!connected
                ? "连接中…"
                : transferred
                  ? "人工客服会话中"
                  : "在线 · 有什么可以帮您？"}
            </span>
          </div>
        </div>
        {/* 当前登录用户与退出登录：双端都可见，明确"我是谁" */}
        <span className="login-user">{auth.display_name || auth.username}</span>
        <button
          className="ghost-btn"
          onClick={() => {
            clearAuth(AUTH_ROLE.USER);   // 聊天记录在服务端按账号存，退出只清登录态
            onLogout();
          }}
        >
          退出
        </button>
      </header>

      <div className="message-area">
        <div className={`transfer-banner ${transferred ? "visible" : ""}`} aria-hidden={!transferred}>
          <span className="dot pulse" />
          已转接人工，您可以继续补充问题，客服会尽快回复
        </div>
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
            className={`msg-row ${m.role === ROLE.USER ? "user" : "assistant"}`}
          >
            <MessageBubble
              message={m}
              bubbleClass={m.role === ROLE.USER ? "user" : m.role === ROLE.HUMAN ? "human" : "agent-view"}
              withAvatar={m.role !== ROLE.USER}
              showTag={m.role !== ROLE.USER}
              showTime={true}   // 用户自己的消息也显示发送时间
            />
          </div>
        ))}
        {loading && !transferred && (
          <div className="msg-row assistant">
            <span className="avatar bot bot2">智</span>
            <div className="msg assistant typing"><i /><i /><i /></div>
          </div>
        )}
        </div>
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
