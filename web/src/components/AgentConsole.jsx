import { useState, useEffect, useRef, useCallback, useLayoutEffect } from "react";
import {
  API_BASE, AUTH_ROLE, getAuth, clearAuth, authFetch, errorMessage,
} from "../constants";
import { createSocket } from "../ws";
import { MessageBubble, agentViewBubbleClass } from "./MessageBubble";
import LoginView from "./LoginView";

/**
 * 坐席端控制台（模拟坐席工作台）。
 *
 * 与后端的协议：
 * - WS /ws/agent
 *   收 → ready（队列全量快照）/ queue_changed（队列有变，重拉）
 *        / message（正在查看的会话有新消息）/ error / pong
 *   发 → {"type":"watch","thread_id":"..."}（订阅当前查看的会话）/ ping
 * - GET  /agent/threads                      → 队列（按坐席过滤，故仍走 REST）
 * - GET  /chat/{thread_id}/history           → 会话历史
 * - POST /agent/threads/{thread_id}/accept   → 接入（queued → in_service）
 * - POST /agent/threads/{thread_id}/close    → 结束（→ closed）
 * - POST /agent/threads/{thread_id}/reply    → 回复（落库后服务端推给用户端）
 *
 * 为什么队列用"信号 + 重拉"而不是直接推列表：队列是按坐席过滤的
 * （in_service 只对接入者可见），推列表要给每条连接算一份；
 * 推信号让客户端用同一个 REST 接口取自己那份，过滤口径只有一处。
 */

