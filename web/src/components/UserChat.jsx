import { useState, useRef, useEffect } from "react";

/**
 * 用户端聊天视图。
 *
 * 与后端的协议（见 app/api/schemas.py）：
 * - POST /chat { message, thread_id? } → { message, thread_id, transferred }
 * - thread_id 首轮由后端生成，前端存下后原样携带即可延续多轮
 * - transferred=true 表示已转人工，输入框禁用并显示等待提示
 */

// .env 里配置后端地址（Vite 约定：VITE_ 前缀才会暴露给前端）
const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

// 快捷提问：降低新用户输入门槛，也顺带引导到知识库覆盖率高的问题
const QUICK_QUESTIONS = ["怎么退货？", "退款多久到账？", "会员积分怎么用？"];

function UserChat() {
  const [messages, setMessages] = useState([]);       // { role: 'user'|'assistant', content }
  const [input, setInput] = useState("");
  const [threadId, setThreadId] = useState(null);     // 会话 ID（多轮记忆的关键）
  const [loading, setLoading] = useState(false);      // 等待后端回复中
  const [transferred, setTransferred] = useState(false); // 是否已转人工
  const listRef = useRef(null);                       // 自动滚到底部用
  const inputRef = useRef(null);                      // 新会话后焦点回输入框

  // 消息变化时滚动到底部
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, loading]);

  async function send(textArg) {
    const text = (textArg ?? input).trim();
    if (!text || loading || transferred) return;

    // 乐观更新：立刻显示用户消息
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInput("");
    setLoading(true);

    try {
      const resp = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          thread_id: threadId,  // 首轮为 null，后端会生成
        }),
      });
      if (!resp.ok) {
        // 后端 503 等会带 detail 文案，优先展示给用户而非笼统的"网络异常"
        const data = await resp.json().catch(() => null);
        throw new Error(data?.detail || `HTTP ${resp.status}`);
      }
      const data = await resp.json();

      setThreadId(data.thread_id);          // 记住会话 ID，后续多轮携带
      setTransferred(data.transferred);     // 转接后禁用输入
      setMessages((prev) => [...prev, { role: "assistant", content: data.message }]);
    } catch (err) {
      // 网络失败/后端 503：友好提示而非白屏；detail 里是后端给的可操作文案
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: err.message || "网络异常，请稍后重试。" },
      ]);
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }

  function reset() {
    // "新会话"：清空本地状态即可，thread_id 置空后端会开新会话
    setMessages([]);
    setThreadId(null);
    setTransferred(false);
    inputRef.current?.focus();
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
                ? "已为您转接人工客服"
                : threadId
                  ? `会话 ${threadId.slice(0, 8)}`
                  : "在线 · 有什么可以帮您？"}
            </span>
          </div>
        </div>
        <button className="ghost-btn" onClick={reset}>新会话</button>
      </header>

      {/* 转接状态横幅：比只在标题里换文案更醒目，用户不会错过 */}
      {transferred && (
        <div className="transfer-banner">
          <span className="dot pulse" />
          已转接人工，客服正在处理，请耐心等待…
        </div>
      )}

      <div className="msg-list" ref={listRef}>
        {messages.length === 0 && (
          <div className="welcome">
            <div className="welcome-icon">💬</div>
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
          <div key={i} className={`msg-row ${m.role}`}>
            {m.role === "assistant" && <span className="avatar bot">智</span>}
            <div className={`msg ${m.role}`}>{m.content}</div>
          </div>
        ))}
        {loading && (
          <div className="msg-row assistant">
            <span className="avatar bot">智</span>
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
          placeholder={transferred ? "人工客服处理中，暂无需回复" : "输入消息，回车发送"}
          disabled={transferred || loading}
          autoFocus
        />
        <button onClick={() => send()} disabled={transferred || loading || !input.trim()}>
          发送
        </button>
      </footer>
    </div>
  );
}

export default UserChat;
