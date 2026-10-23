import { useState, useEffect, useRef, useCallback } from "react";
import { API_BASE, AUTH_ROLE, getAuth, clearAuth, authFetch, errorMessage } from "../constants";
import LoginView from "./LoginView";

/**
 * 知识库管理台。
 *
 * 与后端的协议（app/api/kb_admin.py）：
 * - GET  /kb/overview                  → 统计 + 向量集合 + 上次重建 + 是否需重建
 * - GET  /kb/documents                 → 文件清单（策略/块数/是否落后）
 * - GET  /kb/documents/chunks?path=    → 某文件的切块预览
 * - POST /kb/documents/strategy        → 设置单文件切分策略（重建后生效）
 * - POST /kb/rebuild                   → 触发重建（202 + 任务，异步）
 * - GET  /kb/rebuild                   → 轮询任务状态
 * - POST /kb/search?q=&top_k=          → 用生产链路验证检索
 *
 * 为什么需要这一页：在此之前生产环境没有任何入口能更新知识库——
 * 重建只能 `curl POST /debug/kb/rebuild`，而 debug 路由生产不注册。
 * 改语料就得重启服务，或者临时打开 debug（等于暴露一堆内部接口）。
 *
 * 权限：坐席账号登录（与坐席工作台共用登录态）。知识库是所有坐席的公共资产，
 * 不引入"管理员"这个新角色——项目只有 user/agent 两级。
 */

// 重建是 20 秒级的操作，轮询间隔取 1.5 秒：够及时，又不会把后端刷爆
const POLL_MS = 1500;

// 状态徽章：与坐席端同一套语义类名（排队=琥珀、成功=绿、失败=红）
const JOB_STATUS = {
  running: { label: "重建中", cls: "badge--queued" },
  succeeded: { label: "成功", cls: "badge--in_service" },
  failed: { label: "失败", cls: "badge--failed" },
};

function timeAgo(iso) {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec} 秒前`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour} 小时前`;
  return `${Math.floor(hour / 24)} 天前`;
}

function duration(job) {
  if (!job?.finished_at) return null;
  const ms = new Date(job.finished_at) - new Date(job.started_at);
  return `${(ms / 1000).toFixed(1)}s`;
}

/** 外层：坐席登录门卫（hooks 不能写在条件 return 之后，所以拆两层） */
function KbConsole() {
  const [auth, setAuth] = useState(() => getAuth(AUTH_ROLE.AGENT));
  if (!auth) {
    return <LoginView role={AUTH_ROLE.AGENT} onLogin={setAuth} />;
  }
  return <KbConsoleInner auth={auth} onLogout={() => setAuth(null)} />;
}

