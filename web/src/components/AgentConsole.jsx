import { useState, useEffect, useRef, useCallback, useLayoutEffect } from "react";
import {
  API_BASE, AUTH_ROLE, getAuth, clearAuth, authFetch, errorMessage,
} from "../constants";
import { createSocket } from "../ws";
import { mergeMessage } from "../messages";
import { MessageBubble } from "./MessageBubble";
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
 * 布局：左侧队列栏（比详情区深一档底色）+ 右侧会话区。
 * 不用"两张白卡片并排"——那样两个区域权重相同，坐席分不清主次。
 */

const STATUS_LABEL = {
  queued: "排队中",
  in_service: "处理中",
  active: "自助中",
  closed: "已结束",
  // 本地态：会话已从队列消失但不是被自己接入/关闭的（被其他坐席接走）
  locked: "他人在办",
};

// 状态徽章的修饰类：类名不能直接用状态值（带下划线/特殊字符时易踩坑，
// 也让"状态值"和"样式名"耦合），显式映射一份更安全
const STATUS_CLASS = {
  queued: "badge--queued",
  in_service: "badge--in_service",
  closed: "badge--closed",
  locked: "badge--locked",
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
  // 因为操作按钮按状态显隐（queued→接入 / in_service→结束）
  const [selected, setSelected] = useState(null);
  const [history, setHistory] = useState([]);       // 当前会话的消息
  // 用户完整历史流：跨 thread 合并。null = 匿名会话/未加载，回退 thread history
  const [userHistory, setUserHistory] = useState(null);
  const [reply, setReply] = useState("");
  const [note, setNote] = useState("");
  const [acting, setActing] = useState(false);
  const [connected, setConnected] = useState(false);
  const listRef = useRef(null);
  const socketRef = useRef(null);

  // 事件回调里要读"当前选中的会话"，但回调在建立连接时就被闭包捕获了。
  // 用 ref 存一份最新的；赋值放 effect 而不是渲染期（渲染期写 ref 在
  // 并发渲染下可能被丢弃的那次渲染写脏）
  const selectedRef = useRef(null);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  // 队列快照 → state，并同步选中会话的状态（多坐席并发场景）：
  // - 会话已从队列消失：若不是自己刚关闭的，说明被其他坐席接走
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
      // 拉取失败不打断页面（WS 的 queue_changed 会再触发一次）
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
          // 两条流都并入（实际只展示其中一条：有用户历史流就展示它）。
          // 必须用 mergeMessage：自己发出的回复也会被广播回来
          setHistory((prev) => mergeMessage(prev, msg));
          setUserHistory((prev) => (prev === null ? null : mergeMessage(prev, msg)));
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
  // 匿名会话（无归属用户）返回空列表，保持 null 以便回退到 thread history。
  //
  // 会话守卫：坐席快速连点两个工单时，先发的请求可能**后**返回，
  // 把面板刷成上一个工单的内容。所以回写前确认"当前选中的还是它"。
  async function loadUserHistory(threadId) {
    try {
      const resp = await authFetch(
        `${API_BASE}/agent/threads/${threadId}/user_history`,
        {}, AUTH_ROLE.AGENT, onLogout,
      );
      if (!resp.ok) return;
      const data = await resp.json();
      if (selectedRef.current?.thread_id !== threadId) return;   // 已切走，丢弃
      if (data.messages?.length) setUserHistory(data.messages);
    } catch {
      // 加载失败静默：详情面板继续展示当前 thread 历史，不阻断
    }
  }

  async function openThread(thread) {
    const threadId = thread.thread_id;
    setSelected({
      thread_id: threadId,
      status: thread.status,
      user_display_name: thread.user_display_name,
    });
    setHistory([]);
    setUserHistory(null);   // 主动切换会话时才清空，避免显示上一单内容
    try {
      const resp = await authFetch(
        `${API_BASE}/chat/${threadId}/history`, {}, AUTH_ROLE.AGENT, onLogout,
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      // 守卫同上：慢响应回来时用户可能已经点了别的工单
      if (selectedRef.current?.thread_id !== threadId) return;
      setHistory(data.messages ?? []);
      setSelected((sel) => (sel ? { ...sel, status: data.status } : null));
      loadUserHistory(threadId);
    } catch {
      setError("会话历史加载失败");
    }
  }

  async function act(action) {
    if (!selected || acting) return;
    setActing(true);
    try {
      // 坐席身份取自登录态：后端以 Token 中的账号为准
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
      const data = await resp.json().catch(() => null);
      setReply("");
      // 用服务端回传的权威消息（含 id / created_at）并入列表，
      // 不再自己拼一条本地消息：本地时间戳会让时间轴倒挂，
      // 且没有 id 就无法与随后到达的 WS 推送合并，最终表现为两条重复
      if (data?.message) {
        const sent = { ...data.message, thread_id: selected.thread_id };
        setHistory((prev) => mergeMessage(prev, sent));
        setUserHistory((prev) => (prev === null ? null : mergeMessage(prev, sent)));
      }
    } catch {
      alert("回复发送失败，请检查网络");
    } finally {
      setActing(false);
    }
  }

  const visible = userHistory ?? history;

  return (
    <div className="console">
      {/* 页面级标题只给读屏用：视觉上顶栏已经说明了"坐席端"，
          但没有 h1 会让屏幕阅读器失去页面结构的起点 */}
      <h1 className="sr-only">坐席工作台</h1>
      {/* 左侧：待处理队列 */}
      <aside className="rail">
        <div className="rail-head">
          <h2>待处理队列</h2>
          {/* 计数用 aria-hidden 藏掉裸数字，另给一句完整语义：
              读屏念"待处理会话 3 个"而不是孤零零的"3" */}
          <span className="count">
            <span aria-hidden="true">{String(threads.length).padStart(2, "0")}</span>
            <span className="sr-only">待处理会话 {threads.length} 个</span>
          </span>
        </div>

        {!connected && <div className="rail-note rail-note--warn">实时连接已断开，正在重连…</div>}
        {error && <div className="rail-note rail-note--error">{error}</div>}

        <div className="queue">
          {threads.length === 0 && !error && (
            <div className="thread-empty" style={{ marginTop: 40 }}>
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M3 6h18M3 12h18M3 18h11" />
              </svg>
              暂无待处理会话
            </div>
          )}

          {threads.map((t) => (
            <button
              key={t.thread_id}
              className="queue-item"
              // 选中态用 aria-current 表达，不只靠颜色
              aria-current={selected?.thread_id === t.thread_id ? "true" : undefined}
              onClick={() => openThread(t)}
            >
              <div className="queue-top">
                {/* 优先显示用户昵称（后端 JOIN 得出）；匿名会话回退"游客 + ID 前 8 位" */}
                <span className="queue-name">
                  {t.user_display_name || `游客 ${t.thread_id.slice(0, 8)}`}
                </span>
                <span className={`badge ${STATUS_CLASS[t.status] ?? "badge--closed"}`}>
                  <span className="dot" />
                  {STATUS_LABEL[t.status] ?? t.status}
                </span>
              </div>
              <div className="queue-reason">{t.reason || "（无转接原因）"}</div>
              <div className="queue-time">{timeAgo(t.updated_at)}</div>
            </button>
          ))}
        </div>

        <div className="rail-foot">
          <span className="who">{auth.display_name || auth.username}</span>
          <button
            className="btn"
            onClick={() => {
              clearAuth(AUTH_ROLE.AGENT);
              onLogout();
            }}
          >
            退出
          </button>
        </div>
      </aside>

      {/* 右侧：会话详情 */}
      <section className="thread">
        {!selected ? (
          <div className="thread-empty">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="4" width="18" height="15" rx="2" />
              <path d="M3 9h18" />
            </svg>
            从左侧队列选择一个会话
          </div>
        ) : (
          <>
            <header className="thread-head">
              <h2 className="thread-name">
                {selected.user_display_name || `游客 ${selected.thread_id.slice(0, 8)}`}
              </h2>
              <span className={`badge ${STATUS_CLASS[selected.status] ?? "badge--closed"}`}>
                <span className="dot" />
                {STATUS_LABEL[selected.status] ?? selected.status}
              </span>
              {/* 完整 ID 给排查用：坐席反馈问题时需要能定位到具体会话 */}
              <span className="thread-id">{selected.thread_id.slice(0, 8)}</span>
              <div className="thread-actions">
                {/* 操作按钮按会话状态显隐：
                    queued → 接入（接入后才能回复，后端有状态校验）；
                    in_service → 结束处理；closed → 无操作 */}
                {selected.status === "queued" && (
                  <button className="btn-primary" disabled={acting} onClick={() => act("accept")}>
                    接入
                  </button>
                )}
                {selected.status === "in_service" && (
                  <button className="btn-danger" disabled={acting} onClick={() => act("close")}>
                    结束处理
                  </button>
                )}
              </div>
            </header>

            {/* 消息列表：优先展示用户完整历史流（旧会话降透明度），
                匿名会话/未加载回退当前 thread 历史 */}
            <div className="log" ref={listRef} role="log" aria-live="polite" aria-label="会话消息">
              {visible.length === 0 && (
                <div className="thread-empty" style={{ marginTop: 40 }}>加载中…</div>
              )}
              {visible.map((m, i) => (
                <MessageBubble
                  key={`${m.thread_id ?? selected.thread_id}|${m.ts ?? ""}|${m.role}|${i}`}
                  message={m}
                  // 坐席视角：客户消息靠左，客服/坐席消息靠右（与自己同侧）
                  side={m.role === "user" ? "start" : "end"}
                  // 历史流里非当前 thread 的消息置灰，坐席能一眼看出哪条属于本轮
                  old={userHistory !== null && m.thread_id && m.thread_id !== selected.thread_id}
                />
              ))}
            </div>

            {/* 回复区：仅接入后（in_service）显示；排队中可看记录但须先接入 */}
            {selected.status === "in_service" && (
              <div className="reply">
                <form
                  className="reply-row"
                  onSubmit={(e) => {
                    e.preventDefault();
                    sendReply();
                  }}
                >
                  <input
                    value={reply}
                    onChange={(e) => setReply(e.target.value)}
                    placeholder="回复客户，回车发送"
                    disabled={acting}
                    aria-label="回复客户"
                  />
                  <button
                    className="send"
                    type="submit"
                    disabled={acting || !reply.trim()}
                    aria-label="发送回复"
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M4 12h15M13 6l6 6-6 6" />
                    </svg>
                  </button>
                </form>
                {/* 处理结果备注：次级动作，视觉上明显弱于回复框 */}
                <div className="note-row">
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="处理结果备注（结束处理时记录，可选）"
                    aria-label="处理结果备注"
                  />
                </div>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}

export default AgentConsole;
