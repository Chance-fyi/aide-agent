"""LLM 客户端封装。

要点：
- 用 langchain-OpenAI 的 ChatOpenAI，它兼容任何 OpenAI 格式接口
  （DeepSeek / 硅基流动 / 通义 / Ollama……），切换厂商只改 .env
- 统一从这里拿客户端，后续 Agent / 接口层不直接接触连接细节
"""

from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.core.config import get_settings


@lru_cache
def get_llm() -> ChatOpenAI:
    """创建 LLM 客户端（进程内单例）。

    temperature=0.1：客服场景要"稳"不要"飘"，
    回答越确定越好，temperature 调低能减少胡说八道。
    """
    s = get_settings()
    return ChatOpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.llm_model,
        temperature=0.1,
        # 网络超时保护：LLM 卡死时不能拖住整个请求链路（步骤 3 只设参数，
        # 重试与降级在异常处理迭代里完善）
        timeout=30,
        max_retries=2,
    )