function KbConsoleInner({ auth, onLogout }) {
  const [overview, setOverview] = useState(null);
  const [documents, setDocuments] = useState([]);
  const [strategies, setStrategies] = useState([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  // 选中的文件（右侧切块预览）
  const [activeDoc, setActiveDoc] = useState(null);
  const [chunks, setChunks] = useState([]);
  const [chunksLoading, setChunksLoading] = useState(false);

  // 检索验证
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState(null);
  const [searching, setSearching] = useState(false);

  const pollRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const [ov, docs] = await Promise.all([
        authFetch(`${API_BASE}/kb/overview`, {}, AUTH_ROLE.AGENT, onLogout),
        authFetch(`${API_BASE}/kb/documents`, {}, AUTH_ROLE.AGENT, onLogout),
      ]);
      if (!ov.ok || !docs.ok) throw new Error(`HTTP ${ov.status}/${docs.status}`);
      setOverview(await ov.json());
      const d = await docs.json();
      setDocuments(d.documents ?? []);
      setStrategies(d.strategies ?? []);
      setError("");
    } catch {
      setError("加载知识库状态失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  }, [onLogout]);

  // 首屏加载：load() 内部是异步的，卸载后到达的响应不该再写 state
  useEffect(() => {
    let alive = true;
    (async () => {
      await load();
      if (!alive) return;
    })();
    return () => {
      alive = false;
    };
  }, [load]);

  // 打开文件的切块预览。提成 useCallback 是为了让轮询回调能安全引用它
  // （否则每次渲染都是新函数，轮询闭包会捕获过期版本）
  const openDoc = useCallback(async (doc) => {
    setActiveDoc(doc);
    setChunksLoading(true);
    setChunks([]);
    try {
      const resp = await authFetch(
        `${API_BASE}/kb/documents/chunks?path=${encodeURIComponent(doc.path)}&limit=5`,
        {},
        AUTH_ROLE.AGENT,
        onLogout,
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setChunks(data.chunks ?? []);
    } catch {
      setChunks([]);
    } finally {
      setChunksLoading(false);
    }
  }, [onLogout]);

  // 重建中轮询：直到 running 变 false 才停。
  // 用 ref 存 timer 以便组件卸载/手动刷新时清理，避免离开页面后仍在打接口
  const stopPolling = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
  }, []);

  // 卸载时停掉轮询（返回 cleanup 函数）
  useEffect(() => stopPolling, [stopPolling]);

  const startPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const resp = await authFetch(
          `${API_BASE}/kb/rebuild`, {}, AUTH_ROLE.AGENT, onLogout,
        );
        if (!resp.ok) return;
        const data = await resp.json();
        setOverview((prev) => (prev ? { ...prev, rebuild: data.job, rebuild_running: data.running } : prev));
        if (!data.running) {
          stopPolling();
          // 重建会改变块数/索引时间/检索结果，整体重载一次
          load();
          if (activeDoc) openDoc(activeDoc);
        }
      } catch {
        // 单次轮询失败静默：下一轮继续
      }
    }, POLL_MS);
  }, [onLogout, load, openDoc, activeDoc, stopPolling]);

  // 首屏若已有任务在跑（比如别人触发的），直接开始轮询
  useEffect(() => {
    if (overview?.rebuild_running) startPolling();
  }, [overview?.rebuild_running, startPolling]);

  async function rebuild() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const resp = await authFetch(
        `${API_BASE}/kb/rebuild`,
        { method: "POST" },
        AUTH_ROLE.AGENT,
        onLogout,
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setError(errorMessage(data, "触发重建失败"));
        return;
      }
      const data = await resp.json();
      setOverview((prev) => (prev ? { ...prev, rebuild: data.job, rebuild_running: true } : prev));
      startPolling();
    } catch {
      setError("触发重建失败，请检查网络");
    } finally {
      setBusy(false);
    }
  }

  async function setStrategy(path, strategy) {
    try {
      const resp = await authFetch(
        `${API_BASE}/kb/documents/strategy`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, strategy: strategy || null }),
        },
        AUTH_ROLE.AGENT,
        onLogout,
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setError(errorMessage(data, "保存策略失败"));
        return;
      }
      // 本地同步：让下拉框立刻反映选择，同时标记该文件需要重建
      setDocuments((prev) =>
        prev.map((d) =>
          d.path === path
            ? {
                ...d,
                override_strategy: strategy || null,
                effective_strategy: strategy || d.default_strategy,
                stale: true,
              }
            : d,
        ),
      );
    } catch {
      setError("保存策略失败，请检查网络");
    }
  }

  async function search(e) {
    e.preventDefault();
    const q = query.trim();
    if (!q || searching) return;
    setSearching(true);
    setSearchResults(null);
    try {
      const resp = await authFetch(
        `${API_BASE}/kb/search?q=${encodeURIComponent(q)}&top_k=3`,
        { method: "POST" },
        AUTH_ROLE.AGENT,
        onLogout,
      );
      const data = await resp.json().catch(() => null);
      if (!resp.ok) {
        setError(errorMessage(data, "检索失败"));
        return;
      }
      setSearchResults(data);
    } catch {
      setError("检索失败，请检查网络");
    } finally {
      setSearching(false);
    }
  }

  const stats = overview?.stats;
  const running = overview?.rebuild_running;
  const job = overview?.rebuild;
  const pending = overview?.pending_rebuild ?? [];
  const collection = overview?.collection ?? {};

  return (
    <section className="kb">
      <h1 className="sr-only">知识库管理</h1>

      <header className="kb-head">
        <div>
          <h2 className="kb-title">知识库</h2>
          <p className="kb-sub">
            语料 → 切块 → 向量索引。改完语料或切分策略后需要重建才生效。
          </p>
        </div>
        <div className="kb-head-actions">
          <span className="topbar-user">{auth.display_name || auth.username}</span>
          <button className="btn" onClick={() => { clearAuth(AUTH_ROLE.AGENT); onLogout(); }}>
            退出
          </button>
        </div>
      </header>

      {error && <div className="rail-note rail-note--error kb-note">{error}</div>}

      {/* 需要重建的提醒：语料比索引新时，线上检索用的还是旧内容。
          这是管理台最重要的信号，放在最上方 */}
      {pending.length > 0 && !running && (
        <div className="rail-note rail-note--warn kb-note">
          <strong>{pending.length} 个文件</strong>的语料比索引新，线上检索仍是旧内容：
          <span className="mono"> {pending.slice(0, 4).join(", ")}</span>
          {pending.length > 4 && <span className="mono"> 等</span>}
        </div>
      )}

      {/* 概览读数：等宽数字，像仪表盘 */}
      <div className="kb-metrics">
        <div className="metric">
          <span className="metric-label">文件</span>
          <span className="metric-value mono">{loading ? "—" : (overview?.file_count ?? 0)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">切块</span>
          <span className="metric-value mono">{loading ? "—" : (stats?.chunks ?? 0)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">父块</span>
          <span className="metric-value mono">{loading ? "—" : (stats?.parents ?? 0)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">向量库</span>
          <span className="metric-value mono">
            {collection.error ? "不可用" : (collection.chunks ?? "—")}
          </span>
        </div>
        <div className="metric metric--wide">
          <span className="metric-label">集合</span>
          <span className="metric-value mono metric-value--sm">
            {collection.error ? "—" : (collection.current_collection ?? "未建库")}
          </span>
        </div>
      </div>

      {/* 重建区：按钮 + 最近一次结果 */}
      <div className="kb-rebuild">
        <button className="btn-primary" onClick={rebuild} disabled={busy || running}>
          {running ? "重建进行中…" : "重建索引"}
        </button>

        {job && (
          <span className="kb-job">
            <span className={`badge ${JOB_STATUS[job.status]?.cls ?? "badge--closed"}`}>
              <span className="dot" />
              {JOB_STATUS[job.status]?.label ?? job.status}
            </span>
            <span className="mono">{timeAgo(job.finished_at ?? job.started_at)}</span>
            {job.chunks != null && <span className="mono">{job.chunks} 块</span>}
            {duration(job) && <span className="mono">耗时 {duration(job)}</span>}
            <span className="mono">by {job.triggered_by || "—"}</span>
          </span>
        )}
        {running && <span className="kb-hint">全量重建约需 20-30 秒，期间旧索引仍在服务</span>}
        {job?.status === "failed" && job.error && (
          <span className="kb-error">{job.error}</span>
        )}
        {collection.stale_collections?.length > 0 && (
          <span className="kb-hint">
            残留旧集合 {collection.stale_collections.length} 个（可清理释放空间）
          </span>
        )}
      </div>

      <div className="kb-body">
        {/* 左：文件清单 */}
        <div className="kb-docs">
          <div className="kb-panel-head">
            <h3>语料文件</h3>
            <span className="count mono">
              <span aria-hidden="true">{String(documents.length).padStart(2, "0")}</span>
              <span className="sr-only">{documents.length} 个文件</span>
            </span>
          </div>

          <div className="kb-table-wrap">
            <table className="kb-table">
              <thead>
                <tr>
                  <th scope="col">文件</th>
                  <th scope="col">策略</th>
                  <th scope="col" className="num">块</th>
                  <th scope="col">状态</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((d) => (
                  <tr
                    key={d.path}
                    className={activeDoc?.path === d.path ? "selected" : ""}
                    onClick={() => openDoc(d)}
                  >
                    <td className="kb-path">{d.path}</td>
                    <td>
                      {/* 下拉框即改即存；空值 = 用后缀默认策略 */}
                      <select
                        value={d.override_strategy ?? ""}
                        aria-label={`${d.path} 的切分策略`}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => setStrategy(d.path, e.target.value)}
                      >
                        <option value="">默认（{d.default_strategy}）</option>
                        {strategies.map((s) => (
                          <option key={s} value={s}>{s}</option>
                        ))}
                      </select>
                    </td>
                    <td className="num mono">{d.chunks}</td>
                    <td>
                      {d.stale ? (
                        <span className="badge badge--queued"><span className="dot" />待重建</span>
                      ) : (
                        <span className="badge badge--closed"><span className="dot" />已索引</span>
                      )}
                    </td>
                  </tr>
                ))}
                {!loading && documents.length === 0 && (
                  <tr>
                    <td colSpan={4} className="kb-empty">
                      语料目录为空，请把 Markdown 文件放入 data/knowledge_base/
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* 右：切块预览 + 检索验证 */}
        <div className="kb-side">
          <div className="kb-panel">
            <div className="kb-panel-head">
              <h3>切块预览</h3>
              {activeDoc && <span className="mono kb-panel-sub">{activeDoc.path}</span>}
            </div>
            <div className="kb-panel-body">
              {!activeDoc && <p className="kb-hint">点左侧文件名查看它的切块内容</p>}
              {activeDoc && chunksLoading && <p className="kb-hint">加载中…</p>}
              {activeDoc && !chunksLoading && chunks.length === 0 && (
                <p className="kb-hint">该文件还没有切块（可能尚未重建）</p>
              )}
              {chunks.map((c) => (
                <div key={c.id} className="kb-chunk">
                  <div className="kb-chunk-meta mono">
                    #{c.id}
                    {c.h1 && <span> · {c.h1}</span>}
                    {c.h2 && <span> / {c.h2}</span>}
                    <span> · {c.content.length} 字</span>
                  </div>
                  <pre className="kb-chunk-body">{c.content.slice(0, 400)}</pre>
                </div>
              ))}
            </div>
          </div>

          <div className="kb-panel">
            <div className="kb-panel-head">
              <h3>检索验证</h3>
              <span className="mono kb-panel-sub">生产链路</span>
            </div>
            <form className="kb-search" onSubmit={search}>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="输入问题，看实际会检索到什么"
                aria-label="检索验证查询"
              />
              <button className="btn" type="submit" disabled={searching || !query.trim()}>
                {searching ? "检索中" : "检索"}
              </button>
            </form>
            <div className="kb-panel-body">
              {searchResults?.results?.length === 0 && (
                <p className="kb-hint">
                  没有命中（分数低于阈值 {overview?.config?.min_score}）。这是预期行为：
                  宁可"没检到"也不把无关内容喂给模型。
                </p>
              )}
              {searchResults?.results?.map((r, i) => (
                <div key={i} className="kb-chunk">
                  <div className="kb-chunk-meta mono">
                    <span className={r.score < (overview?.config?.min_score ?? 0.4) ? "score-low" : "score-ok"}>
                      {r.score.toFixed(3)}
                    </span>
                    <span> · {r.source}</span>
                    {r.h2 && <span> / {r.h2}</span>}
                  </div>
                  <pre className="kb-chunk-body">{r.content.slice(0, 300)}</pre>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

export default KbConsole;
