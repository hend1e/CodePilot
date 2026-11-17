# backend/infra/github.py
# GitHub 接入：webhook 解析、拉取 PR diff、回写评论。
# 原则：网络失败不影响主流程（审查结果已落库，评论只是「通知」）。

import httpx

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _headers() -> dict:
    h = dict(HEADERS)
    token = get_settings().github_token
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def parse_webhook(payload: dict) -> dict | None:
    """从 pull_request 事件里抽出审查所需的最小信息。非目标事件返回 None。"""
    pr = payload.get("pull_request")
    repo = payload.get("repository")
    if not pr or not repo:
        return None
    if payload.get("action") not in ("opened", "synchronize", "reopened", "ready_for_review"):
        return None
    return {
        "provider": "github",
        "repo_full_name": repo["full_name"],
        "clone_url": repo.get("clone_url"),
        "local_path": None,
        "pr_number": pr["number"],
        "title": pr.get("title", ""),
        "author": (pr.get("user") or {}).get("login", ""),
        "base_sha": (pr.get("base") or {}).get("sha"),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "html_url": pr.get("html_url"),
        "idempotency_key": f"{repo['full_name']}#{pr['number']}@{((pr.get('head') or {}).get('sha') or '')[:12]}",
    }


async def fetch_pr_diff(repo_full_name: str, number: int) -> str:
    """拉取 PR unified diff。GitHub 的 .diff 端点返回纯文本。"""
    url = f"{get_settings().github_api_base}/repos/{repo_full_name}/pulls/{number}"
    async with httpx.AsyncClient(timeout=30, headers=_headers(), follow_redirects=True) as client:
        resp = await client.get(url, headers={"Accept": "application/vnd.github.v3.diff"})
        if resp.status_code >= 400:
            logger.warning("github.diff_failed", repo=repo_full_name, pr=number,
                           status=resp.status_code)
            raise httpx.HTTPStatusError(
                f"fetch diff failed: {resp.status_code}", request=resp.request, response=resp
            )
        return resp.text


def render_comment(final: dict) -> str:
    """把审查报告渲染成 Markdown 评论。"""
    s = final.get("summary", {})
    sev = s.get("by_severity", {})
    lines = [
        "## 🤖 CodePilot 审查报告",
        "",
        f"**结论**：{'❌ 不可合并（存在阻断问题）' if not s.get('mergeable', True) else '✅ 可合并（无阻断问题）'}",
        f"**统计**：阻断 {sev.get('block', 0)} · 警告 {sev.get('warn', 0)} · 建议 {sev.get('info', 0)}"
        f" · 降级 {'是' if final.get('degraded') else '否'}",
        f"**成本**：{final.get('cost_usd', 0):.4f} USD · 步数 {final.get('steps', 0)}",
        "",
    ]
    groups: dict[str, list[dict]] = {"block": [], "warn": [], "info": []}
    for f in final.get("findings", []):
        groups[f["severity"]].append(f)

    titles = {"block": "### ⛔ 阻断", "warn": "### ⚠️ 警告", "info": "### 💡 建议"}
    for sev_key in ("block", "warn", "info"):
        items = groups[sev_key]
        if not items:
            continue
        lines.append(titles[sev_key])
        for f in items[:20]:
            lines.append(
                f"- `{f['file_path']}:{f['line_start']}` [{f['dimension']}] {f['message']} "
                f"（{f.get('source', 'rule')}，置信度 {f.get('confidence', 0):.2f}）"
            )
            if f.get("suggestion"):
                lines.append(f"  - 建议：{f['suggestion']}")
        lines.append("")

    tests = final.get("test_report")
    if tests:
        cov = tests.get("coverage")
        cov_text = f"，覆盖率 {cov}%" if cov is not None else ""
        lines.append(f"### 🧪 单测沙箱：{tests.get('status')} "
                     f"（{tests.get('passed', 0)} passed / {tests.get('failed', 0)} failed{cov_text}）")
        lines.append("")

    lines.append(f"<sub>trace: `{final.get('thread_id', '')}`</sub>")
    return "\n".join(lines)


async def post_pr_comment(repo_full_name: str, number: int, body: str) -> bool:
    if not get_settings().github_comment_enabled:
        logger.info("github.comment_skipped", reason="disabled", pr=number)
        return False
    url = f"{get_settings().github_api_base}/repos/{repo_full_name}/issues/{number}/comments"
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        resp = await client.post(url, json={"body": body})
        ok = resp.status_code < 400
        logger.info("github.comment_posted", pr=number, ok=ok, status=resp.status_code)
        return ok
