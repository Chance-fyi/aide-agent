import { useEffect, useState } from "react";
import UserChat from "./components/UserChat";
import AgentConsole from "./components/AgentConsole";
import KbConsole from "./components/KbConsole";

/**
 * 应用外壳：用户端 / 坐席端分成两个独立页面。
 *
 * 路由用原生 History API（无 react-router 依赖，项目只需三个路径）：
 * - `/`      → 用户端
 * - `/agent` → 坐席端
 * - `/kb`    → 知识库管理（坐席登录，生产可用）
 *
 * 视觉上外壳只做一件事：一条 48px 深色顶栏。旧版是"渐变背景 + 悬浮圆角卡片"，
 * 内容被包在浮层里；新版把 chrome 压成一条铭牌式的深色条，工作区满幅铺开——
 * 读起来是工具，不是落地页。
 */

// 路径 → 页面标识（多一层间接，未来加页面只改这张表）
const ROUTES = {
  "/": "user",
  "/agent": "agent",
  "/kb": "kb",
};

function viewFromPath() {
  return ROUTES[window.location.pathname] ?? "user";
}

// 品牌标识：几何线条（一个方框 + 内部斜切），比汉字圆角块更中性，
// 也不依赖任何字体渲染
function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <rect x="1.5" y="1.5" width="17" height="17" rx="2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M6 13.5V9.5M10 13.5V6.5M14 13.5v-2.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="square" />
    </svg>
  );
}

function App() {
  // view: 'user' 用户端 | 'agent' 坐席端 | 'kb' 知识库管理
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
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <BrandMark />
          <span className="brand-name">舒心购</span>
          <span className="brand-tag">support console</span>
        </div>
        {/* 视图切换用 aria-current 表达"当前页"，
            无障碍上比 class 高亮更准确（读屏会念"当前页面"） */}
        <nav className="switch" aria-label="视图切换">
          <button
            className="switch-btn"
            aria-current={view === "user" ? "page" : undefined}
            onClick={() => navigate("/")}
          >
            用户端
          </button>
          <button
            className="switch-btn"
            aria-current={view === "agent" ? "page" : undefined}
            onClick={() => navigate("/agent")}
          >
            坐席端
          </button>
          <button
            className="switch-btn"
            aria-current={view === "kb" ? "page" : undefined}
            onClick={() => navigate("/kb")}
          >
            知识库
          </button>
        </nav>
      </header>
      <main className="workspace">
        {view === "user" && <UserChat />}
        {view === "agent" && <AgentConsole />}
        {view === "kb" && <KbConsole />}
      </main>
    </div>
  );
}

export default App;
