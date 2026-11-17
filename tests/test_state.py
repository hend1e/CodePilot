"""State reducer / 聚合口径单测：并行写入必须幂等、去重、排序稳定。"""

from backend.agents.review.state import Finding, merge_findings, merge_status, summarize


def _f(**kw) -> Finding:
    base = dict(dimension="style", severity="info", file_path="a.py", line_start=1,
                evidence="x = 1", message="m", confidence=0.5)
    base.update(kw)
    return Finding(**base)


def test_dedupe_keeps_higher_confidence():
    a = _f(confidence=0.4, severity="warn")
    b = _f(confidence=0.9, severity="warn")
    merged = merge_findings([a], [b])
    assert len(merged) == 1 and merged[0].confidence == 0.9


def test_merge_is_idempotent_and_ordered():
    a = _f(severity="info", line_start=2)
    b = _f(severity="block", line_start=3, rule_id="SEC001")
    first = merge_findings([a], [b])
    second = merge_findings(first, [a, b])
    assert first == second
    assert [f.severity for f in first] == ["block", "info"]


def test_merge_findings_does_not_mutate_input():
    a = [_f()]
    out = merge_findings(a, [_f(rule_id="X")])
    assert len(a) == 1 and len(out) == 2


def test_merge_status_overrides():
    assert merge_status({"security": "ok", "style": "ok"}, {"style": "failed"}) == {
        "security": "ok", "style": "failed"}


def test_summary_gate():
    findings = [_f(severity="block", rule_id="SEC001"), _f(severity="warn", dimension="performance")]
    s = summarize(findings)
    assert s["total"] == 2 and s["mergeable"] is False
    assert s["by_severity"] == {"block": 1, "warn": 1, "info": 0}
    assert summarize([_f()])["mergeable"] is True
