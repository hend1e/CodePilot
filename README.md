# CodePilot

**多 Agent 研发协作与代码审查平台** —— PR 四维并行审查 · 代码库 RAG 问答 · 单测生成与沙箱验证。

FastAPI · LangGraph · PostgreSQL · Redis · Milvus · Docker

---

## 项目简介

CodePilot 把大模型能力嵌入研发流程的三个高频环节：

- **PR 评审**：安全、性能、规范、测试四个维度由规则引擎与 LLM 协同检查，结论带证据、可分级、可阻断合入；
- **内部知识检索**：代码库与历史资产建成索引，支持带引用的自然语言问答与统一对话入口；
- **单元测试**：从 diff 识别新增函数，生成测试并在受限容器里真实执行，覆盖率与失败结果回流到流程中。

平台的设计取向是 **主干确定、局部自主**：编排是一张有向图，超时、降级、预算、人工确认都是图上的显式节点或条件边；Agent 只在节点内部拥有自主权。因此每条结论都可追溯（文本证据 → diff 行）、可观测（状态、用量、降级原因）、可兜底（规则降级、人工介入）。

| 能力 | 入口 | 关键实现 |
|---|---|---|
| PR 四维并行审查 | GitHub Webhook / REST / Chat | LangGraph `Send` 扇出 + 规则引擎 + 证据校验 + 分级阻断门 |
| 代码库 RAG 问答 | REST / Chat（SSE） | 函数级切分 + dense/sparse 混合召回 + RRF + 精排 + 引用校验 |
| 单测生成与验证 | REST / Chat | diff 识别新增符号 → LLM 生成 → 沙箱 `pytest --cov` → `interrupt` 人工确认 |

## 界面

| 审查工作台 | 代码问答 |
|---|---|
| ![review](docs/screenshots/review.png) | ![chat](docs/screenshots/chat.png) |

Vue 3 + Vite 构建的单页应用，由 FastAPI 同源托管，无需额外反向代理。三个页签：**审查工作台**（提交 diff、轮询状态、按严重度与维度过滤、HitL 确认）、**代码问答**（SSE 流式输出、引用可跳转）、**任务记录**（历史任务与报告回看）。构建产物 `frontend/dist` 随仓库提交，因此只运行后端也能获得完整界面。

## 系统架构

```
GitHub Webhook ──验签──┐
Manual / Chat ────────┤
                      ▼
                FastAPI (202) ──► Redis Streams ──► Worker
                                                    │
                                    ┌───────────────▼────────────────┐
                                    │ LangGraph Orchestrator（主图）  │
                                    │ route → review / qa / test     │
                                    │          （3 子图）             │
                                    └───────────────┬────────────────┘
        ┌───────────────────────────────────────────┼──────────────────────────────┐
        ▼                                           ▼                              ▼
  PR 审查子图                                  RAG 子图                    单测生成子图
  ingest → triage → retrieve                  rewrite → retrieve           generate → sandbox
   → Send 四维并行 → aggregate                 → answer → persist           → 回灌 ≤2 轮 → interrupt
   → testgen → sandbox → hitl → publish                                            → resume → done
        │                                           │                              │
        └──────────────► PostgreSQL / Milvus / Docker Sandbox ◄────────────────────┘
```

- **接入层**：Webhook 验签后立即返回 `202`，任务写入 Redis Streams，由独立 worker 消费；幂等键为 `(repo, pr, head_sha)`，新 commit 会把旧任务标记为 `superseded`。
- **编排层**：主图按意图路由到三个子图；状态中只放决策所需的最小集，大文本落库并保留引用。并行写入统一走幂等 reducer，输出顺序稳定可 diff。
- **能力层**：`backend/core/` 提供 LLM 工厂（多槽位 + mock 模式 + 成本估算）、检索、沙箱、队列、限流、指标等通用能力，与具体的 Agent 逻辑解耦。

状态字段、reducer 约束、超时与预算闸门、降级矩阵等细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 技术栈

