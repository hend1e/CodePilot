# backend/agents/review/rules.py
# 确定性规则引擎：不花 token、不抖动、可单测。它承担「机械审查」，LLM 只做「需要理解的判断」。
# 每条规则返回 Finding；evidence 取真实新增行，后续还会被 validate_evidence 二次校验。

import re
from collections.abc import Callable

from backend.agents.review.diff import DiffBundle, FileDiff, is_test_path
from backend.agents.review.state import Finding, Severity

RuleFn = Callable[[DiffBundle], list[Finding]]


def _f(dim: str, sev: Severity, fd: FileDiff, line_no: int, evidence: str,
       rule_id: str, message: str, suggestion: str, confidence: float = 0.9) -> Finding:
    return Finding(
        dimension=dim, severity=sev, file_path=fd.path, line_start=line_no, line_end=line_no,
        rule_id=rule_id, evidence=evidence.strip()[:1000], message=message,
        suggestion=suggestion, confidence=confidence, source="rule",
    )


# ── 安全 ──────────────────────────────────────────────────────
_SECRET_RE = re.compile(
    r"""(?ix)
    (?:api[_-]?key|secret|token|password|passwd|access[_-]?key|private[_-]?key)
    \s*[:=]\s*
    ['"]([A-Za-z0-9_\-./+=]{12,})['"]
    """
)
_PLACEHOLDERS = ("xxx", "your_", "changeme", "placeholder", "<", "${", "***", "example")


