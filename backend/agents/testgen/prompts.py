# backend/agents/testgen/prompts.py
# 单测生成提示词（对应课件 6.7 代码题评估里的「生成可运行用例」）。

TESTGEN_SYSTEM = """你是 Python 测试工程师。为给定函数生成 pytest 单测。
要求：
1. 只测试 diff 中新增/修改的函数，不要臆造不存在的接口。
2. 覆盖：正常路径、边界条件、异常路径；用 monkeypatch/mocker 隔离外部依赖（数据库、HTTP）。
3. 不访问网络、不写文件、不依赖真实时间。
4. 输出完整可运行文件，import 路径按仓库结构修正。
5. 只输出代码，不要解释，不要 Markdown 围栏。"""


def build_testgen_prompt(symbols: list[dict], diff_text: str) -> str:
    lines = "\n".join(f"- {s['path']}::{s['symbol']} ({s['kind']})" for s in symbols)
    return f"""## 目标符号
{lines}

## diff
```
{diff_text}
```

请生成 `tests/test_generated.py` 的完整内容。"""
