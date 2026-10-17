"""输入防护（guardrails）：提示注入检测与敏感词拦截。

设计定位——纵深防御的第一道闸：
1. 本模块做规则级前置拦截，零 LLM 成本、毫秒级响应，挡住明显的恶意输入
2. 挡不住的由系统提示词兜底（graph.py 行为准则 6/7：拒答 + 引导）
3. 规则必然有漏报/误报，所以只拦"高置信度"模式；
   拦截结果以 200 友好话术返回而非报错，不给攻击者探测信号

为什么不放 graph 里：注入/敏感词与 Agent 决策无关，在 API 层拦截
可以在进入 LangGraph 前短路，省掉 Checkpointer 读写和状态污染。
"""

import logging
import re

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 提示注入的高置信度模式（正则，忽略大小写）。
# 只收录"出现即恶意"的表达，避免误伤正常咨询（如用户真的在问"系统提示音怎么关"）
_INJECTION_PATTERNS = [
    # 中文：忽略/无视既往指令类
    r"忽略(之前|上面|以上|前面|先前|所有|全部)?的?(指令|提示|设定|规则)",
    r"(无视|不要遵守)(你的|之前|上述)?(指令|规则|设定)",
    # 英文经典注入句式
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions",
    # 试图套取/扮演系统角色
    r"(system|developer)\s*prompt",
    r"你现在是?一?个?(没有限制|不受(任何)?(限制|约束)|无道德|没有规则)",
    r"(扮演|假装|角色扮演).{0,12}(DAN|开发者模式|越狱)",
]

_INJECTION_RES = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]

# 违禁词默认表：电商客服场景下"出现即应拒绝服务"的最小集合。
# 业务可通过 BLOCKED_WORDS 环境变量追加（逗号分隔），不动代码
_DEFAULT_BLOCKED_WORDS = ["枪支", "毒品", "爆炸物", "代开发票", "刷单兼职", "博彩"]


def _blocked_words() -> list[str]:
    extra = get_settings().blocked_words
    return _DEFAULT_BLOCKED_WORDS + [w.strip() for w in extra.split(",") if w.strip()]


def check_message(text: str) -> str | None:
    """输入安检：返回违规类别，正常内容返回 None。

    类别：'injection'（提示注入）/ 'sensitive'（违禁词）。
    顺序：先查注入再查违禁——注入是更明确的恶意信号，日志里优先暴露。
    """
    for pattern in _INJECTION_RES:
        if pattern.search(text):
            return "injection"

    for word in _blocked_words():
        if word in text:
            return "sensitive"

    return None

def refusal_reply(category: str) -> str:
    """按违规类别生成友好拒答话术。

    话术原则：不说"检测到攻击"等对抗性措辞（避免教唆绕过），
    给用户明确的出路——回到正常咨询。
    """
    if category == "injection":
        return "为了对话安全，我无法处理包含特殊指令的内容。请直接描述您遇到的问题，我很乐意帮您解答～"
    return "您的消息包含我无法协助的内容。如有购物、售后方面的问题，欢迎随时告诉我～"

def log_rejection(thread_id: str, category: str, text: str) -> None:
    """拦截记录：安全事件必须留痕（脱敏截断，不落全文）。"""
    logger.warning(
        "输入拦截 category=%s thread=%s text=%r",
        category,
        thread_id,
        text[:80],
    )
