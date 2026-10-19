"""Chat 请求/响应模型与业务异常定义。"""

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    """POST /chat 的请求体。

    thread_id 可选：不传则新建会话（服务端生成并返回），
    前端首轮拿到后原样带上即可延续多轮对话。
    """

    message: str = Field(..., min_length=1, max_length=2000, description="用户消息")
    thread_id: str | None = Field(default=None, description="会话 ID，首轮不传")

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        """去首尾空白后拒绝纯空白消息：纯空格也能通过 min_length，会白白消耗一次 LLM 调用。"""
        v = v.strip()
        if not v:
            raise ValueError("消息不能为空白")
        return v


class ChatResponse(BaseModel):
    message: str                # Agent 回复
    thread_id: str              # 会话 ID（多轮对话凭此延续）
    transferred: bool           # 本轮是否发生了人工转接（前端据此切换 UI 状态）


class SessionMessage(BaseModel):
    role: str                   # user / agent / human
    content: str
    ts: str | None = None
    sender_name: str | None = None   # human 消息的坐席昵称（用户端展示用）


class SessionHistoryResponse(BaseModel):
    thread_id: str
    status: str                 # active / queued / in_service / closed
    messages: list[SessionMessage]

class SessionSummary(BaseModel):
    """会话列表项：用户端"历史会话"侧栏用。"""

    thread_id: str
    status: str
    updated_at: str
    last_message: str | None    # 最后一条消息预览
    last_message_at: str | None
    last_role: str | None

class SessionListResponse(BaseModel):
    threads: list[SessionSummary]

class AgentActionRequest(BaseModel):
    """坐席操作请求：接入排队会话 / 结束人工处理。"""

    agent_name: str = Field(default="agent", max_length=64, description="坐席标识")
    resolution: str = Field(default="", max_length=500, description="处理结果（close 时使用）")

class AgentReplyRequest(BaseModel):
    """坐席回复消息：写入会话记录表，用户端轮询拉取展示。"""

    content: str = Field(..., min_length=1, max_length=2000, description="回复内容")

    @field_validator("content")
    @classmethod
    def strip_content(cls, v: str) -> str:
        """去首尾空白后拒绝纯空白：与用户消息同一口径，避免空消息进记录表。"""
        v = v.strip()
        if not v:
            raise ValueError("回复内容不能为空白")
        return v