def rule_hardcoded_secret(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        for a in fd.added:
            m = _SECRET_RE.search(a.text)
            if not m:
                continue
            val = m.group(1).lower()
            if any(p in val for p in _PLACEHOLDERS) or a.text.strip().startswith(("#", "//")):
                continue
            out.append(_f("security", "block", fd, a.line_no, a.text, "SEC001",
                          "疑似硬编码密钥/口令", "改为从环境变量或密钥管理服务读取，并轮换已泄露的凭证",
                          0.95))
    return out


_SQL_RE = re.compile(r"(?i)\b(select|insert\s+into|update|delete\s+from)\b")
_SQL_INTERP_RE = re.compile(r"(f['\"]|\.format\(|\+\s*\w+|%s?\s*%\s*)")
_SQL_EXEC_RE = re.compile(r"(?i)(execute|executemany|raw|text)\s*\(")


def rule_sql_injection(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        if fd.lang not in ("python", "javascript", "typescript", "java", "go"):
            continue
        for a in fd.added:
            t = a.text
            if _SQL_RE.search(t) and _SQL_INTERP_RE.search(t) and (
                _SQL_EXEC_RE.search(t) or "cursor" in t.lower() or "query" in t.lower()
            ):
                out.append(_f("security", "block", fd, a.line_no, t, "SEC002",
                              "SQL 语句通过字符串拼接/插值构造，存在注入风险",
                              "改用参数化查询（占位符 + 绑定参数），不要拼接用户输入", 0.92))
    return out


_DANGEROUS = [
    (re.compile(r"\beval\s*\("), "SEC003", "使用 eval 执行动态代码", "改用 ast.literal_eval 或显式解析"),
    (re.compile(r"\bexec\s*\("), "SEC004", "使用 exec 执行动态代码", "移除动态执行，改为显式分发"),
    (re.compile(r"\bshell\s*=\s*True\b"), "SEC005", "subprocess 开启 shell=True", "传参数列表并保持 shell=False"),
    (re.compile(r"\bverify\s*=\s*False\b"), "SEC006", "关闭了 TLS 证书校验", "移除 verify=False，修正 CA 配置"),
    (re.compile(r"\byaml\.load\s*\("), "SEC007", "yaml.load 可执行任意对象", "改用 yaml.safe_load"),
    (re.compile(r"\bpickle\.loads?\s*\("), "SEC008", "反序列化不可信 pickle 数据", "改用 JSON 或签名校验后的数据"),
]


def rule_dangerous_calls(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        for a in fd.added:
            for pattern, rid, msg, sug in _DANGEROUS:
                if pattern.search(a.text):
                    out.append(_f("security", "warn", fd, a.line_no, a.text, rid, msg, sug, 0.85))
    return out


_AUTH_RE = re.compile(r"(?i)@(?:router|app)\.(get|post|put|delete)\(['\"]([^'\"]*)")


def rule_route_without_auth(bundle: DiffBundle) -> list[Finding]:
    """新增 HTTP 路由但函数签名里没有鉴权依赖 —— 教育场景里最常见的越权来源。"""
    out = []
    for fd in bundle.files:
        if fd.lang != "python":
            continue
        lines = fd.added
        for i, a in enumerate(lines):
            if not _AUTH_RE.search(a.text):
                continue
            window = "\n".join(x.text for x in lines[i:i + 4])
            if any(k in window for k in ("Depends(get_current_user", "require_role", "Security(")):
                continue
            out.append(_f("security", "warn", fd, a.line_no, a.text, "SEC010",
                          "新增路由未声明鉴权依赖", "补 Depends(get_current_user) 或角色校验", 0.7))
    return out


# ── 性能 ──────────────────────────────────────────────────────
_LOOP_RE = re.compile(r"^\s*(for\s+.+?\s+in\s+|while\s+)")
_IO_RE = re.compile(r"\b(requests\.(get|post|put|delete)|httpx\.|open\(|\.execute\(|\.query\(|"
                    r"session\.get\(|urlopen\(|boto3\.)")


def rule_io_in_loop(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        if fd.lang != "python":
            continue
        indent: int | None = None
        for a in fd.added:
            if _LOOP_RE.match(a.text):
                indent = len(a.text) - len(a.text.lstrip())
                continue
            if indent is None:
                continue
            cur = len(a.text) - len(a.text.lstrip())
            if a.text.strip() and cur <= indent:
                indent = None
                continue
            if _IO_RE.search(a.text):
                out.append(_f("performance", "warn", fd, a.line_no, a.text, "PERF001",
                              "循环体内发起 IO/查询，容易出现 N+1",
                              "改为批量查询（IN 查询 / pipeline / 预取），或把 IO 移出循环", 0.8))
                indent = None
    return out


_DB_IN_LOOP_RE = re.compile(
    r"(db|session|objects|queryset|cursor|conn)\.(query|filter|get|first|all|execute|executemany)\(")


def rule_n_plus_one(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        if fd.lang != "python":
            continue
        for i, a in enumerate(fd.added):
            if not _LOOP_RE.match(a.text):
                continue
            body = "\n".join(x.text for x in fd.added[i:i + 6])
            if _DB_IN_LOOP_RE.search(body):
                out.append(_f("performance", "warn", fd, a.line_no, a.text, "PERF002",
                              "循环内逐条查库（N+1 查询）",
                              "用 selectinload / join / IN 批量取数，一次查询解决", 0.78))
    return out


def rule_nested_loops(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        if fd.lang not in ("python", "javascript", "java", "go"):
            continue
        stack: list[int] = []
        for a in fd.added:
            if not a.text.strip():
                continue
            indent = len(a.text) - len(a.text.lstrip())
            is_loop = bool(_LOOP_RE.match(a.text))
            while stack and indent <= stack[-1]:
                stack.pop()
            if is_loop:
                stack.append(indent)
                if len(stack) >= 3:
                    out.append(_f("performance", "info", fd, a.line_no, a.text, "PERF003",
                                  "三层及以上嵌套循环，复杂度可能是 O(n^3)",
                                  "评估能否用哈希表/排序+双指针降维，或提前剪枝", 0.7))
                    stack.pop()
    return out


# ── 规范 ──────────────────────────────────────────────────────
def rule_broad_except(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        if fd.lang != "python":
            continue
        for i, a in enumerate(fd.added):
            if re.match(r"^\s*except\s*(Exception)?\s*(as\s+\w+)?\s*:\s*$", a.text):
                nxt = fd.added[i + 1].text.strip() if i + 1 < len(fd.added) else ""
                if nxt in ("pass", "...") or nxt.startswith("#"):
                    out.append(_f("style", "warn", fd, a.line_no, a.text, "STY001",
                                  "捕获异常后静默吞掉", "至少记录日志并保留上下文，或只捕获具体异常类型", 0.88))
    return out


def rule_debug_leftover(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        for a in fd.added:
            if re.match(r"^\s*print\s*\(", a.text) and not is_test_path(fd.path):
                out.append(_f("style", "info", fd, a.line_no, a.text, "STY002",
                              "调试用 print 残留在业务代码", "改用 logger 并删除临时输出", 0.75))
            elif re.search(r"\bconsole\.log\s*\(", a.text) and fd.lang in ("javascript", "typescript"):
                out.append(_f("style", "info", fd, a.line_no, a.text, "STY003",
                              "调试用 console.log 残留", "改用统一日志组件", 0.7))
            elif re.search(r"\bdebugger\b", a.text):
                out.append(_f("style", "warn", fd, a.line_no, a.text, "STY004",
                              "残留 debugger 语句", "提交前删除", 0.9))
    return out


def rule_todo(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        for a in fd.added:
            if re.search(r"(TODO|FIXME|XXX)(?![\w])", a.text):
                out.append(_f("style", "info", fd, a.line_no, a.text, "STY005",
                              "新增未完成标记", "补 issue 编号并限期清理", 0.8))
    return out


def rule_long_line(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        for a in fd.added:
            if len(a.text) > 140 and fd.lang in ("python", "javascript", "typescript", "java"):
                out.append(_f("style", "info", fd, a.line_no, a.text, "STY006",
                              f"单行长度 {len(a.text)} 超过 140", "按团队行宽约定换行", 0.6))
    return out


def rule_naming(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        if fd.lang != "python":
            continue
        for a in fd.added:
            m = re.match(r"^\s*(?:async\s+)?def\s+([A-Za-z]\w*)\s*\(", a.text)
            if m and re.search(r"[A-Z]", m.group(1)):
                out.append(_f("style", "info", fd, a.line_no, a.text, "STY007",
                              "函数名不是 snake_case", "按 PEP8 改为小写下划线风格", 0.85))
    return out


# ── 测试 ──────────────────────────────────────────────────────
def rule_missing_tests(bundle: DiffBundle) -> list[Finding]:
    if bundle.touches_tests():
        return []
    symbols = bundle.added_python_symbols()
    if not symbols:
        return []
    fd = next(f for f in bundle.files if f.path == symbols[0]["path"])
    names = ", ".join(s["symbol"] for s in symbols[:5])
    return [Finding(
        dimension="test", severity="warn", file_path=fd.path,
        line_start=symbols[0]["line"], line_end=symbols[0]["line"], rule_id="TEST001",
        evidence=f"新增/修改符号: {names}",
        message="本次变更新增了函数/类，但 diff 中没有对应测试文件",
        suggestion="为新增分支补单测（含边界与异常路径），或说明为何无需测试",
        confidence=0.7, source="rule",
    )]


def rule_skipped_test(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        if not is_test_path(fd.path):
            continue
        for a in fd.added:
            if re.search(r"@(pytest\.mark\.)?skip\b", a.text):
                out.append(_f("test", "warn", fd, a.line_no, a.text, "TEST002",
                              "新增被跳过的测试", "修复后移除 skip，或写明 skip 原因与恢复条件", 0.85))
    return out


def rule_assertion_free_test(bundle: DiffBundle) -> list[Finding]:
    out = []
    for fd in bundle.files:
        if not is_test_path(fd.path) or fd.lang != "python":
            continue
        has_assert = any(re.search(r"\bassert\b|pytest\.raises", a.text) for a in fd.added)
        has_test_def = any(re.match(r"^\s*def\s+test_", a.text) for a in fd.added)
        if has_test_def and not has_assert:
            first = next(a for a in fd.added if re.match(r"^\s*def\s+test_", a.text))
            out.append(_f("test", "warn", fd, first.line_no, first.text, "TEST003",
                          "测试函数内没有任何断言", "补充 assert，否则用例永远通过、没有保护作用", 0.8))
    return out


RULES: list[RuleFn] = [
    rule_hardcoded_secret,
    rule_sql_injection,
    rule_dangerous_calls,
    rule_route_without_auth,
    rule_io_in_loop,
    rule_n_plus_one,
    rule_nested_loops,
    rule_broad_except,
    rule_debug_leftover,
    rule_todo,
    rule_long_line,
    rule_naming,
    rule_missing_tests,
    rule_skipped_test,
    rule_assertion_free_test,
]


def run_rules(bundle: DiffBundle, dimensions: list[str] | None = None) -> list[Finding]:
    """按需跑维度规则；单条规则异常不影响其他规则（失败隔离）。"""
    wanted = set(dimensions or ("security", "performance", "style", "test"))
    out: list[Finding] = []
    for rule in RULES:
        try:
            out.extend(f for f in rule(bundle) if f.dimension in wanted)
        except Exception:       # noqa: BLE001 —— 规则引擎必须永不炸图
            continue
    return _dedupe_overlaps(out)


# 同一处命中两条规则时，前者被后者取代（更具体的结论信息量更大）
_SUPERSEDED = {"PERF001": "PERF002", "STY006": "STY002"}


def _dedupe_overlaps(findings: list[Finding]) -> list[Finding]:
    """同文件、相近行、可归并的规则同时命中时，只保留更具体的那条（降误报）。"""
    drop: set[int] = set()
    for i, a in enumerate(findings):
        target = _SUPERSEDED.get(a.rule_id)
        if not target:
            continue
        for j, b in enumerate(findings):
            if i == j or a.file_path != b.file_path or b.rule_id != target:
                continue
            if abs(a.line_start - b.line_start) <= 2:
                drop.add(i)
                break
    return [f for k, f in enumerate(findings) if k not in drop]


def validate_evidence(findings: list[Finding], added: dict[str, set[str]]) -> tuple[list[Finding], int]:
    """证据校验：evidence 必须能在 diff 新增行里找到，否则视为幻觉丢弃。
    返回 (保留的 findings, 被丢弃条数)。"""
    kept: list[Finding] = []
    dropped = 0
    for f in findings:
        lines = added.get(f.file_path)
        if lines is None:
            # LLM 可能返回相似路径，做一次后缀匹配
            for path, vals in added.items():
                if path.endswith(f.file_path) or f.file_path.endswith(path):
                    lines = vals
                    f.file_path = path
                    break
        if lines is None:
            dropped += 1
            continue
        ev = " ".join(f.evidence.split())
        if not ev:
            dropped += 1
            continue
        if any(ev in line or line in ev for line in lines if line):
            kept.append(f)
        else:
            dropped += 1
    return kept, dropped
