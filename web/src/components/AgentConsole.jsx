import { useState, useEffect, useRef, useCallback } from "react";
import {
  API_BASE, AUTH_ROLE, POLL_INTERVAL_MS, isLiveStatus,
  getAuth, clearAuth, authFetch,
} from "../constants";
import { MessageBubble, agentViewBubbleClass } from "./MessageBubble";
import LoginView from "./LoginView";

/**
 * 坐席端控制台（模拟坐席工作台）。
 *
 * 与后端的协议：
 * - GET  /agent/threads                      → { threads: [{thread_id, status, reason, agent_name, updated_at}] }
 * - GET  /chat/{thread_id}/history           → { thread_id, status, messages: [{role, content}] }
 * - POST /agent/threads/{thread_id}/accept   → 接入（queued → in_service）
 * - POST /agent/threads/{thread_id}/close    → 结束（→ closed）
 *
 * 队列用轮询刷新（MVP 够用，SSE/WebSocket 属于后续流式迭代范围）。
 */

const STATUS_LABEL = {
  queued: "排队中",
  in_service: "处理中",
  active: "自助中",
  closed: "已结束",
};

function timeAgo(iso) {
  // 相对时间展示：坐席更关心"等了多久"而不是绝对时刻
  const diff = Date.now() - new Date(iso).getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  return `${Math.floor(min / 60)} 小时前`;
}

/**
 * 外层组件：坐席登录门卫（与 UserChat 同样拆两层以满足 Rules of Hooks）。
 * 坐席账号不开放注册，由管理员用 scripts/create_agent.py 创建
 */
function AgentConsole() {
  const [auth, setAuth] = useState(() => getAuth(AUTH_ROLE.AGENT));
  if (!auth) {
    return <LoginView role={AUTH_ROLE.AGENT} onLogin={setAuth} />;
  }
  return <AgentConsoleInner auth={auth} onLogout={() => setAuth(null)} />;
}

