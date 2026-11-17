# backend/agents/review/diff.py
# 统一 diff 解析：把 git diff 文本变成结构化的「文件 → 新增行」，供规则引擎、证据校验、
# 单测生成共同使用。为什么不用第三方库：我们只需要 added lines + 行号，自己解析最可控。

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")
_BINARY_RE = re.compile(r"^Binary files .* differ$")


@dataclass
class AddedLine:
    line_no: int          # 新文件中的行号
    text: str             # 不含前导 '+'


@dataclass
class FileDiff:
    path: str
    old_path: str = ""
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    added: list[AddedLine] = field(default_factory=list)
    removed_count: int = 0

    @property
    def lang(self) -> str:
        return lang_of(self.path)

    @property
    def added_text(self) -> str:
        return "\n".join(a.text for a in self.added)


@dataclass
class DiffBundle:
    files: list[FileDiff] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.files)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def added_lines(self) -> int:
        return sum(len(f.added) for f in self.files)

    @property
    def removed_lines(self) -> int:
        return sum(f.removed_count for f in self.files)

    def text(self, max_chars: int = 120_000) -> str:
        """回放为紧凑 diff 文本，供 prompt 使用（超长截断，预算保护）。"""
        out: list[str] = []
        used = 0
        for f in self.files:
            head = f"--- a/{f.old_path or f.path}\n+++ b/{f.path}"
            body = "\n".join(f"+{a.text}" for a in f.added)
            block = f"{head}\n{body}"
            if used + len(block) > max_chars:
                out.append(f"... 已截断（剩余 {len(self.files) - len(out)} 个文件）")
                break
            out.append(block)
            used += len(block)
        return "\n".join(out)

    def added_line_index(self) -> dict[str, set[str]]:
        """路径 → 新增行文本集合（规范化空白），用于证据校验。"""
        return {f.path: {_norm(a.text) for a in f.added if a.text.strip()} for f in self.files}

    def added_python_symbols(self) -> list[dict]:
        """新增/修改的函数与类（单测生成的靶子）。"""
        symbols: list[dict] = []
        for f in self.files:
            if f.lang != "python" or f.is_deleted:
                continue
            for a in f.added:
                m = re.match(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", a.text)
                if m and not m.group(1).startswith("_"):
                    symbols.append({"path": f.path, "symbol": m.group(1), "kind": "function",
                                    "line": a.line_no})
                    continue
                m = re.match(r"^\s*class\s+([A-Za-z_]\w*)", a.text)
                if m:
                    symbols.append({"path": f.path, "symbol": m.group(1), "kind": "class",
                                    "line": a.line_no})
        # 去重（同一函数多个新增行只保留一次）
        seen: set[tuple[str, str]] = set()
        uniq = []
        for s in symbols:
            k = (s["path"], s["symbol"])
            if k not in seen:
                seen.add(k)
                uniq.append(s)
        return uniq

    def touches_tests(self) -> bool:
        return any(is_test_path(p) for p in self.paths)


LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".go": "go", ".rs": "rust", ".sql": "sql", ".md": "markdown",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".json": "json", ".sh": "shell",
}


def lang_of(path: str) -> str:
    for ext, lang in LANG_BY_EXT.items():
        if path.lower().endswith(ext):
            return lang
    return "text"


def is_test_path(path: str) -> bool:
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    return (
        "/test" in p or p.startswith("test") or "/tests/" in p
        or name.startswith("test_") or name.endswith("_test.py")
        or ".test." in name or ".spec." in name
    )


def _norm(s: str) -> str:
    return " ".join(s.split())


def parse_diff(text: str) -> DiffBundle:
    """解析 `git diff` 文本。容忍邮件格式、CRLF、缺失文件头。"""
    bundle = DiffBundle()
    current: FileDiff | None = None
    new_line = 0
    in_hunk = False

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw

        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if m:
                current = FileDiff(path=m.group(2), old_path=m.group(1))
                bundle.files.append(current)
            in_hunk = False
            continue

        if current is None:
            continue

        if line.startswith("new file mode"):
            current.is_new = True
            continue
        if line.startswith("deleted file mode"):
            current.is_deleted = True
            continue
        if _BINARY_RE.match(line):
            current.is_binary = True
            continue
        if line.startswith("--- ") and line != "--- /dev/null":
            current.old_path = line[4:].removeprefix("a/")
            continue
        if line.startswith("+++ "):
            p = line[4:]
            if p == "/dev/null":
                current.is_deleted = True
            else:
                current.path = p.removeprefix("b/")
            continue

        m = _HUNK_RE.match(line)
        if m:
            new_line = int(m.group("new"))
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if line.startswith("+"):
            current.added.append(AddedLine(line_no=new_line, text=line[1:]))
            new_line += 1
        elif line.startswith("-"):
            current.removed_count += 1
        elif line.startswith("\\"):     # "\ No newline at end of file"
            continue
        else:
            new_line += 1

    # 只保留有新增行的文件：纯删除/二进制对「新增代码审查」没有意义
    bundle.files = [f for f in bundle.files if f.added and not f.is_binary and not f.is_deleted]
    return bundle
