import { useState, useRef, useEffect } from "react";

/**
 * 极简客服聊天前端。
 *
 * 与后端的协议（见 app/api/schemas.py）：
 * - POST /chat { message, thread_id? } → { message, thread_id, transferred }
 * - thread_id 首轮由后端生成，前端存下后原样携带即可延续多轮
 * - transferred=true 表示已转人工，输入框禁用并显示等待提示
 */

// .env 里配置后端地址（Vite 约定：VITE_ 前缀才会暴露给前端）
const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

function App() {
  const [messages, setMessages] = useState([]);       // { role: 'user'|'assistant', content }
  const [input, setInput] = useState("");
  const [threadId, setThreadId] = useState(null);     // 会话 ID（多轮记忆的关键）
  const [loading, setLoading] = useState(false);      // 等待后端回复中
  const [transferred, setTransferred] = useState(false); // 是否已转人工
  const listRef = useRef(null);                       // 自动滚到底部用

  // 消息变化时滚动到底部
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [messages, loading]);

  async function send() {
    const text = input.trim();
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
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();

      setThreadId(data.thread_id);          // 记住会话 ID，后续多轮携带
      setTransferred(data.transferred);     // 转接后禁用输入
      setMessages((prev) => [...prev, { role: "assistant", content: data.message }]);
    } catch (err) {
      // 网络失败/后端 503：友好提示而非白屏
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "网络异常，请稍后重试。" },
      ]);
    } finally {
      setLoading(false);
    }
  }

  function reset() {
    // "新会话"：清空本地状态即可，thread_id 置空后端会开新会话
    setMessages([]);
    setThreadId(null);
    setTransferred(false);
  }

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1>舒心购客服</h1>
          <span className="subtitle">
            {transferred ? "已转接人工，请等待客服接入" : threadId ? `会话 ${threadId.slice(0, 8)}` : "有什么可以帮您？"}
          </span>
        </div>
        <button className="reset-btn" onClick={reset}>新会话</button>
      </header>

      <div className="msg-list" ref={listRef}>
        {messages.length === 0 && (
          <div className="empty-hint">试试问：怎么退货？ / 退款多久到账？ / 会员积分怎么用？</div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>{m.content}</div>
        ))}
        {loading && <div className="msg assistant typing">正在输入…</div>}
      </div>

      <footer className="input-bar">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder={transferred ? "已转接人工" : "输入消息，回车发送"}
          disabled={transferred || loading}
        />
        <button onClick={send} disabled={transferred || loading || !input.trim()}>
          发送
        </button>
      </footer>
    </div>
  );
}

export default App;
