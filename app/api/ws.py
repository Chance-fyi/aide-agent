"""WebSocket 端点：用户端 /ws/chat、坐席端 /ws/agent。

与 REST 的分工：
- **收**（服务端主动推）走 WebSocket：坐席回复、转接状态、队列变化
- **发**（客户端主动做）两条路都有：
  - 用户发消息：走 WS（`{"type":"message"}`），回复由服务端推送回来
  - 坐席操作（接入/结束/回复）：仍走 REST（有完整的状态机校验与 409/403 语义，
    没必要为"实时"把权限逻辑搬一遍）
- 历史/队列快照：连接建立时由 ready 事件给全量，断线重连即自愈

鉴权：浏览器 WebSocket API 不允许自定义请求头，Authorization 传不进去，
因此 Token 走查询参数。注意 uvicorn 访问日志会记录 URL，Token 可能落进日志——
与"Token 有效期 7 天"叠加是可接受的风险；更严格的做法是握手后第一帧再发
Token（本步不引入额外握手，见 docs/step-39.md 遗留项）。
"""

import asyncio
import json
import logging
import re

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app.agents import handoff
from app.api import chat
from app.api.chat import THREAD_ID_PATTERN
from app.api.schemas import ChatRequest
from app.core import ratelimit
from app.core.config import get_settings
from app.core.hub import Connection, hub
from app.core.security import ROLE_AGENT, decode_token
from app.core.users import get_user_by_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])

# 应用层关闭码（4000-4999 为私有区间）：
# 前端据此区分"该重新登录"和"网络抖动，该重连"
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403

def _authenticate(token: str) -> dict | None:
    """Token → 用户；验签失败/账号已删都按未认证处理（与 REST 同一套原语）。"""
    payload = decode_token(token)
    if payload is None:
        return None
    return get_user_by_id(payload["sub"])

def _origin_allowed(ws: WebSocket) -> bool:
    """校验握手来源。

    WebSocket 不受同源策略约束：任意网页都能连上我们的 WS 并冒充已登录用户
    收消息（Token 在 localStorage 里，第三方页面拿不到，但 CSWSH 类攻击靠的是
    "浏览器自动带上 Cookie/凭据"，这里没有 Cookie 也一并挡掉更稳）。
    非浏览器客户端（脚本/测试）不带 Origin，放行——它们没有"被第三方页面驱动"的风险。
    """
    origin = ws.headers.get("origin")
    if origin is None:
        return True
    return origin in get_settings().cors_origin_list

async def _reject(ws: WebSocket, message: str, code: int) -> None:
    """拒绝连接：先告知原因再关闭。

    为什么必须先 accept 再 close：ASGI 里未 accept 就 close 会被服务器
    转成 HTTP 403，前端拿不到任何原因（只会看到"连接失败"）。
    accept 后发一条 error 事件再带自定义关闭码，前端才能决定
    "重新登录"还是"稍后重连"。
    """
    try:
        await ws.send_json({"type": "error", "code": code, "message": message})
        await ws.close(code=code)
    except Exception:
        logger.debug("拒绝连接时发送失败（对端已断开）", exc_info=True)

async def _receive_json(ws: WebSocket) -> dict | None:
    """读一帧 JSON；非法帧返回 None（不因一条脏数据断开整条连接）。"""
    try:
        data = await ws.receive_json()
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None

# ============================================================
# 用户端
# ============================================================

@router.websocket("/ws/chat")
async def chat_socket(ws: WebSocket) -> None:
    """用户端实时通道。

    客户端 → 服务端：{"type":"message","content":"..."} / {"type":"ping"}
    服务端 → 客户端：
      - ready   ：连接建立，带历史消息 + 当前状态（替代首屏 GET /chat/my/history）
      - message ：新消息（智能客服回复 / 人工坐席回复 / 排队确认话术）
      - status  ：转接状态变化（排队中 → 服务中 → 已结束/回退）
      - error   ：友好错误文案（校验失败 / LLM 故障）
      - pong    ：心跳应答
    """
    await ws.accept()
    if not _origin_allowed(ws):
        await _reject(ws, "来源不被允许", CLOSE_FORBIDDEN)
        return
    user = _authenticate(ws.query_params.get("token", ""))
    if user is None:
        await _reject(ws, "登录已失效，请重新登录", CLOSE_UNAUTHORIZED)
        return

    conn = Connection(ws=ws, user_id=user["id"])
    await hub.add(conn)
    # 匿名限流键用握手时的客户端 IP（与 REST 的 _client_ip 同一口径，
    # 但 WS 拿不到 X-Forwarded-For 之外的 request 对象，这里直接用 scope 的 client）
    client_ip = ws.client.host if ws.client else None
    try:
        # 首帧快照：客户端拿到即完成"恢复历史 + 恢复转接状态"，无需再发 HTTP 请求。
        # 查库是同步 psycopg，扔到线程池，避免阻塞事件循环里的其他连接
        try:
            snapshot = await asyncio.to_thread(chat.build_user_snapshot, user["id"])
        except Exception:
            # 快照失败也要让连接活着：先给空快照，用户继续发消息即可
            logger.exception("用户快照加载失败 user=%s", user["id"])
            snapshot = {"thread_id": "", "status": handoff.STATUS_ACTIVE, "messages": []}
        hub.send(conn, {"type": "ready", **snapshot})

        while True:
            data = await _receive_json(ws)
            if data is None:
                hub.send(conn, {"type": "error", "message": "消息格式不正确"})
                continue
            kind = data.get("type")
            if kind == "ping":
                hub.send(conn, {"type": "pong"})
            elif kind == "message":
                await _handle_user_message(conn, user, data.get("content"), client_ip)
            else:
                hub.send(conn, {"type": "error", "message": f"不支持的事件类型: {kind}"})
    except WebSocketDisconnect:
        pass   # 正常断开（关页面/切网络），不是异常
    except Exception:
        logger.exception("用户端 WS 异常退出 user=%s", user["id"])
    finally:
        hub.remove(conn)

