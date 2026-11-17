# scripts/eval_review.py
# 审查效果评测：用人工标注的 diff 跑一遍，算 precision / recall / 误报率。
# 为什么必须有这个脚本：简历上的「召回 79%、误报 12%」只有在自己数据集上跑出来才站得住。
#
# 标注格式（JSON）：
# [
#   {"name": "case01", "repo": "local/demo", "diff_path": "eval/case01.diff",
#    "expected": [{"file_path": "a.py", "line": 3, "rule_id": "SEC001"}]}
# ]
# 用法：LLM_PROVIDER=mock python -m scripts.eval_review --cases eval/cases.json

import argparse
import asyncio
import json
import os
from pathlib import Path

from backend.agents.review.diff import parse_diff
from backend.agents.review.rules import run_rules
from backend.agents.review.state import Finding, SEVERITY_ORDER


def _key(f: dict) -> tuple:
    return (f.get("file_path"), int(f.get("line_start") or f.get("line") or 0),
            f.get("rule_id") or "")


def match(expected: list[dict], actual: list[Finding], line_tolerance: int = 2) -> tuple[int, int]:
    """按 (文件, 行号±容差, rule_id) 匹配。返回 (命中数, 误报数)。"""
    used: set[int] = set()
    hits = 0
    for e in expected:
        for i, a in enumerate(actual):
            if i in used or a.file_path != e["file_path"]:
                continue
            if abs(a.line_start - int(e.get("line") or e.get("line_start") or 0)) > line_tolerance:
                continue
            if e.get("rule_id") and a.rule_id and e["rule_id"] != a.rule_id:
                continue
            used.add(i)
            hits += 1
            break
    return hits, len(actual) - len(used)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    tp = fp = fn = 0
    per_case = []

    for case in cases:
        diff_text = Path(case["diff_path"]).read_text(encoding="utf-8")
        bundle = parse_diff(diff_text)
        findings = run_rules(bundle)
        hits, false_pos = match(case.get("expected", []), findings)
        expected_n = len(case.get("expected", []))
        tp += hits
        fp += false_pos
        fn += expected_n - hits
        per_case.append({"name": case.get("name"), "expected": expected_n,
                         "hits": hits, "false_positive": false_pos,
                         "findings": len(findings)})

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    summary = {
        "cases": len(cases), "tp": tp, "fp": fp, "fn": fn,
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "note": "指标口径：按 (文件, 行号±2, rule_id) 匹配；仅统计规则引擎，LLM 需开启后另跑",
        "per_case": per_case,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
