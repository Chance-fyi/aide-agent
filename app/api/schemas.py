"""Chat 请求/响应模型与业务异常定义。"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """POST /chat 的请求体。

    thread_id 可选：不传则新建会话（服务端生成并返回），
    前端首轮拿到后原样带上即可延续多轮对话。
    """

    message: str = Field(..., min_length=1, max_length=2000, description="用户消息")
    thread_id: str | None = Field(default=None, description="会话 ID，首轮不传")


class ChatResponse(BaseModel):
    message: str                # Agent 回复
    thread_id: str              # 会话 ID（多轮对话凭此延续）
    transferred: bool           # 本轮是否发生了人工转接（前端据此切换 UI 状态）


class SessionMessage(BaseModel):
    role: str                   # user / assistant
    content: str
    ts: str | None = None


class SessionHistoryResponse(BaseModel):
    thread_id: str
    status: str                 # active / pending_human
    messages: list[SessionMessage]
