/**
 * WebSocket 客户端封装（用户端 / 坐席端共用）。
 *
 * 为什么不用裸 WebSocket：三件"必须自己写"的事都在这里收口
 * 1. 断线重连：指数退避（1s→2s→…→10s 封顶），服务端重启时不会把后端打爆
 * 2. 心跳：浏览器不能发 ping 帧，只能应用层 ping/pong。
 *    更关键的是——网络静默断开（拔网线/休眠/代理超时）时 readyState 仍是 OPEN，
 *    永远等不到 onclose。所以按"多久没收到任何帧"判定假死并主动重连
 * 3. 鉴权失败不重连：4401/4403 说明 Token 失效或角色不对，重试一万次也没用，
 *    交回登录页才是正确动作
 */

import { API_WS_BASE } from "./constants";

// 退避参数：首次 1s，逐次翻倍，封顶 10s（避免长时间断网后重连风暴）
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 10000;
// 心跳间隔 25s：低于常见代理的 30s/60s 空闲超时，保证连接不会被中间设备掐断
const HEARTBEAT_MS = 25000;
// 假死判定：两个心跳周期都没收到任何帧（含 pong）就认为连接已死
const DEAD_AFTER_MS = HEARTBEAT_MS * 2.5;

// 与服务端约定的应用层关闭码（见 app/api/ws.py）
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_FORBIDDEN = 4403;

/**
 * 建立一条带自动重连的 WebSocket。
 *
 * @param path      连接路径，如 "/ws/chat"
 * @param token     登录 Token（浏览器不允许自定义请求头，只能走查询参数）
 * @param onEvent   收到服务端事件时回调（已 JSON 解析）
 * @param onOpen    连接建立时回调（重连成功也会触发）
 * @param onClose   连接断开时回调（重连中也会触发，用于展示"连接中"）
 * @param onAuthError 鉴权失败时回调：清登录态回登录页，且不再重连
 * @returns {{ send: (obj: object) => boolean, close: () => void, isOpen: () => boolean }}
 */
export function createSocket({ path, token, onEvent, onOpen, onClose, onAuthError }) {
  let ws = null;
  let retry = 0;
  let closedByUs = false;
  let reconnectTimer = null;
  let heartbeatTimer = null;
  let lastFrameAt = 0;

  function clearTimers() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    reconnectTimer = null;
    heartbeatTimer = null;
  }

  function connect() {
    reconnectTimer = null;
    // 拼 URL：token 走查询参数（WebSocket 构造函数无法设置 Authorization 头）
    ws = new WebSocket(`${API_WS_BASE}${path}?token=${encodeURIComponent(token)}`);
    lastFrameAt = Date.now();

    ws.onopen = () => {
      retry = 0;              // 连上即重置退避，否则断一次就永久慢重连
      onOpen?.();
      heartbeatTimer = setInterval(() => {
        // 假死检测：长时间没有任何帧（连 pong 都没有）→ 主动关掉触发重连
        if (Date.now() - lastFrameAt > DEAD_AFTER_MS) {
          ws?.close();
          return;
        }
        send({ type: "ping" });
      }, HEARTBEAT_MS);
    };

    ws.onmessage = (ev) => {
      lastFrameAt = Date.now();   // 任何帧都算"活着"
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;                   // 脏帧丢弃，不因此断开整条连接
      }
      onEvent?.(data);
    };

    ws.onclose = (ev) => {
      if (heartbeatTimer) clearInterval(heartbeatTimer);
      heartbeatTimer = null;
      onClose?.();
      if (closedByUs) return;
      if (ev.code === CLOSE_UNAUTHORIZED || ev.code === CLOSE_FORBIDDEN) {
        onAuthError?.();
        return;
      }
      // 指数退避重连：delay = min(base * 2^retry, max)
      const delay = Math.min(RECONNECT_BASE_MS * 2 ** retry, RECONNECT_MAX_MS);
      retry += 1;
      reconnectTimer = setTimeout(connect, delay);
    };

    // onerror 后必然紧跟 onclose：重连逻辑只在 onclose 里做，避免重复调度
    ws.onerror = () => {};
  }

  function send(obj) {
    if (ws?.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(obj));
    return true;
  }

  function close() {
    closedByUs = true;
    clearTimers();
    ws?.close();
    ws = null;
  }

  connect();

  return { send, close, isOpen: () => ws?.readyState === WebSocket.OPEN };
}