| 层 | 选型 |
|---|---|
| 服务 | FastAPI、uvicorn、pydantic-settings、asyncio |
| 编排 | LangGraph（主图 + 子图、`Send` 扇出、checkpoint、`interrupt`） |
| 存储 | PostgreSQL 16（业务表 + `AsyncPostgresSaver`）、Redis 7（Streams 队列 + 限流） |
| 检索 | Milvus 2.4 + Attu（向量）、PG `tsvector`（稀疏）、RRF、lexical / cross-encoder 精排 |
| 模型 | 任意 OpenAI 兼容网关（默认 DeepSeek）；hash / BGE-M3 / OpenAI 兼容 embedding |
| 沙箱 | Docker：断网、内存/CPU/PID 限额、`--cap-drop ALL`、非 root 用户 |
| 前端 | Vue 3 + Vite |
| 工程 | pytest（47 项单测）、ruff、GitHub Actions、Docker Compose |

## 代码结构

```
backend/
  main.py / worker.py / config.py / dependencies.py   入口、队列消费者、配置、鉴权
  core/        llm_factory · retry · security · logger · exceptions
               knowledge_base · chunking · embedding · reranker · query_classifier · memory
               orchestrator · queue · sandbox · github · rate_limit · metrics · review_service
  db/          session（asyncpg + AsyncPostgresSaver） · migrations
  agents/
    review/    state(reducer) · rules(15 条确定性规则) · diff · reviewers · nodes · graph · prompts · persist
    qa/        state · prompts · nodes（rewrite / retrieve / answer / persist） · graph
    testgen/   state · prompts · nodes（生成 / 沙箱 / HitL） · graph
  api/
    router.py  聚合 v1 路由
    v1/        auth · reviews · qa · unified_chat（SSE） · webhooks · health
frontend/      Vue 3 + Vite 单页应用（审查工作台 / 代码问答 / 任务记录）
scripts/       init_db.sql · seed_data · ingest_repo · demo_review · eval_review
tests/         diff · rules · reducer · chunking · router · security · sandbox 等纯函数单测
docs/          ARCHITECTURE（架构与降级） · DEPLOY（部署与运维） · COURSE_MAP / MAPPING（能力与实现对照）
eval/          人工标注的 diff 样本与评测脚本配置
```

## 运行方式

```bash
cp .env.example .env.local      # 填入 LLM_API_KEY（DeepSeek 或任意 OpenAI 兼容网关）
docker compose up -d            # PostgreSQL / Redis / Milvus / Attu
make sandbox-image              # 构建单测沙箱镜像
python3.11 -m venv .venv && make install
make seed                       # 初始化演示账号 codepilot / codepilot123
make api                        # http://localhost:8000/docs
```

`make demo` 会提交一个包含硬编码密钥、SQL 注入、循环内 IO、吞异常、`eval`、缺鉴权的示例 diff，返回 6~8 条 findings，其中 3 条为 `block`、`mergeable=false`。`make ingest REPO=local/demo PATH=./workspace/demo` 可为本地仓库建立检索索引。

全容器部署使用 `docker compose -f docker-compose.yml -f docker-compose.app.yml up -d --build`（app compose 复用 infra compose 中的 postgres/redis）。端口默认错开常见取值：`5434 / 6381 / 19532 / 30010 / 8000`。完整容器清单、生产加固与 Webhook 接入见 [docs/DEPLOY.md](docs/DEPLOY.md)。

无外部依赖模式：`LLM_PROVIDER=mock` 时四维审查完全由 15 条确定性规则完成，RAG 返回抽取式答案，单测链路跳过 LLM；CI 即运行在该模式下（`LLM_PROVIDER=mock`、`SANDBOX_MODE=off`、`EMBEDDING_BACKEND=hash`）。

## 模型配置

LLM 侧只需要 1 个 provider，模型名按用途分为 2 个基础槽位与 5 个可选覆盖槽位，未配置时自动回落：

| 槽位 | 环境变量 | 默认回落 | 使用方 |
|---|---|---|---|
| chat | `LLM_MODEL_CHAT` | `deepseek-chat` | 分诊、RAG 回答、查询改写、意图路由、摘要 |
| code | `LLM_MODEL_CODE` | `deepseek-chat` | 四维审查、单测生成 |
| review | `LLM_MODEL_REVIEW` | code 槽 | 安全 / 性能 / 规范 / 测试四个 reviewer |
| testgen | `LLM_MODEL_TESTGEN` | code 槽 | 单测生成 |
| triage | `LLM_MODEL_TRIAGE` | chat 槽 | PR 分诊 |
| rag | `LLM_MODEL_RAG` | chat 槽 | 问答与 HyDE 改写 |
| router | `LLM_MODEL_ROUTER` | chat 槽 | 统一入口意图分类 |