function AgentConsoleInner({ auth, onLogout }) {
  const [threads, setThreads] = useState([]);
  const [error, setError] = useState("");
  // 当前查看的会话：{thread_id, status}。存状态对象而非裸 ID，
  // 因为操作按钮按状态显隐（queued→接入 / in_service→结束），
  // 队列轮询时同步刷新选中会话的状态，避免按钮与实际状态脱节
  const [selected, setSelected] = useState(null);
  const [history, setHistory] = useState([]);       // 当前会话的消息
  const [reply, setReply] = useState("");           // 坐席回复用户的输入
  const [note, setNote] = useState("");             // 结束处理时的结果备注
  const [acting, setActing] = useState(false);      // accept/close 请求进行中
  const listRef = useRef(null);
  const pollRef = useRef(null);

  const refreshQueue = useCallback(async () => {
    try {
      const resp = await authFetch(
        `${API_BASE}/agent/threads`, {}, AUTH_ROLE.AGENT, onLogout,
      );
      if (resp.status === 401) return;   // authFetch 已清登录态，组件将回到登录页
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setThreads(data.threads ?? []);
      setError("");
      // 同步选中会话的最新状态（可能已被其他坐席接入/关闭，多坐席并发场景）
      setSelected((sel) =>
        sel ? data.threads.find((t) => t.thread_id === sel.thread_id) ?? { ...sel, status: "closed" } : null
      );
    } catch {
      // 轮询失败不打断页面，只展示提示，下一轮自动重试
      setError("队列刷新失败，正在重试…");
    }
  }, []);

  // 队列轮询：4 秒一次，组件卸载时清理定时器
  useEffect(() => {
    refreshQueue();
    pollRef.current = setInterval(refreshQueue, POLL_INTERVAL_MS);
    return () => clearInterval(pollRef.current);
  }, [refreshQueue]);

  // 消息/历史变化时滚动到底部
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [history]);

  // 轮询选中会话的历史：用户在等待期间补发的消息要能被坐席看到。
  // 与队列轮询同频（4s），记录表读多写少、查询轻量
  useEffect(() => {
    const threadId = selected?.thread_id;
    if (!threadId) return;
    const timer = setInterval(async () => {
      try {
        const resp = await authFetch(
          `${API_BASE}/chat/${threadId}/history`, {}, AUTH_ROLE.AGENT, onLogout,
        );
        if (!resp.ok) return;
        const data = await resp.json();
        setHistory(data.messages ?? []);
      } catch {
        // 轮询失败静默：下轮重试
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [selected]);

  async function openThread(thread) {
    const threadId = thread.thread_id;
    setSelected({ thread_id: threadId, status: thread.status });
    setHistory([]);
    try {
      const resp = await authFetch(
        `${API_BASE}/chat/${threadId}/history`, {}, AUTH_ROLE.AGENT, onLogout,
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setHistory(data.messages ?? []);
      setSelected((sel) => (sel ? { ...sel, status: data.status } : null));
    } catch {
      setError("会话历史加载失败");
    }
  }

  async function act(action) {
    if (!selected || acting) return;
    setActing(true);
    try {
      // 坐席身份取自登录态：后端以 Token 中的账号为准，请求体不再传 agent_name
      const resp = await authFetch(`${API_BASE}/agent/threads/${selected.thread_id}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          resolution: action === "close" ? note || "处理完成" : "",
        }),
      }, AUTH_ROLE.AGENT, onLogout);
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        // 409 = 工单已被接走/已回退/已关闭：提示刷新而不是报错
        alert(data?.detail || "操作失败，请刷新队列");
        return;
      }
      if (action === "close") {
        // 结束处理 = 这单办完了：关闭右侧详情回到空态，坐席回到队列继续下一单
        setSelected(null);
        setHistory([]);
        setNote("");
      }
      await refreshQueue();
    } finally {
      setActing(false);
    }
  }

  // 坐席回复：写进会话记录表，用户端轮询拉取展示。
  // 回复后立即刷新本地历史，让坐席马上看到自己刚发的内容
  async function sendReply() {
    const text = reply.trim();
    if (!selected || !text || acting) return;
    setActing(true);
    try {
      const resp = await authFetch(`${API_BASE}/agent/threads/${selected.thread_id}/reply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
      }, AUTH_ROLE.AGENT, onLogout);
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        alert(data?.detail || "回复发送失败，请重试");
        return;
      }
      setReply("");
      await openThread(selected);
    } catch {
      alert("回复发送失败，请检查网络");
    } finally {
      setActing(false);
    }
  }

  return (
    <div className="agent-layout">
      {/* 左侧：待处理队列 */}
      <aside className="queue-panel">
        <div className="queue-head">
          <h2>待处理队列</h2>
          <span className="queue-count">{threads.length}</span>
        </div>
        <div className="queue-agent">
          <span className="login-user">{auth.display_name || auth.username}</span>
          <button
            className="ghost-btn"
            onClick={() => {
              clearAuth(AUTH_ROLE.AGENT);
              onLogout();
            }}
          >
            退出
          </button>
        </div>
        {error && <div className="queue-error">{error}</div>}
        <div className="queue-list">
          {threads.length === 0 && !error && (
            <div className="queue-empty">
              <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M22 12h-6l-2 3h-4l-2-3H2" />
                <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
              </svg>
              暂无待处理会话
            </div>
          )}
          {threads.map((t) => (
            <button
              key={t.thread_id}
              className={`queue-item ${selected?.thread_id === t.thread_id ? "selected" : ""} ${t.status}`}
              onClick={() => openThread(t)}
            >
              <div className="queue-item-top">
                <span className="thread-id">{t.thread_id.slice(0, 8)}</span>
                <span className={`badge ${t.status}`}>{STATUS_LABEL[t.status] ?? t.status}</span>
              </div>
              <div className="queue-reason">{t.reason || "（无转接原因）"}</div>
              <div className="queue-time">{timeAgo(t.updated_at)}</div>
            </button>
          ))}
        </div>
      </aside>

      {/* 右侧：会话详情 */}
      <section className="detail-panel">
        {!selected ? (
          <div className="detail-empty">
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="4" width="18" height="14" rx="2" />
              <path d="M3 8h18" />
            </svg>
            从队列中选择一个会话查看详情
          </div>
        ) : (
          <>
            <div className="detail-head">
              <h2>
                会话 {selected.thread_id.slice(0, 8)}
                <span className={`badge ${selected.status}`}>{STATUS_LABEL[selected.status] ?? selected.status}</span>
              </h2>
              {/* 操作按钮按会话状态显隐：
                  queued → 接入（接入后才能回复，后端有状态校验）；
                  in_service → 结束处理；closed → 无操作 */}
              <div className="detail-actions">
                {selected.status === "queued" && (
                  <button className="primary-btn" disabled={acting} onClick={() => act("accept")}>
                    接入
                  </button>
                )}
                {selected.status === "in_service" && (
                  <button className="danger-btn" disabled={acting} onClick={() => act("close")}>
                    结束处理
                  </button>
                )}
              </div>
            </div>
            <div className="msg-list" ref={listRef}>
              {history.length === 0 && <div className="queue-empty">加载中…</div>}
              {history.map((m, i) => (
                <div
                  key={i}
                  /* 坐席端视角：客户靠左，智能客服/人工坐席都靠右 */
                  className={`msg-row ${m.role === "user" ? "assistant" : "user"}`}
                >
                  <MessageBubble
                    message={m}
                    bubbleClass={agentViewBubbleClass(m.role)}
                  />
                </div>
              ))}
            </div>
            {/* 回复栏：仅接入后（in_service）显示；排队中可看记录但须先接入再回复 */}
            {selected.status === "in_service" && (
            <div className="reply-bar">
              <input
                value={reply}
                onChange={(e) => setReply(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && sendReply()}
                placeholder="回复客户…（回车发送）"
                disabled={acting}
                aria-label="回复客户"
              />
              <button
                className="send-btn"
                onClick={sendReply}
                disabled={acting || !reply.trim()}
                aria-label="发送回复"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <line x1="22" y1="2" x2="11" y2="13" />
                  <polygon points="22 2 15 22 11 13 2 9 22 2" />
                </svg>
              </button>
            </div>
            )}
            {/* 处理结果备注：仅结束处理时需要，in_service 才显示 */}
            {selected.status === "in_service" && (
            <div className="resolve-bar">
              <input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="处理结果备注（结束后记录，可选）"
              />
            </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}

export default AgentConsole;
