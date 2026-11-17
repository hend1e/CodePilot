# backend/agents/rag/chunking.py
# 代码切分：按「函数/类」切，而不是按字符窗口切。
# 优先用 tree-sitter（Python）；不可用时退回缩进感知的正则扫描 —— 两条路径输出同样结构。
# 为什么函数级：一个函数被切断，检索到的就是半个语义，RAG 必然答不准。

import re
from dataclasses import dataclass, field

from backend.agents.review.diff import lang_of

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

MAX_CHUNK_LINES = 120
OVERLAP_LINES = 12
MAX_FILE_BYTES = 400_000
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
             ".mypy_cache", ".pytest_cache", ".idea", ".next", "target", "vendor"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
              ".whl", ".so", ".dll", ".exe", ".mp4", ".woff", ".woff2", ".ttf"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".java", ".go", ".rs", ".sql", ".sh",
            ".yml", ".yaml", ".toml", ".json", ".md", ".txt"}


@dataclass
class CodeChunk:
    path: str
    symbol: str = ""
    symbol_type: str = "block"
    start_line: int = 1
    end_line: int = 1
    content: str = ""
    lang: str = "text"

    def citation(self) -> str:
        sym = f"::{self.symbol}" if self.symbol else ""
        return f"{self.path}{sym}:{self.start_line}-{self.end_line}"

    def as_dict(self) -> dict:
        return {"path": self.path, "symbol": self.symbol, "symbol_type": self.symbol_type,
                "start_line": self.start_line, "end_line": self.end_line,
                "content": self.content, "lang": self.lang}


def _split_long(path: str, symbol: str, stype: str, start: int, lines: list[str],
                lang: str) -> list[CodeChunk]:
    if len(lines) <= MAX_CHUNK_LINES:
        return [CodeChunk(path, symbol, stype, start, start + len(lines) - 1,
                          "\n".join(lines), lang)]
    out: list[CodeChunk] = []
    i = 0
    while i < len(lines):
        part = lines[i:i + MAX_CHUNK_LINES]
        out.append(CodeChunk(path, f"{symbol}#{len(out) + 1}" if symbol else "",
                             stype, start + i, start + i + len(part) - 1,
                             "\n".join(part), lang))
        if i + MAX_CHUNK_LINES >= len(lines):
            break
        i += MAX_CHUNK_LINES - OVERLAP_LINES
    return out


# ── 方案 A：tree-sitter（更准，可选依赖）───────────────────────
def _ts_chunks(path: str, content: str) -> list[CodeChunk] | None:
    if not path.endswith(".py"):
        return None
    try:
        import tree_sitter_python as tsp
        from tree_sitter import Language, Parser
    except Exception:       # noqa: BLE001 —— 依赖缺失就走方案 B
        return None

    try:
        parser = Parser(Language(tsp.language()))
        tree = parser.parse(content.encode("utf-8"))
    except Exception:       # noqa: BLE001
        return None

    lines = content.split("\n")
    chunks: list[CodeChunk] = []

    def visit(node, depth: int = 0):
        if node.type in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            name = content[name_node.start_byte:name_node.end_byte] if name_node else ""
            stype = "class" if node.type == "class_definition" else "function"
            start = node.start_point[0]
            end = node.end_point[0]
            chunks.append(CodeChunk(path, name, stype, start + 1, end + 1,
                                    "\n".join(lines[start:end + 1]), "python"))
            if depth == 0:      # 方法不再单独切（跟随类一起，避免语义碎片）
                return
        for child in node.children:
            visit(child, depth + 1)

    visit(tree.root_node)
    if not chunks:
        return None
    chunks.sort(key=lambda c: c.start_line)
    if chunks[0].start_line > 1:
        header = lines[:chunks[0].start_line - 1]
        if any(x.strip() for x in header):
            chunks.insert(0, CodeChunk(path, "__module__", "module", 1,
                                       len(header), "\n".join(header), "python"))
    return chunks


# ── 方案 B：缩进感知正则（零依赖兜底）────────────────────────
_DEF_RE = re.compile(r"^(?P<indent>\s*)(?:async\s+)?(?:def|class)\s+(?P<name>[A-Za-z_]\w*)")


def _regex_chunks(path: str, content: str) -> list[CodeChunk]:
    lang = lang_of(path)
    lines = content.split("\n")
    if lang != "python":
        return _split_long(path, "", "block", 1, lines, lang)

    starts: list[tuple[int, str, str, int]] = []   # (行号0基, 名字, 类型, 缩进)
    for i, line in enumerate(lines):
        m = _DEF_RE.match(line)
        if m:
            kind = "class" if re.search(r"\bclass\b", line) else "function"
            starts.append((i, m.group("name"), kind, len(m.group("indent"))))
    if not starts:
        return _split_long(path, "", "module", 1, lines, lang)

    chunks: list[CodeChunk] = []
    # 文件头（import / 常量 / 模块 docstring）
    if starts[0][0] > 0:
        chunks.extend(_split_long(path, "__module__", "module", 1, lines[:starts[0][0]], lang))

    for idx, (ln, name, kind, indent) in enumerate(starts):
        if indent > 0:          # 方法：跳过，跟随类
            continue
        end = len(lines)
        for nxt_ln, _, _, nxt_indent in starts[idx + 1:]:
            if nxt_indent <= indent:
                end = nxt_ln
                break
        chunks.extend(_split_long(path, name, kind, ln + 1, lines[ln:end], lang))
    return chunks


def chunk_file(path: str, content: str) -> list[CodeChunk]:
    if len(content.encode("utf-8", "ignore")) > MAX_FILE_BYTES:
        content = content.encode("utf-8", "ignore")[:MAX_FILE_BYTES].decode("utf-8", "ignore")
    raw = _ts_chunks(path, content) or _regex_chunks(path, content)
    chunks: list[CodeChunk] = []
    for c in raw:
        chunks.extend(_split_long(c.path, c.symbol, c.symbol_type, c.start_line,
                                  c.content.split("\n"), c.lang))
    return [c for c in chunks if c.content.strip() and len(c.content.strip()) >= 20]


def iter_repo_files(root: str):
    """遍历仓库文件（跳过依赖/构建/二进制/超大文件）。返回 (相对路径, 绝对路径)。"""
    from pathlib import Path

    base = Path(root).resolve()
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(base).parts):
            continue
        if p.suffix.lower() in BINARY_EXT or (p.suffix.lower() not in CODE_EXT and p.name != "Dockerfile"):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield str(p.relative_to(base)), p


# ── 文本工具（稀疏检索与索引共用，放在这里避免循环导入）──────────
def term_queries(text: str, limit: int = 40) -> list[str]:
    """拆检索项：英文标识符 + 中文 bigram（PG simple 配置不分中文词，必须自己切）。"""
    terms: list[str] = [m.group(0).lower() for m in _IDENT_RE.finditer(text)]
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def search_text(content: str) -> str:
    """稀疏检索文本：原文 + 中文 bigram，保证中文问题能命中中文注释。"""
    bigrams = [t for t in term_queries(content) if not t.isascii()]
    return content + ("\n" + " ".join(bigrams) if bigrams else "")
