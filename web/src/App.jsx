import { useEffect, useState } from "react";
import UserChat from "./components/UserChat";
import AgentConsole from "./components/AgentConsole";

/**
 * 应用外壳：用户端 / 坐席端分成两个独立页面。
 *
 * 路由用原生 History API（无 react-router 依赖，项目只需两个路径）：
 * - `/`      → 用户端
 * - `/agent` → 坐席端
 *
 * 两个页面各自维护 URL，浏览器刷新/收藏/前进后退都停在原页面；
 * 坐席工作台与用户聊天彻底隔离，坐席不再需要看用户端入口。
 * Vite dev server 默认带 SPA history fallback，深链直达 /agent 也能打开。
 */

// 路径 → 页面标识（多一层间接，未来加页面只改这张表）
const ROUTES = {
  "/": "user",
  "/agent": "agent",
};

function viewFromPath() {
  return ROUTES[window.location.pathname] ?? "user";
}

function App() {
  // view: 'user' 用户端 | 'agent' 坐席端
  const [view, setView] = useState(viewFromPath);

  // 浏览器前进/后退时同步页面（pushState 不会触发 popstate，点击切换里手动 setState）
  useEffect(() => {
    const onPop = () => setView(viewFromPath());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  function navigate(path) {
    if (window.location.pathname === path) return;
    window.history.pushState(null, "", path);
    setView(ROUTES[path]);
  }

  return (
    <div className="shell">
      {/* 页面切换导航：真实 URL 而非 tab 内部状态，语义是"两个页面" */}
      <nav className="view-tabs">
        <div className="view-tabs-inner">
          <button
            className={view === "user" ? "tab active" : "tab"}
            onClick={() => navigate("/")}
          >
            用户端
          </button>
          <button
            className={view === "agent" ? "tab active" : "tab"}
            onClick={() => navigate("/agent")}
          >
            坐席端
          </button>
        </div>
      </nav>
      <div className="view-body">
        {view === "user" ? <UserChat /> : <AgentConsole />}
      </div>
    </div>
  );
}

export default App;
