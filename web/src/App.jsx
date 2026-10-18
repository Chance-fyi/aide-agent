import { useState } from "react";
import UserChat from "./components/UserChat";
import AgentConsole from "./components/AgentConsole";

/**
 * 应用外壳：用户端 / 坐席端双视图切换。
 *
 * MVP 用 tab 切换而非路由（无 react-router 依赖）：
 * 两个视图互不依赖状态，切换即完全重挂载，行为可预期。
 */
function App() {
  // view: 'user' 用户端 | 'agent' 坐席端
  const [view, setView] = useState("user");

  return (
    <div className="shell">
      <nav className="view-tabs">
        <button
          className={view === "user" ? "tab active" : "tab"}
          onClick={() => setView("user")}
        >
          用户端
        </button>
        <button
          className={view === "agent" ? "tab active" : "tab"}
          onClick={() => setView("agent")}
        >
          坐席端
        </button>
      </nav>
      <div className="view-body">
        {view === "user" ? <UserChat /> : <AgentConsole />}
      </div>
    </div>
  );
}

export default App;
