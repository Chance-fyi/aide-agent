import { useState, useRef, useEffect, useCallback, useLayoutEffect } from "react";
import {
  AUTH_ROLE, ROLE, isLiveStatus, getAuth, clearAuth,
} from "../constants";
import { createSocket } from "../ws";
import { mergeMessage } from "../messages";
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
 * 会话连续性：thread_id 由服务端按登录账号解析（"一个用户一条聊天记录"），
 * 前端不持有它——换设备/清缓存/退出重登都会自动接回同一条流。
 */

// 快捷提问：降低新用户输入门槛，也顺带引导到知识库覆盖率高的问题。
// 展示成编号清单而不是药丸按钮——药丸+图标圆底是"AI 生成"的典型版式
const QUICK_QUESTIONS = ["怎么退货？", "退款多久到账？", "会员积分怎么用？"];

/**
 * 外层组件：只负责登录门卫。
 * 未登录渲染登录页；登录后挂载真正的聊天组件。
 * 必须拆两层：hooks 不能出现在条件 return 之后（Rules of Hooks）
 */
function UserChat() {
  const [auth, setAuth] = useState(() => getAuth(AUTH_ROLE.USER));
  if (!auth) {
    return <LoginView role={AUTH_ROLE.USER} onLogin={setAuth} />;
  }
  return <UserChatInner auth={auth} onLogout={() => setAuth(null)} />;
}

