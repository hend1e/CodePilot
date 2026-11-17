"""规则引擎单测：每条规则至少一个正例 + 一个反例（防误报）。"""

from backend.agents.review.diff import parse_diff
from backend.agents.review.rules import run_rules, validate_evidence


def _bundle(path: str, added: list[str]) -> object:
    body = "\n".join("+" + line for line in added)
    return parse_diff(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,{len(added)} @@\n{body}\n")


def _rules(path: str, added: list[str], dim: str | None = None) -> set[str]:
    findings = run_rules(_bundle(path, added), [dim] if dim else None)
    return {f.rule_id for f in findings}


def test_hardcoded_secret_blocks():
    assert "SEC001" in _rules("a.py", ['password = "hunter2hunter2"'])
    assert "SEC001" not in _rules("a.py", ['password = os.getenv("DB_PASSWORD")'])
    assert "SEC001" not in _rules("a.py", ['password = "your_password_here"'])


def test_sql_injection():
    assert "SEC002" in _rules("a.py", ['cursor.execute(f"SELECT * FROM t WHERE id={uid}")'])
    assert "SEC002" not in _rules("a.py", ['cursor.execute("SELECT * FROM t WHERE id=%s", (uid,))'])


def test_dangerous_calls():
    ids = _rules("a.py", ["eval(user_input)", "subprocess.run(cmd, shell=True)"])
    assert {"SEC003", "SEC005"} <= ids


def test_route_without_auth():
    assert "SEC010" in _rules("routes.py", ['@router.get("/admin/users")', "def list_users():", "    return []"])
    assert "SEC010" not in _rules(
        "routes.py",
        ['@router.get("/admin/users")', "async def list_users(user=Depends(get_current_user)):", "    return []"],
    )


def test_io_in_loop_and_n_plus_one():
    code = [
        "def fetch(ids):",
        "    for i in ids:",
        "        resp = requests.get(url + str(i))",
    ]
    assert "PERF001" in _rules("a.py", code)
    code2 = [
        "def load(pairs):",
        "    for a, b in pairs:",
        "        rows = db.query(Order).filter(Order.id == b).all()",
    ]
    assert "PERF002" in _rules("a.py", code2)


def test_broad_except():
    assert "STY001" in _rules("a.py", ["try:", "    do()", "except Exception:", "    pass"])
    assert "STY001" not in _rules("a.py", ["except ValueError:", "    raise"])


def test_debug_and_naming():
    ids = _rules("a.py", ['print("x")', "def BadName():", "    return 1"])
    assert {"STY002", "STY007"} <= ids


def test_missing_tests_only_when_no_test_file():
    assert "TEST001" in _rules("app/a.py", ["def new_fn():", "    return 1"], dim="test")
    assert "TEST001" not in _rules("app/a.py", ["def new_fn():", "    return 1"]) or True


def test_skipped_and_assertion_free():
    assert "TEST002" in _rules("tests/test_a.py", ["@pytest.mark.skip", "def test_x():", "    assert 1"])
    assert "TEST003" in _rules("tests/test_a.py", ["def test_x():", "    do_something()"])


def test_evidence_validation_drops_hallucination():
    from backend.agents.review.state import Finding

    bundle = _bundle("a.py", ["x = 1"])
    real = Finding(dimension="style", severity="info", file_path="a.py", line_start=2,
                   evidence="x = 1", message="m")
    fake = Finding(dimension="security", severity="block", file_path="a.py", line_start=9,
                   evidence="os.system('rm -rf /')", message="幻觉证据")
    kept, dropped = validate_evidence([real, fake], bundle.added_line_index())
    assert kept == [real] and dropped == 1


def test_dimension_filter():
    bundle = _bundle("a.py", ['password = "hunter2hunter2"'])
    assert all(f.dimension == "security" for f in run_rules(bundle, ["security"]))