const STATUS_LABEL = {
  queued: "排队中",
  in_service: "处理中",
  active: "自助中",
  closed: "已结束",
  // 本地态：会话已从队列消失但不是被自己接入/关闭的（被其他坐席接走）。
  // 只用于详情面板展示，不出现在队列列表里
  locked: "其他坐席处理中",
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
  // 队列刷新时同步更新选中会话的状态，避免按钮与实际状态脱节
  const [selected, setSelected] = useState(null);
  const [history, setHistory] = useState([]);       // 当前会话的消息
  // 用户完整历史流：跨 thread 合并（含当前会话与以前会话）。
  // null = 匿名会话/未加载，回退展示当前 thread 的 history
  const [userHistory, setUserHistory] = useState(null);
  const [reply, setReply] = useState("");           // 坐席回复用户的输入
  const [note, setNote] = useState("");             // 结束处理时的结果备注
  const [acting, setActing] = useState(false);      // accept/close/reply 请求进行中
  const [connected, setConnected] = useState(false); // WS 是否已连上
  const listRef = useRef(null);
  const socketRef = useRef(null);
  // 事件回调里要读"当前选中的会话"，但回调在建立连接时就被闭包捕获了。
  // 用 ref 存一份最新的，回调里读 ref 才不会拿到过期值。
  // 赋值放 effect 而不是渲染期：渲染期写 ref 在并发渲染下可能被丢弃的
  // 那次渲染写脏（React 官方不推荐，oxlint 也会告警）
  const selectedRef = useRef(null);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  // 队列快照 → state，并同步选中会话的状态（多坐席并发场景）：
  // - 会话已从队列消失：若不是自己刚关闭的，说明被其他坐席接走/关闭，
  //   标记为 locked 展示"其他坐席处理中"（没有操作按钮与回复栏）
  // - 会话还在队列里：以队列返回的最新状态为准（按钮显隐跟着变）
  const applyQueue = useCallback((list) => {
    setThreads(list);
    setError("");
    setSelected((sel) => {
      if (!sel) return null;
      const found = list.find((t) => t.thread_id === sel.thread_id);
      if (found) return { ...sel, ...found };
      return sel.status === "locked" || sel.status === "closed"
        ? sel
        : { ...sel, status: "locked" };
    });
  }, []);

  const refreshQueue = useCallback(async () => {
    try {
      const resp = await authFetch(
        `${API_BASE}/agent/threads`, {}, AUTH_ROLE.AGENT, onLogout,
      );
      if (resp.status === 401) return;   // authFetch 已清登录态，组件将回到登录页
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      applyQueue(data.threads ?? []);
    } catch {
      // 拉取失败不打断页面，只展示提示（WS 的 queue_changed 会再触发一次）
      setError("队列刷新失败，正在重试…");
    }
  }, [applyQueue, onLogout]);

  // 实时通道：队列变化信号 + 当前会话的新消息
  useEffect(() => {
    const socket = createSocket({
      path: "/ws/agent",
      token: auth.token,
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
      onAuthError: () => {
        clearAuth(AUTH_ROLE.AGENT);
        onLogout();
      },
      onEvent: (ev) => {
        if (ev.type === "ready") {
          applyQueue(ev.threads ?? []);
        } else if (ev.type === "queue_changed") {
          // 只推信号不推列表：按坐席过滤的逻辑留在后端一个地方
          refreshQueue();
        } else if (ev.type === "message") {
          // 只处理"正在查看的会话"：其他会话的消息不打扰当前阅读
          const sel = selectedRef.current;
          if (!sel || ev.thread_id !== sel.thread_id) return;
          const msg = { ...ev.message, thread_id: ev.thread_id };
          // 两条流都追加（实际只展示其中一条：有用户历史流就展示它）
          setHistory((prev) => [...prev, msg]);
          setUserHistory((prev) => (prev === null ? null : [...prev, msg]));
        } else if (ev.type === "error") {
          setError(ev.message);
        }
      },
    });
    socketRef.current = socket;
    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [auth.token, applyQueue, refreshQueue, onLogout]);

  // 订阅当前查看的会话：坐席打开哪一单，服务端就把那一单的消息推过来
  useEffect(() => {
    const threadId = selected?.thread_id;
    if (!threadId) return;
    socketRef.current?.send({ type: "watch", thread_id: threadId });
  }, [selected?.thread_id]);

  // 只在显示消息数量变化时贴底；acting 等无关状态更新不碰滚动位置
  const previousVisibleCount = useRef(0);
  useLayoutEffect(() => {
    const el = listRef.current;
    const count = (userHistory ?? history).length;
    if (!el || count === previousVisibleCount.current) return;
    previousVisibleCount.current = count;
    el.scrollTop = el.scrollHeight;
  }, [history, userHistory]);

  // 加载用户完整历史流（打开会话时调用一次）。
  // 匿名会话（无归属用户）返回空列表，保持 null 以便回退到 thread history
  async function loadUserHistory(threadId) {
    try {
      const resp = await authFetch(
        `${API_BASE}/agent/threads/${threadId}/user_history`,
        {}, AUTH_ROLE.AGENT, onLogout,
      );
      if (!resp.ok) return;
      const data = await resp.json();
      // 有归属且有消息才切换到历史流视图；否则维持 thread history 回退
      if (data.messages?.length) setUserHistory(data.messages);
    } catch {
      // 加载失败静默：详情面板继续展示当前 thread 历史，不阻断
    }
  }

  async function openThread(thread) {
    const threadId = thread.thread_id;
    // 整包存队列项：user_display_name 供详情标题展示，status 供操作按钮显隐
    setSelected({ thread_id: threadId, status: thread.status, user_display_name: thread.user_display_name });
    setHistory([]);
    setUserHistory(null);   // 用户主动切换会话时才清空，避免显示上一单内容
    try {
      const resp = await authFetch(
        `${API_BASE}/chat/${threadId}/history`, {}, AUTH_ROLE.AGENT, onLogout,
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setHistory(data.messages ?? []);
      setSelected((sel) => (sel ? { ...sel, status: data.status } : null));
      // 异步加载完整历史流：成功后详情面板切到跨会话视图
      loadUserHistory(threadId);
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
        alert(errorMessage(data, "操作失败，请刷新队列"));
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

  // 坐席回复：服务端落库后通过 WebSocket 推给用户端。
  // 本地立即追加，坐席不必等推送回来才看到自己发的内容
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
        alert(errorMessage(data, "回复发送失败，请重试"));
        return;
      }
      const humanMessage = {
        role: "human",
        content: text,
        ts: new Date().toISOString(),
        sender_name: auth.display_name || auth.username,
        thread_id: selected.thread_id,
      };
      setReply("");
      // 乐观更新当前消息流：不清空/重载整个会话，避免发送后列表闪空再重绘
      setHistory((prev) => [...prev, humanMessage]);
      setUserHistory((prev) => prev === null ? null : [...prev, humanMessage]);
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
        {!connected && <div className="queue-error">实时连接已断开，正在重连…</div>}
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
                {/* 优先显示用户昵称（后端 JOIN 得出）；匿名会话回退"游客 + ID 前 8 位" */}
                <span className="thread-id">
                  {t.user_display_name || `游客 ${t.thread_id.slice(0, 8)}`}
                </span>
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
                {selected.user_display_name || `游客 ${selected.thread_id.slice(0, 8)}`}
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
            {/* 消息列表：优先展示用户完整历史流（旧会话置灰），
                匿名会话/未加载回退当前 thread 历史 */}
            <div className="msg-list" ref={listRef}>
              {(userHistory ?? history).length === 0 && <div className="queue-empty">加载中…</div>}
              {(userHistory ?? history).map((m, i) => {
                // 完整历史流里非当前 thread 的消息 = 以前会话的记录：置灰区分，
                // 坐席能一眼看出哪些是本轮对话、哪些是用户以前问过的
                const isOldThread = userHistory !== null && m.thread_id && m.thread_id !== selected.thread_id;
                return (
                  <div
                    key={`${m.thread_id ?? selected.thread_id}|${m.ts ?? ""}|${m.role}|${m.content}|${i}`}
                    className={`msg-row ${m.role === "user" ? "assistant" : "user"} ${isOldThread ? "old-thread" : ""}`}
                  >
                    <MessageBubble
                      message={m}
                      bubbleClass={agentViewBubbleClass(m.role)}
                    />
                  </div>
                );
              })}
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
