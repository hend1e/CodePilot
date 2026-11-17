"""diff 解析与证据校验的单测（纯函数，不依赖 DB/LLM）。"""

from backend.agents.review.diff import is_test_path, parse_diff

SAMPLE = """diff --git a/app/svc.py b/app/svc.py
--- a/app/svc.py
+++ b/app/svc.py
@@ -1,3 +1,8 @@
 import os
+API_KEY = "sk-live-1234567890abcdef"
+
+def get_orders(uid):
+    return db.query(uid)
diff --git a/tests/test_svc.py b/tests/test_svc.py
new file mode 100644
--- /dev/null
+++ b/tests/test_svc.py
@@ -0,0 +1,3 @@
+def test_ok():
+    assert True
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""


def test_parse_files_and_added_lines():
    bundle = parse_diff(SAMPLE)
    assert bundle.paths == ["app/svc.py", "tests/test_svc.py", "README.md"]
    assert bundle.added_lines == 7
    svc = bundle.files[0]
    assert [a.line_no for a in svc.added] == [2, 3, 4, 5]
    assert svc.added[0].text.startswith("API_KEY")


def test_new_file_flag_and_added_line_index():
    bundle = parse_diff(SAMPLE)
    assert bundle.files[1].is_new is True
    index = bundle.added_line_index()
    assert 'API_KEY = "sk-live-1234567890abcdef"' in index["app/svc.py"]


def test_python_symbols():
    symbols = parse_diff(SAMPLE).added_python_symbols()
    assert {"path": "app/svc.py", "symbol": "get_orders", "kind": "function", "line": 4} in symbols
    assert any(s["symbol"] == "test_ok" for s in symbols)


def test_touches_tests_and_paths():
    bundle = parse_diff(SAMPLE)
    assert bundle.touches_tests() is True
    assert is_test_path("tests/test_a.py") and is_test_path("src/a.test.ts")
    assert not is_test_path("app/svc.py")


def test_crlf_and_garbage_tolerated():
    text = SAMPLE.replace("\n", "\r\n")
    assert len(parse_diff(text).files) == 3
    assert parse_diff("not a diff").files == []


def test_binary_and_deleted_filtered():
    text = """diff --git a/x.png b/x.png
Binary files a/x.png and b/x.png differ
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
"""
    assert parse_diff(text).files == []
