"""沙箱结果解析与配置单测（不真正启动容器）。"""

import json

from backend.core.sandbox import SandboxRunner


def test_collect_parses_summary_and_coverage(tmp_path):
    (tmp_path / ".codepilot_coverage.json").write_text(
        json.dumps({"totals": {"percent_covered": 81.25}}), encoding="utf-8"
    )
    runner = SandboxRunner()
    out = runner._collect(tmp_path, "3 passed, 1 failed in 0.42s", 1)
    assert out.passed == 3 and out.failed == 1
    assert out.status == "failed"
    assert out.coverage == 81.25


def test_collect_all_passed():
    runner = SandboxRunner()
    out = runner._collect(__import__("pathlib").Path("/tmp"), "5 passed in 0.2s", 0)
    assert out.status == "passed" and out.failed == 0


def test_sandbox_off_skips(monkeypatch):
    from backend.config import get_settings

    runner = SandboxRunner()
    monkeypatch.setattr(runner.s, "sandbox_mode", "off")
    import asyncio

    result = asyncio.run(runner.run(None, {"tests/test_a.py": "def test_a(): assert 1"}))
    assert result.status == "skipped"


def test_llm_enabled_flags(monkeypatch):
    from backend.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "mock")
    assert s.llm_enabled is False
    monkeypatch.setattr(s, "llm_provider", "deepseek")
    monkeypatch.setattr(s, "llm_api_key", "sk-x")
    assert s.llm_enabled is True
