"""代码切分单测：函数级切分、行号正确、长函数滑窗、非 Python 兜底。"""

from backend.core.chunking import chunk_file, iter_repo_files

PY = '''"""module doc"""
import os

CONST = 1


def alpha(x):
    return x + 1


class Beta:
    def method(self):
        return 2


async def gamma():
    return 3
'''


def test_python_chunk_by_symbol():
    chunks = chunk_file("pkg/mod.py", PY)
    names = {c.symbol for c in chunks}
    # 类只作为方法前缀出现，避免「类块 + 方法块」重复索引
    assert {"__module__", "alpha", "Beta.method", "gamma"} <= names
    alpha = next(c for c in chunks if c.symbol == "alpha")
    assert alpha.start_line == 7 and alpha.end_line == 8
    assert "return x + 1" in alpha.content
    assert alpha.citation() == "pkg/mod.py::alpha:7-8"


def test_class_methods_are_split_with_prefix():
    chunks = chunk_file("pkg/mod.py", PY)
    method = next(c for c in chunks if c.symbol == "Beta.method")
    assert "return 2" in method.content
    assert not any(c.symbol == "Beta" for c in chunks)      # 不再重复切整个类


def test_long_function_is_split_with_overlap():
    lines = ["def big():"] + [f"    x{i} = {i}" for i in range(300)]
    chunks = chunk_file("big.py", "\n".join(lines))
    assert len(chunks) >= 2
    assert all(c.start_line <= c.end_line for c in chunks)
    assert chunks[1].start_line <= chunks[0].end_line      # 有重叠，语义不断裂


def test_non_python_falls_back_to_windows():
    js = "\n".join(f"const v{i} = {i};" for i in range(200))
    chunks = chunk_file("web/a.js", js)
    assert chunks and chunks[0].lang == "javascript"


def test_iter_repo_files_filters(tmp_path):
    (tmp_path / "app.py").write_text("print(1)\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("var a=1;\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")

    found = {rel for rel, _ in iter_repo_files(str(tmp_path))}
    assert found == {"app.py"}


def test_term_queries_and_search_text():
    from backend.core.chunking import search_text, term_queries

    terms = term_queries("鉴权中间件 AuthMiddleware 怎么用")
    assert "authmiddleware" in terms
    assert "鉴权" in terms and "中间" in terms            # 中文 bigram（按 2 字切）
    text = search_text("统一鉴权中间件")
    assert "鉴权" in text.split("\n")[-1]