`LLM_PRICE_INPUT_PER_M` / `LLM_PRICE_OUTPUT_PER_M` 用于成本估算，换模型时同步调整。解析结果可通过 `GET /healthz` 的 `models` 字段查看。

非 LLM 模型独立配置：`EMBEDDING_BACKEND` / `EMBEDDING_MODEL` / `EMBEDDING_DIM`（hash、本地 BGE-M3、OpenAI 兼容），`RERANK_BACKEND` / `RERANK_MODEL`（none、lexical、cross-encoder）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/login` | 登录获取 JWT |
| GET | `/api/v1/auth/me` | 校验令牌 |
| POST | `/api/v1/webhooks/github` | PR webhook（HMAC 验签，202） |
| POST | `/api/v1/reviews` | 提交 diff 审查（202） |
| GET | `/api/v1/reviews/{id}` | 任务状态、findings 与报告 |
| GET | `/api/v1/reviews` | 任务列表 |
| POST | `/api/v1/reviews/{id}/resume` | HitL 恢复（approve / reject / edit） |
| POST | `/api/v1/rag/ingest` | 为本地仓库建立索引 |
| POST | `/api/v1/rag/query` | 代码库问答（带引用） |
| GET | `/api/v1/rag/repos` | 已索引仓库与 chunk 数 |
| POST | `/api/v1/chat` | 统一入口（SSE：route / citations / token / result / done） |
| GET | `/healthz` `/readyz` `/metrics` | 健康检查与指标 |
| GET | `/` | 前端工作台（Vue SPA） |

## 设计原则

1. **确定主干 + 局部自主**。检索是否执行、测试是否生成、沙箱失败是否转人工，都是图上的条件边；只有审查判断本身交给模型。可解释、可测试、成本可控。
2. **规则引擎与 LLM 分工**。密钥泄漏、SQL 字符串拼接、吞异常等机械问题由 15 条规则零成本零抖动处理；LLM 只补语义理解部分，prompt 中显式列出「已发现」项以避免重复告警。
3. **证据即门槛**。LLM 给出的 evidence 必须能在 diff 新增行中逐字命中，否则丢弃并计数，使幻觉率成为可观测指标。
4. **全链路降级**。LLM、Milvus、Redis、Docker、Checkpointer 任一不可用时服务仍可返回结果，响应标记 `degraded` 并给出原因；`/readyz` 暴露依赖状态。
5. **单写者与幂等 reducer**。四个并行维度只写入 `findings`，去重、跨维仲裁与排序在 reducer 中完成一次，相同输入产生相同顺序的报告。
6. **成本闸门**。单维超时、token 与成本预算、`recursion_limit`、回灌 ≤2 轮、幂等键、旧任务取消共同约束单次评审的开销与步数。
7. **安全默认值**。沙箱断网并以最小权限运行；Webhook HMAC 验签；JWT 鉴权与路径白名单；限流按仓库维度 60 秒 30 次。

## 评测

`eval/` 提供人工标注的 diff 样本与评测脚本，可复现规则引擎维度的准确率：

```bash
python -m scripts.eval_review --cases eval/cases.json
```

仓库自带 3 个样本，实测 precision 0.727 / recall 1.000 / F1 0.842。样本量仅能验证评测链路可用，生产结论需以更大规模的标注集支撑；检索 Recall@K 依据 `/api/v1/rag/query` 返回的 citations 判定，单 PR 成本取 `review_tasks.cost_usd` 均值。

## 已知限制与 Roadmap

- [ ] GitLab 适配器（`repos.provider` 已留枚举，暂无实现）
- [ ] 多租户行级安全（当前依靠查询条件隔离）
- [ ] 沙箱独立服务化（当前由 worker 调用宿主机 Docker，权限等价于宿主机 root，仅适用于内网/演示环境）
- [ ] 前端：Markdown 完整渲染、diff 高亮、任务详情分享链接
- [ ] Contextual RAG（chunk 摘要增强）
- [ ] 评测集扩充与 LLM 维度自动化回归

## License

MIT。密钥、`.env.local`、`workspace/` 已在 `.gitignore` 中，不应提交。
