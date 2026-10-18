/**
 * 前后端共享的展示常量与工具（与后端 app/api/chat.py 的 ROLE_* 对应）。
 */

export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

// 会话记录角色（chat_message.role 取值）：
// agent = 智能客服，human = 人工坐席，两者分立以便双端区分展示气泡
export const ROLE = {
  USER: "user",
  AGENT: "agent",
  HUMAN: "human",
};

// 会话转接状态（conversation_status.status 取值）
export const THREAD_STATUS = {
  ACTIVE: "active",
  QUEUED: "queued",
  IN_SERVICE: "in_service",
  CLOSED: "closed",
};

// 是否处于"已转接"状态（排队中/坐席服务中），前端据此切换 UI
export function isLiveStatus(status) {
  return status === THREAD_STATUS.QUEUED || status === THREAD_STATUS.IN_SERVICE;
}

// thread_id 本地持久化键：同一浏览器跨天回来自动续接上次会话
export const STORAGE_KEY = "shuxingou_thread_id";

// 轮询间隔（毫秒）：坐席回复/用户补发消息的可见延迟上限
export const POLL_INTERVAL_MS = 4000;
