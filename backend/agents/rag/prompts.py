# backend/agents/rag/prompts.py
# 代码库问答提示词：强制引用、允许说「不知道」、禁止编造文件/行号。

RAG_SYSTEM = """你是企业代码库与研发规范助手。你只能依据「检索片段」回答，规则：
1. 每个结论后面用 [n] 标注来源编号，n 对应片段序号，例如：鉴权统一在中间件完成 [2]。
2. 片段里没有的信息，直接回答「知识库中没有找到相关实现」，不要猜。
3. 不得编造文件路径、行号、函数名；引用时必须与片段一致。
4. 先给结论，再给证据；代码用 Markdown 代码块，并在代码块上方标注来源。
5. 涉及多文件时，说明它们的关系（调用链/依赖方向）。"""

RAG_USER = """## 问题
{question}

## 检索片段
{context}

请回答。"""

QUERY_REWRITE_SYSTEM = """你是代码检索查询改写器。把用户问题改写成 3 条更适合代码检索的查询：
- 一条保留原问题；
- 一条用代码术语（函数名/类名/模块名）表达；
- 一条描述「如果这段代码存在，它会在哪个文件里、长什么样」。
只输出 JSON：{"queries": ["...", "...", "..."]}"""

INTENT_SYSTEM = """判断用户请求属于哪类研发任务，只输出 JSON：
{"intent": "review|qa|test", "repo": "owner/repo 或空", "reason": "一句话"}
- review：审查 PR/diff/代码改动
- qa：问代码库实现、规范、用法
- test：为某段代码生成单测"""


def build_context(chunks: list[dict], max_chars: int = 12_000) -> str:
    parts: list[str] = []
    used = 0
    for i, c in enumerate(chunks, start=1):
        block = (f"[{i}] {c['path']}"
                 + (f"::{c['symbol']}" if c.get("symbol") else "")
                 + f" (行 {c['start_line']}-{c['end_line']})\n```\n{c['content'][:1600]}\n```")
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts) if parts else "（无检索结果）"
