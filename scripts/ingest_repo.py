# scripts/ingest_repo.py
# 给代码库建索引（函数级切分 → 向量化 → PG + Milvus）。
# 用法：
#   python -m scripts.ingest_repo --repo local/demo --path ./workspace/demo-repo
#   python -m scripts.ingest_repo --repo local/demo --path . --no-reset

import argparse
import asyncio
import os

from backend.core.logger import configure_logging
from backend.services.review_service import ensure_repo


async def main() -> None:
    parser = argparse.ArgumentParser(description="CodePilot 代码库索引")
    parser.add_argument("--repo", required=True, help="仓库标识，如 owner/repo")
    parser.add_argument("--path", required=True, help="本地仓库路径")
    parser.add_argument("--tenant", default=os.getenv("DEFAULT_TENANT_ID", "tenant_default"))
    parser.add_argument("--no-reset", action="store_true", help="不删除旧索引（增量追加）")
    args = parser.parse_args()

    configure_logging()

    from backend.agents.rag.store import ingest_repo

    repo = await ensure_repo(args.tenant, args.repo, provider="local", local_path=args.path)
    stats = await ingest_repo(repo["id"], args.path, reset=not args.no_reset)
    print("\n✅ 索引完成：")
    for k, v in stats.items():
        print(f"   {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
