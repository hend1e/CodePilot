# scripts/demo_review.py
# 端到端演示：登录 → 提交一个含问题的 diff → 轮询结果。
# 用法：python -m scripts.demo_review            （需要 API 已启动）
#       python -m scripts.demo_review --offline  （不启动服务，直接跑图，mock 模式也能用）

import argparse
import asyncio
import json
import os

import httpx

BASE = os.getenv("CODEPILOT_BASE", "http://localhost:8000")
USERNAME = os.getenv("SEED_USERNAME", "codepilot")
PASSWORD = os.getenv("SEED_PASSWORD", "codepilot123")

DEMO_DIFF = """diff --git a/app/services/user_service.py b/app/services/user_service.py
--- a/app/services/user_service.py
+++ b/app/services/user_service.py
@@ -1,5 +1,40 @@
 import requests
+import sqlite3
+
+DB_PASSWORD = "SuperSecret123456"
+
+
+def get_user_orders(user_id):
+    conn = sqlite3.connect("app.db")
+    cursor = conn.cursor()
+    for uid in user_id:
+        cursor.execute(f"SELECT * FROM orders WHERE user_id = {uid}")
+        rows = cursor.fetchall()
+    return rows
+
+
+def fetch_profiles(ids):
+    out = []
+    for i in ids:
+        resp = requests.get(f"https://api.example.com/profiles/{i}")
+        out.append(resp.json())
+    return out
+
+
+def processItems(Items):
+    try:
+        return [x * 2 for x in Items]
+    except Exception:
+        pass
+
+
+def render(template, data):
+    print("rendering", template)
+    return eval(template)(data)
diff --git a/app/api/routes.py b/app/api/routes.py
--- a/app/api/routes.py
+++ b/app/api/routes.py
@@ -1,3 +1,8 @@
 from fastapi import APIRouter
+
+router = APIRouter()
+
+
+@router.get("/admin/users")
+def list_users():
+    return {"users": []}
"""


async def online() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=120) as client:
        r = await client.post("/api/v1/auth/login",
                              json={"username": USERNAME, "password": PASSWORD})
        r.raise_for_status()
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = await client.post("/api/v1/reviews", headers=headers, json={
            "repo": "local/demo",
            "diff": DEMO_DIFF,
            "pr_number": 42,
            "pr_title": "demo: 用户服务与订单接口",
            "local_path": os.getenv("SEED_REPO_PATH") or None,
        })
        r.raise_for_status()
        task_id = r.json()["task_id"]
        print(f"✅ 已创建任务 {task_id}，等待审查…")

        for _ in range(60):
            await asyncio.sleep(2)
            r = await client.get(f"/api/v1/reviews/{task_id}", headers=headers)
            data = r.json()
            if data["status"] in ("done", "failed", "waiting_human"):
                print(json.dumps({k: data[k] for k in ("status", "final")},
                                 ensure_ascii=False, indent=2, default=str)[:6000])
                if data["status"] == "waiting_human":
                    print("\n⏸ 等待人工确认：POST /api/v1/reviews/%s/resume" % task_id)
                return
        print("⌛ 超时，请检查 worker 日志")


async def offline() -> None:
    from backend.core.logger import configure_logging
    from backend.db.session import init_checkpointer
    from backend.core.review_service import create_task, run_review_task

    configure_logging()
    os.environ.setdefault("INLINE_WORKER", "false")
    await init_checkpointer()
    repo_path = os.getenv("SEED_REPO_PATH") or None
    task_id, _ = await create_task(
        tenant_id="tenant_default", repo_full_name="local/demo",
        diff_text=DEMO_DIFF, pr={"number": 42, "title": "demo"},
        local_path=repo_path, source="demo",
    )
    final = await run_review_task(task_id)
    print(json.dumps(final, ensure_ascii=False, indent=2, default=str)[:8000])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="不经过 HTTP，直接跑图")
    args = parser.parse_args()
    asyncio.run(offline() if args.offline else online())
