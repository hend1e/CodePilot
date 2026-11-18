# backend/agents/testgen/state.py
# 单测生成子图的 State。字段与 Review 子图保持同名，便于复用节点。

from typing import TypedDict

TEST_PATH = "tests/test_generated.py"


class TestGenState(TypedDict, total=False):
    task_id: str
    tenant_id: str
    repo: dict
    diff_text: str
    test_patch: str | None
    test_targets: list[str]
    testgen_rounds: int
    sandbox_report: dict | None
    hitl: dict | None
    steps: int
    degraded: bool
    open_questions: list[str]
