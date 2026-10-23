"""API 路由层：/health 健康检查与坐席操作接口。

路由层只做参数校验和调用编排，不写业务逻辑。
"""

import logging

from fastapi import APIRouter, HTTPException, Path

from app.agents import handoff
from app.api.chat import ROLE_HUMAN, THREAD_ID_PATTERN
from app.api.deps import AgentUser
from app.api.schemas import (
    AgentActionRequest,
    AgentReplyRequest,
    AgentUserHistoryResponse,
    SessionMessage,
)
from app.core.config import get_settings
from app.core.hub import hub

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/health")
async def health():
    """健康检查：中间件连通性在这里汇总上报（后续步骤逐步补充检查项）。"""
    s = get_settings()
    return {
        "status": "ok",
        "llm_model": s.llm_model,
        "milvus": s.milvus_uri,
    }


# 坐席操作接口（模拟坐席端/管理后台的数据入口）
# 每个接口要求坐席账号登录（require_agent 依赖），坐席身份取自登录态，
# 不再信任请求体裸传的 agent_name，杜绝任意人冒充坐席抢单/回复/关闭会话
agent_router = APIRouter(prefix="/agent", tags=["agent"])

@agent_router.get("/threads")
async def list_threads(agent: AgentUser):
    """坐席端队列：按转接时间先后排序。

    接入即锁定：排队中的工单所有人可见（抢单），
    已接入（in_service）的只出现在接入者本人的队列里。
    """
    try:
        return {"threads": handoff.list_live_threads(agent_id=agent["id"])}
    except Exception:
        logger.exception("坐席端队列查询失败")
        raise HTTPException(status_code=503, detail="队列查询暂时不可用，请稍后重试") from None

@agent_router.post("/threads/{thread_id}/accept")
async def accept_thread(
    agent: AgentUser,  # 依赖参数无默认值，必须放在有默认值的 body 之前
    thread_id: str = Path(..., pattern=THREAD_ID_PATTERN),
    body: AgentActionRequest | None = None,
):
    """坐席接入排队会话：queued → in_service。

    409 而非 404：会话不存在/已被其他坐席接走/超时回退，
    对坐席端来说是同一个含义——"工单不在队列里了，请刷新"。
    """
    body = body or AgentActionRequest()
    # 坐席身份以登录态为准；接入时把账号 ID 锁进会话，后续回复/关闭都以此校验
    if not handoff.agent_accept(thread_id, agent["display_name"], agent["id"]):
        raise HTTPException(
            status_code=409,
            detail="该会话不在排队中（可能已被接入、超时回退或不存在）",
        )
    return {"thread_id": thread_id, "status": handoff.STATUS_IN_SERVICE}

@agent_router.get("/threads/{thread_id}/user_history", response_model=AgentUserHistoryResponse)
async def user_history(
    agent: AgentUser,  # 依赖参数无默认值，必须放在有默认值的 path 参数之前
    thread_id: str = Path(..., pattern=THREAD_ID_PATTERN),
) -> AgentUserHistoryResponse:
    """查该会话归属用户的全部历史消息（跨 thread 合并、时间序）。

    坐席接入前了解用户来龙去脉：同一用户以前问过什么、上一单怎么处理的，
    避免让用户重复叙述。仅要求坐席登录，不限定是否接入者——
    接入前也需要看历史来决定是否抢单。
    """
    owner_id = handoff.get_thread_user(thread_id)
    if owner_id is None:
        # 匿名会话（无归属）没有历史可查，返回空列表而非 404，
        # 前端统一渲染"暂无历史"，不用为匿名场景做特殊分支
        return AgentUserHistoryResponse(user_display_name=None, messages=[])
    try:
        rows = handoff.get_user_messages(owner_id)
        owner_name = handoff.get_user_display_name(owner_id)
    except Exception:
        logger.exception("用户历史查询失败 thread=%s", thread_id)
        raise HTTPException(status_code=503, detail="历史查询暂时不可用，请稍后重试") from None
    return AgentUserHistoryResponse(
        user_display_name=owner_name,
        messages=[SessionMessage(**m) for m in rows],
    )