function UserChatInner({ auth, onLogout }) {
  const [messages, setMessages] = useState([]);       // { role, content, ts, sender_name }
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);      // 等待机器人回复中（仅自助问答阶段）
  const [transferred, setTransferred] = useState(false); // 是否已转人工
  const [restored, setRestored] = useState(false);    // 历史是否已就绪（否则显示骨架）
  const [connected, setConnected] = useState(false);  // WS 是否已连上
  const listRef = useRef(null);
  const inputRef = useRef(null);
  const socketRef = useRef(null);
  // 流式回复进行中：为 true 时渲染占位气泡，delta 追加到它上面
  const [streaming, setStreaming] = useState(false);

  // 连接状态点的颜色语义：断线=灰、人工中=琥珀、自助在线=绿。
  // 用类而不是内联 style：颜色必须从 token 取，内联写死会绕过设计系统
  const dotTone = !connected ? "dot--muted" : transferred ? "dot--busy" : "dot--ok";

  // 并入一条消息：本地乐观消息与服务端推送都走这里。
  // 用 mergeMessage 按服务端 id 去重——重连补发/双通道到达时不会变成两条
  const appendMessage = useCallback((msg) => {
    setMessages((prev) =>
      mergeMessage(prev, { ...msg, ts: msg.ts ?? new Date().toISOString() }),
    );
  }, []);

  // 发送中的保护定时器：清 pending 不能只依赖服务端事件。
  // hub 的 outbox 满时会丢弃事件（见 app/core/hub.py），一条丢失的确认
  // 就能让输入框永久禁用——这类"卡死"必须有一条超时兼容路径。
  const pendingTimer = useRef(null);
  const clearPending = useCallback(() => {
    if (pendingTimer.current) clearTimeout(pendingTimer.current);
    pendingTimer.current = null;
    setLoading(false);
  }, []);
  const startPending = useCallback(() => {
    if (pendingTimer.current) clearTimeout(pendingTimer.current);
    setLoading(true);
    // 60s 远大于正常响应（LLM 超时重试后也会走 error 事件），
    // 只当兵底：宁可偶尔提前解锁，也不要永久锁死
    pendingTimer.current = setTimeout(() => setLoading(false), 60_000);
  }, []);
  // 卸载时清定时器，避免在已卸载组件上 setState
  useEffect(() => () => {
    if (pendingTimer.current) clearTimeout(pendingTimer.current);
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
            setStreaming(false);
            clearPending();
            break;
          case "message":
            appendMessage(ev.message);
            // 对方回复到了 → 收起"输入中"。
            // 用户自己的消息不会推回来（本地已乐观插入）
            if (ev.message?.role !== ROLE.USER) clearPending();
            break;
          case "reply_start":
            // 流式开始：先插入一条占位气泡（无 id，末尾显示光标），
            // 后续 delta 往它上面追加。没有这个起始标记就无法区分
            // "新回复"与"上一条的补充"
            setStreaming(true);
            setMessages((prev) => [...prev, { role: ROLE.AGENT, content: "", streaming: true }]);
            break;
          case "reply_delta":
            // 追加到**最后一条**消息上。用函数式更新读最新 state，
            // 避免闭包里拿到过期的 messages
            setMessages((prev) => {
              if (prev.length === 0) return prev;
              const last = prev[prev.length - 1];
              if (!last.streaming) return prev;   // 已在 reply_end 收尾，迟到的 delta 丢弃
              const next = prev.slice();
              next[next.length - 1] = { ...last, content: last.content + ev.delta };
              return next;
            });
            break;
          case "reply_end":
            // 收尾：把占位气泡替换成服务端权威消息（含 id/ts）。
            // 拿到 id 之后，重连或重载时同一 id 会被 mergeMessage 合并，
            // 不会出现"刷新前一条、刷新后还是一条但内容变了"这类错乱
            setStreaming(false);
            clearPending();
            setMessages((prev) => {
              if (prev.length === 0) return prev;
              const last = prev[prev.length - 1];
              if (!last.streaming) return prev;
              const next = prev.slice();
              next[next.length - 1] = { ...ev.message, streaming: false };
              return next;
            });
            break;
          case "sent":
            // 服务端确认"消息已接收"。这是清 pending 的**主信号**：
            // 已转人工时根本不会有机器人回复，靠回复事件清状态会让输入框
            // 永久禁用（曾导致"转人工后只能发一条消息"）
            clearPending();
            setTransferred(isLiveStatus(ev.status));
            break;
          case "status":
            setTransferred(isLiveStatus(ev.status));
            break;
          case "error":
            // 后端给的是可操作文案（校验失败/LLM 故障），直接展示。
            // 关键：先把流式占位气泡收尾——LLM 在**流式途中**失败时，
            // 已推出的半截内容还挂在 streaming 状态上，不处理就会留下
            // 一个永远闪烁的光标（用户以为还在生成，其实早就断了）
            setStreaming(false);
            setMessages((prev) => {
              if (prev.length === 0) return prev;
              const last = prev[prev.length - 1];
              if (!last.streaming) return prev;
              const next = prev.slice();
              // 有半截内容就保留（用户能看到生成到哪），只是停止闪烁；
              // 完全空白的占位直接丢掉，避免留一个空气泡
              if (last.content) {
                next[next.length - 1] = { ...last, streaming: false, aborted: true };
              } else {
                next.pop();
              }
              return next;
            });
            appendMessage({ role: ROLE.AGENT, content: ev.message });
            clearPending();
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
    // 只依赖 token：登录态不变就不该重建连接
  }, [auth.token, appendMessage, clearPending, onLogout]);

  // 消息列表始终钉在最新消息。滚动容器 CSS 已禁用平滑滚动与浏览器锚定
  useLayoutEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  });

  function send(textArg) {
    const text = (textArg ?? input).trim();
    if (!text) return;
    // 只有"自助问答等待机器人回复"期间需要防重复提交；
    // 已转人工是人和人在对话，不能因为上一条还没收到确认就拦下一条
    if (loading && !transferred) return;

    const socket = socketRef.current;
    // 连接断开时明确告知而不是静默失败：用户以为发出去了、其实没发，
    // 是聊天类产品最伤体验的错觉之一
    if (!socket || !socket.send({ type: "message", content: text })) {
      appendMessage({ role: ROLE.AGENT, content: "连接已断开，正在重连，请稍后重新发送。" });
      return;
    }

    // 乐观更新：服务端不会把用户消息推回来，所以不会重复。
    // 重连后由 ready 快照替换为服务端时间
    appendMessage({ role: ROLE.USER, content: text });
    setInput("");
    setStreaming(false);
    startPending();
    inputRef.current?.focus({ preventScroll: true });
  }

  return (
    <section className="chat">
      <div className="chat-inner">
        <header className="chat-head">
          <div>
            <h1 className="chat-title">智能客服</h1>
            {/* 状态写进标题区，省掉一条独立横幅；点 + 文字双重编码 */}
            <p className="chat-sub">
              <span
                className={`dot ${dotTone} ${connected && !transferred ? "dot-live" : ""}`}
              />
              {!connected
                ? "连接中…"
                : transferred
                  ? "人工坐席服务中"
                  : "在线 · 自助问答"}
            </p>
          </div>
          <div className="chat-head-actions">
            <span className="topbar-user">{auth.display_name || auth.username}</span>
            <button
              className="btn"
              onClick={() => {
                clearAuth(AUTH_ROLE.USER);   // 聊天记录在服务端按账号存，退出只清登录态
                onLogout();
              }}
            >
              退出
            </button>
          </div>
        </header>

        {transferred && (
          <div className="banner">
            <span className="dot" />
            已转接人工，可继续补充问题，坐席会尽快回复
          </div>
        )}

        {/* role="log" + aria-live：新消息推入时读屏会自动播报，
            但不抢焦点（聊天场景不能因为来消息就把用户光标挪走） */}
        <div
          className="log"
          ref={listRef}
          role="log"
          aria-live="polite"
          aria-label="对话记录"
          aria-busy={!restored}
        >
          {/* 恢复历史期间显示骨架：比闪一下欢迎页稳定，也比转圈更有结构感 */}
          {!restored && (
            <div className="skeleton" aria-hidden="true">
              <div className="skeleton-row" />
              <div className="skeleton-row" />
              <div className="skeleton-row" />
            </div>
          )}

          {restored && messages.length === 0 && (
            <div className="empty">
              <p className="empty-title">开始对话</p>
              <p className="empty-note">
                直接输入问题，或从下面挑一个常见问题。转人工后消息会直达坐席。
              </p>
              <ul className="prompts">
                {QUICK_QUESTIONS.map((q, i) => (
                  <li key={q}>
                    <button className="prompt" onClick={() => send(q)}>
                      <span className="prompt-key">{String(i + 1).padStart(2, "0")}</span>
                      {q}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {messages.map((m, i) => (
            <MessageBubble
              key={`${m.ts ?? ""}|${m.role}|${i}`}
              message={m}
              // 自己的消息靠右，标签写"你"——比"客户"更自然
              side={m.role === ROLE.USER ? "end" : "start"}
              label={m.role === ROLE.USER ? "你" : undefined}
              // 流式中的气泡在末尾显示光标（纯装饰，读屏靠 aria-live 播报）
              streaming={m.streaming}
              aborted={m.aborted}
            />
          ))}

          {/* 等待回复：一个闪烁光标，比三个跳动的点安静。
              已进入流式后不再显示——此时真正的气泡已经在渲染了 */}
          {loading && !transferred && !streaming && (
            <div className="line line--start line--agent" aria-hidden="true">
              <div className="line-meta">
                <span className="role role--agent">智能客服</span>
              </div>
              <div className="bubble"><span className="caret" /></div>
            </div>
          )}
        </div>

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={transferred ? "补充问题，坐席会看到" : "输入消息，回车发送"}
            /* 仅自助问答等待机器人时锁输入；已转人工不能锁，
               否则上一条的确认晚到一会儿就会阻止下一条 */
            disabled={loading && !transferred}
            autoFocus
            aria-label="消息输入"
          />
          <button
            className="send"
            type="submit"
            disabled={(loading && !transferred) || !input.trim()}
            aria-label="发送消息"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M4 12h15M13 6l6 6-6 6" />
            </svg>
          </button>
        </form>
      </div>
    </section>
  );
}

export default UserChat;
