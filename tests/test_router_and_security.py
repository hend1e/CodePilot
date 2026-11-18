"""路由 / 混合检索纯函数 / GitHub webhook 解析 / 安全工具 的单测。"""

import hashlib
import hmac

import pytest

from backend.agents.orchestrator.router import rule_route
from backend.agents.rag.retrieval import _lexical_overlap, _rrf
from backend.core.security import safe_repo_path, verify_github_signature
from backend.infra.github import parse_webhook


def test_rule_route_pr_url():
    r = rule_route("帮我审查 https://github.com/acme/pay/pull/128 这个 PR")
    assert r.intent == "review" and r.repo == "acme/pay" and r.pr_number == 128


def test_rule_route_stacktrace_falls_through():
    """堆栈不归属任何子图（本项目没有缺陷定位 Agent），交给 LLM/默认问答。"""
    assert rule_route('Traceback (most recent call last):\n  File "a.py", line 3, in <module>') is None


def test_rule_route_test_and_qa():
    assert rule_route("给 parse_diff 生成单测").intent == "test"
    assert rule_route("我们的鉴权是怎么做的？") is None        # 未命中规则 → 交给 LLM/默认


def test_rrf_monotonic():
    assert _rrf(1) > _rrf(2) > _rrf(30) > 0


def test_lexical_overlap():
    assert _lexical_overlap({"auth", "middleware"}, "class AuthMiddleware: ...") > 0
    assert _lexical_overlap({"zzz"}, "abc") == 0.0


def test_webhook_parse_filters_actions():
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/pay", "clone_url": "https://x"},
        "pull_request": {"number": 7, "title": "feat", "user": {"login": "dev"},
                         "base": {"sha": "b1"}, "head": {"sha": "h2"}, "html_url": "u"},
    }
    parsed = parse_webhook(payload)
    assert parsed["repo_full_name"] == "acme/pay"
    assert parsed["idempotency_key"] == "acme/pay#7@h2"[:len("acme/pay#7@h2")]
    assert parse_webhook({**payload, "action": "closed"}) is None
    assert parse_webhook({"action": "opened"}) is None


def test_safe_repo_path(tmp_path, monkeypatch):
    from backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "repo_local_roots", str(tmp_path))
    target = tmp_path / "repo"
    target.mkdir()
    assert safe_repo_path(str(target)) == target.resolve()
    with pytest.raises(Exception):
        safe_repo_path("/etc")


def test_github_signature(monkeypatch):
    from backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "app_env", "prod")
    body = b'{"hello":"world"}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, sig) is True
    assert verify_github_signature(body, "sha256=deadbeef") is False
    assert verify_github_signature(body, None) is False