@agent_router.post("/threads/{thread_id}/close")
async def close_thread(
    agent: AgentUser,  # 依赖参数无默认值，必须放在有默认值的 body 之前
    thread_id: str = Path(..., pattern=THREAD_ID_PATTERN),
    body: AgentActionRequest | None = None,
):
    """坐席结束处理：→ closed；用户下次发消息视为新问题回自助。

    服务中的会话只有接入者本人能关，其他坐席关别人的在办工单会被拒（409）。
    """
    body = body or AgentActionRequest()
    if not handoff.agent_close(thread_id, body.resolution or "处理完成", agent_id=agent["id"]):
        raise HTTPException(status_code=409, detail="该会话不处于待处理状态，或已由其他坐席接入")
    return {"thread_id": thread_id, "status": handoff.STATUS_CLOSED}

@agent_router.post("/threads/{thread_id}/reply")
async def reply_thread(
    agent: AgentUser,  # 依赖参数无默认值，必须放在有默认值的 body 之前
    thread_id: str = Path(..., pattern=THREAD_ID_PATTERN),
    body: AgentReplyRequest | None = None,
):
    """坐席回复消息：写入会话记录表，并通过 WebSocket 推给用户端。

    只允许接入者本人在处理中（in_service）状态下回复：
    - queued（未接入）不能回复：接入是占位动作（防多坐席抢单 + 状态推进），
      未接入就能回复会让接入形同虚设
    - 非本人的在办会话不能回复：接入即锁定，其他坐席只能看不能插手
    - active 会话没有人工介入的场景；closed 回复与新问题回自助语义冲突
    """
    if body is None:
        raise HTTPException(status_code=422, detail="回复内容不能为空")
    status = handoff.get_status(thread_id)
    if status != handoff.STATUS_IN_SERVICE:
        raise HTTPException(status_code=409, detail="请先接入该会话再回复")
    # 接入即锁定：不是接入者本人的话，即使知道 thread_id 也不能回复。
    # 403 而非 409：会话存在且在服务中，是"身份不对"而非"状态不对"
    if handoff.get_thread_agent_id(thread_id) != agent["id"]:
        raise HTTPException(status_code=403, detail="该会话已由其他坐席接入")
    try:
        # 坐席昵称随消息落库：用户端气泡显示"是谁回复的"
        saved = handoff.append_message(
            thread_id, ROLE_HUMAN, body.content, sender_id=agent["id"]
        )
    except Exception:
        logger.exception("坐席回复落库失败 thread=%s", thread_id)
        raise HTTPException(status_code=503, detail="回复发送失败，请重试") from None
    if saved is None:
        raise HTTPException(status_code=503, detail="回复发送失败，请重试")

    # 推送在落库之后：推出去的消息必须已经持久化，否则用户端看到的内容
    # 刷新后会消失（"我明明收到回复了"）。
    # id 一并下发：消息会经「REST 响应体」和「WS 推送」两条路到前端，
    # 前端靠这个主键去重——否则发送方自己会看到两条（乐观插入 + 被推送回来）
    message = {
        "id": saved["id"],
        "role": ROLE_HUMAN,
        "content": body.content,
        "ts": saved["ts"],
        # 坐席昵称随事件下发：用户端气泡要显示"是谁在回复我"
        "sender_name": agent["display_name"],
    }
    hub.push_thread(thread_id, {"type": "message", "message": message})
    # 其他正在看该会话的坐席也要看到（同一坐席的多标签页属边界情况，见 step-39 遗留项）
    hub.push_agents(
        {"type": "message", "thread_id": thread_id, "message": message},
        thread_id=thread_id,
    )
    # 响应体回传权威消息（含服务端 id 与 created_at）：
    # 前端不再自己拼时间戳，避免本地时钟与服务端时间轴倒挂
    return {"thread_id": thread_id, "status": status, "message": message}