async def _handle_user_message(
    conn: Connection, user: dict, content, client_ip: str | None = None,
) -> None:
    """处理一条用户消息：复用 REST 的同一套业务编排（process_message）。"""
    try:
        # 复用 ChatRequest 做校验（去空白 / 长度上限），与 REST 口径完全一致
        req = ChatRequest(message=content if isinstance(content, str) else "")
    except ValidationError:
        hub.send(conn, {"type": "error", "message": "消息内容不合法（不能为空，最多 2000 字）"})
        return

    try:
        # push_reply=True：机器人的回复走推送，函数返回值这里用不上
        outcome = await chat.process_message(
            req.message, user, push_reply=True, client_ip=client_ip
        )
    except ratelimit.RateLimitExceeded as exc:
        # 限流/繁忙也要回一条明确提示：WS 没有 HTTP 状态码，
        # 不告知的话用户只会看到消息“消失”
        hub.send(conn, {
            "type": "error",
            "message": f"发送太频繁，请 {exc.retry_after} 秒后再试",
        })
        return
    except ratelimit.BusyError:
        hub.send(conn, {"type": "error", "message": "当前咨询人数较多，请稍后重试"})
        return
    except Exception as exc:
        # process_message 内部已把 LLM 异常翻译成 HTTPException（detail 即友好文案）
        detail = getattr(exc, "detail", None)
        if detail is None:
            logger.exception("WS 对话处理失败 user=%s", user["id"])
            detail = "客服服务暂时不可用，请稍后重试"
        hub.send(conn, {"type": "error", "message": detail})
        return

    # 发送确认：无论本轮有没有机器人回复，都要告诉客户端"消息已被接收"。
    # 为什么单独一个事件而不是靠"回复"去推断：已转人工（尤其坐席已接入）时
    # 根本不会有人回复，客户端若等回复来清"发送中"状态，输入框就会永久锁死——
    # 这正是"转人工后只能发一条消息"的根因。
    # status 一并下发：客户端据此同步转接 UI（无需再单独等 status 事件）
    hub.send(conn, {
        "type": "sent",
        "thread_id": outcome.thread_id,
        "status": outcome.status,
    })

# ============================================================
# 坐席端
# ============================================================

@router.websocket("/ws/agent")
async def agent_socket(ws: WebSocket) -> None:
    """坐席端实时通道。

    客户端 → 服务端：{"type":"watch","thread_id":"..."} / {"type":"ping"}
    服务端 → 客户端：
      - ready        ：连接建立，带当前队列快照
      - queue_changed：队列有变化（新转接/被接入/被结束/超时回退），客户端重新拉队列
      - message      ：正在查看的会话有新消息
      - status       ：正在查看的会话状态变化
      - pong         ：心跳应答

    为什么队列用"信号 + 重拉"而不是直接推列表：队列是**按坐席过滤**的
    （in_service 只对接入者可见），推全量列表得给每个连接算一份；
    推信号让客户端用同一个 GET /agent/threads 取自己那份，口径只有一处。
    """
    await ws.accept()
    if not _origin_allowed(ws):
        await _reject(ws, "来源不被允许", CLOSE_FORBIDDEN)
        return
    user = _authenticate(ws.query_params.get("token", ""))
    if user is None or user["role"] != ROLE_AGENT:
        await _reject(ws, "需要坐席账号登录", CLOSE_UNAUTHORIZED)
        return

    conn = Connection(ws=ws, agent_id=user["id"])
    await hub.add(conn)
    try:
        try:
            threads = await asyncio.to_thread(handoff.list_live_threads, user["id"])
        except Exception:
            logger.exception("坐席队列快照加载失败 agent=%s", user["id"])
            threads = []
        hub.send(conn, {"type": "ready", "threads": threads})

        while True:
            data = await _receive_json(ws)
            if data is None:
                hub.send(conn, {"type": "error", "message": "消息格式不正确"})
                continue
            kind = data.get("type")
            if kind == "ping":
                hub.send(conn, {"type": "pong"})
            elif kind == "watch":
                thread_id = data.get("thread_id")
                # 与 REST 同一套 ID 约束：脏 ID 不进内存里的订阅表
                if isinstance(thread_id, str) and re.match(THREAD_ID_PATTERN, thread_id):
                    hub.watch(conn, thread_id)
                else:
                    hub.send(conn, {"type": "error", "message": "thread_id 不合法"})
            else:
                hub.send(conn, {"type": "error", "message": f"不支持的事件类型: {kind}"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("坐席端 WS 异常退出 agent=%s", user["id"])
    finally:
        hub.remove(conn)
