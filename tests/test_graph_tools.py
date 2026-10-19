"""search_kb 工具异常路径测试（不依赖真实 Milvus / LLM）。

通过 monkeypatch 替换 store.search，模拟三种情况：
- 知识库故障（抛异常）→ 降级提示，不能把异常抛出拖垮对话
- 检索无命中 → 引导转人工的提示语
- 正常命中 → 拼接带来源的上下文
"""

from app.agents.graph import search_kb
from app.rag import store


def test_search_kb_degrades_on_store_failure(monkeypatch):
    """Milvus 挂掉时 search_kb 必须降级为提示语，而不是把异常抛给 ToolNode。"""

    def boom(*args, **kwargs):
        raise RuntimeError("知识库集合不存在")

    monkeypatch.setattr(store, "search", boom)
    result = search_kb("怎么退货")
    assert "系统提示" in result
    assert "转接人工" in result or "人工客服" in result

def test_search_kb_no_hits_guides_transfer(monkeypatch):
    """无命中时应返回引导转人工的提示，防止 LLM 编造。"""
    monkeypatch.setattr(store, "search", lambda *a, **kw: [])
    result = search_kb("外星人购物政策")
    assert "没有" in result
    assert "transfer_to_human" in result

def test_search_kb_normal_hits_include_source(monkeypatch):
    """正常命中：内容拼接进上下文，且带来源标记方便 LLM 声明依据。"""
    fake_hits = [
        {
            "score": 0.82,
            "content": "自签收之日起 7 天内可申请无理由退货。",
            "parent_content": "## 退货条件\n\n自签收之日起 7 天内可申请无理由退货。定制类商品除外。",
            "parent_id": 1,
            "source": "returns.md",
            "h1": "退货政策",
            "h2": "退货条件",
        }
    ]
    monkeypatch.setattr(store, "search", lambda *a, **kw: fake_hits)
    result = search_kb("退货")
    assert "returns.md" in result
    assert "无理由退货" in result
    # 父子检索：上下文应取父块（完整小节）而非子块碎片
    assert "定制类商品除外" in result
