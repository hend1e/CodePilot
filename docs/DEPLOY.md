# 部署与运维

## 一、两种部署形态

### A. 本地开发（推荐先跑通这个）

基础设施跑容器，应用跑本机：

```bash
cp .env.example .env.local          # 填 LLM_API_KEY
docker compose up -d                # PG / Redis / Milvus / Attu
make sandbox-image                  # 构建沙箱镜像
python3.11 -m venv .venv && make install
make seed
make api                            # 终端 1
make demo                           # 终端 2（INLINE_WORKER=true 时任务在 API 进程内执行）
```

优点：代码热重载；worker 直接调用宿主机 Docker，不需要挂 `docker.sock`。

### B. 全容器（单文件）

```bash
export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
docker compose -f docker-compose.all.yml up -d --build
```

只需要基础设施时：`docker compose -f docker-compose.all.yml up -d postgres redis milvus attu`。

> 两个 compose 文件必须**合并**启动：`docker-compose.app.yml` 里 api/worker 的 `depends_on` 指向 infra 文件里的 postgres/redis。

API：`http://localhost:8000/docs`；Attu：`http://localhost:30000`。

> ⚠️ worker 挂载 `/var/run/docker.sock` 等价于给它宿主机 root 权限。
> 只适合内网/演示。生产请用：独立沙箱服务（gRPC 调用）+ gVisor/Kata/Firecracker，
> 或把 worker 部署在专用节点且只允许访问沙箱镜像。

## 二、容器清单（到底起什么）

| 容器 | 镜像 | 端口（宿主→容器） | 作用 | 必需 |
|---|---|---|---|---|
| `codepilot-postgres` | `postgres:16-alpine` | 5434→5432 | 业务表 + LangGraph checkpoint | ✅ |
| `codepilot-redis` | `redis:7-alpine` | 6381→6379 | 任务队列（Streams）+ 限流 | ✅ |
| `codepilot-etcd` | `quay.io/coreos/etcd:v3.5.14` | 不暴露 | Milvus 元数据 | ✅（用 Milvus 时） |
| `codepilot-minio` | `minio/minio:RELEASE.2023-03-20...` | 不暴露 | Milvus 对象存储 | ✅（用 Milvus 时） |
| `codepilot-milvus` | `milvusdb/milvus:v2.4.9` | 19532→19530 | 向量库 | 可选（缺省降级为纯全文检索） |
| `codepilot-attu` | `zilliz/attu:v2.4.12` | 30010→3000 | Milvus 可视化 | 可选 |
| `codepilot-api` | 自建 `codepilot/api:0.1.0` | 8000→8000 | HTTP / SSE | ✅ |
| `codepilot-worker` | 同上（换 command） | 无 | 消费队列、跑图、调沙箱 | ✅ |
| `codepilot/sandbox:py311` | 自建 | 一次性 | 跑 LLM 生成的单测（`--network none`） | 用单测功能时 |

**最小可用组合**：postgres + redis + api + worker（Milvus 不启则 RAG 走 PG 全文检索，会标 `degraded`）。
**完整组合**：上表全部。

端口故意错开了常见默认值（5432/6379/19530），也避开了 EduAgent 课程项目占用的 5433/6380/19531/30000，避免和你机器上已有服务冲突；要改就改 `.env.local`。

## 三、GitHub Webhook 接入

1. 仓库 → Settings → Webhooks → Add webhook
2. Payload URL：`https://<你的域名>/api/v1/webhooks/github`
   （本地调试用 `cloudflared tunnel --url http://localhost:8000` 或 ngrok）
3. Content type：`application/json`
4. Secret：填 `.env.local` 里的 `GITHUB_WEBHOOK_SECRET`（**必须**，生产不填会拒绝）
5. 事件：勾选 `Pull requests`
6. `.env.local` 里配 `GITHUB_TOKEN`（拉 diff、写评论），需要 `repo` 权限；
   想自动回写评论再打开 `GITHUB_COMMENT_ENABLED=true`

触发后：webhook 立刻返回 `202`，任务进 Redis，worker 拉 diff → 审查 → 回写评论。

## 四、生产加固清单

- [ ] `APP_ENV=prod`，`APP_DEBUG=false`，`JWT_SECRET_KEY` 换成 32 字节随机值
- [ ] `GITHUB_WEBHOOK_SECRET` 必填；反向代理只暴露 443
- [ ] 不挂 `docker.sock`，沙箱改成独立服务 + gVisor/Kata
- [ ] 数据库开启 RLS（当前靠 `tenant_id` 查询条件隔离）
- [ ] Redis 设 `maxmemory` + AOF；Milvus 数据卷定期快照
- [ ] worker 多副本（消费组自动分片），`INLINE_WORKER=false`
- [ ] LLM 网关加熔断与配额；`REVIEW_MAX_COST_USD` 按团队预算调小
- [ ] 接 Langfuse（填 `LANGFUSE_*`）看每次图执行的 token 与耗时
- [ ] `/metrics` 接 Prometheus，重点看 `codepilot_reviews_created_total`、`degraded`

## 五、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `/readyz` 里 milvus=false | 首次拉镜像 + 启动 30~90s | `docker compose logs -f milvus`，等 healthy |
| 表不存在 | `init_db.sql` 只在**数据卷为空**时执行 | `docker compose down -v` 重来，或手动 `psql -f scripts/init_db.sql` |
| 任务一直 queued | 没有 worker 在消费 | 看 `INLINE_WORKER`；或 `make worker` |
| 沙箱 `docker: not found` | worker 容器没有 docker CLI / socket | 用全容器 compose（镜像已装 CLI）或改 `SANDBOX_MODE=local` |
| 沙箱全部 error | `--user uid:gid` 与挂载目录权限不匹配 | 确保 `./workspace` 对运行用户可读写 |
| 审查结果全是 rules_only | `LLM_PROVIDER=mock` 或 API Key 为空 | 检查 `LLM_API_KEY`，`/healthz` 里看 `llm_enabled` |
| webhook 401 | 签名不匹配 | 检查 Secret；确认代理没有改写 body |
| 500 且日志 `api.unhandled` | 未预期异常 | 按 `trace id` 查 JSON 日志，先看 `error` 字段 |
| Milvus 写入报维度不匹配 | `EMBEDDING_DIM` 与模型维度不一致（BGE-M3=1024） | 改配置后删除 collection：`attu` 里删或 `pymilvus` `drop_collection` |

## 六、资源建议

| 规模 | CPU / 内存 | 说明 |
|---|---|---|
| 本地开发 | 4C / 8G | Milvus 约占 2G |
| 小团队（<10 仓库，日 <50 PR） | 8C / 16G | API 2 副本 + worker 2 副本 |
| 中等（40 仓库，日 160 PR） | 16C / 32G | worker 按并发调，Milvus 独立节点 |
