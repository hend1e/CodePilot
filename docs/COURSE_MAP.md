# 课件 ↔ 代码对照表

> 目标：**看到课件第 N 章，就能在仓库里找到对应文件**。
> 目录结构和课件保持一致：`api/` 接入层、`agents/` 业务 Agent、`core/` 通用能力、`db/` 存储。

## 一、章节对照

| 课件章节 | 本项目文件 | 本项目做了什么 |
|---|---|---|
| 3.1 环境与基础设施 | `docker-compose.all.yml`、`.env.example` | PG/Redis/Milvus/MinIO/etcd + api/worker |
| 3.2 数据库设计与建表 | `scripts/init_db.sql`、`backend/db/migrations.py` | 11 张表：用户/仓库/PR/任务/发现/测试/HitL/索引 |
| 3.3 配置、日志与异常 | `backend/config.py`、`core/logger.py`、`core/exceptions.py` | pydantic-settings + JSON 日志 + 可重试异常 |
| 3.4 LLM Factory | `backend/core/llm_factory.py` | 按 Agent 槽位路由模型、成本估算、mock 模式 |
| 3.5 三层兜底与重试 | `backend/core/retry.py` + 各节点的 `try/except` | 指数退避 + 单维超时 + 图级降级标记 |
| 3.6 认证与依赖注入 | `core/security.py`、`dependencies.py`、`api/v1/auth.py` | JWT + bcrypt + Bearer 依赖 |
| 4.2 State 与数据模型 | `agents/review/state.py` | `Finding` + reducer 去重 + 严重度排序 |
| 4.3 提示词 | `agents/review/prompts.py` | 四维系统提示 + 分诊提示 |
| 4.4 PDF 解析 | `agents/review/diff.py` | **换成 diff 解析**（本项目输入是代码改动） |
| 4.5 六维并行评审 | `agents/review/nodes.py::fanout_dimensions`、`reviewers.py`、`rules.py` | Send 四路并行 + 15 条规则 + LLM 结构化输出 |
| 4.6 问题诊断与整体评价 | `agents/review/nodes.py::aggregate` | 跨维仲裁、阻断门、`mergeable` |
| 4.7 持久化与图装配 | `agents/review/persist.py`、`agents/review/graph.py` | 幂等落库 + 条件边 + HitL 挂起点 |
| 4.8 API 接口 | `api/v1/reviews.py` | 创建/查询/恢复任务 |
| 5.2 文档读取 | `core/chunking.py::iter_repo_files` | 扩展名白名单 + 跳过依赖目录 |
| 5.3 智能分块 | `core/chunking.py::chunk_file` | tree-sitter 函数级（缺失退化正则） |
| 5.4 BGE-M3 嵌入 | `core/embedding.py` | hash / local(BGE-M3) / openai 三种后端 |
| 5.5 Milvus 初始化与写入 | `core/knowledge_base.py::MilvusStore`、`ingest_repo` | 懒连接 + 熔断 + PG/Milvus 双写 |
| 5.6 Hybrid 召回 | `core/knowledge_base.py::_dense/_sparse` | Milvus + PG `tsvector`，RRF 融合 |
| 5.7 重排序 | `core/reranker.py` | cross-encoder / lexical / none |
| 5.8 意图分类 | `core/query_classifier.py` | 规则分类（locate/howto/concept），决定 HyDE |
| 5.9 记忆管理 | `core/memory.py` | Redis 滚动窗口 + 摘要，按 `thread_id` |
| 5.10 MCP 工具 | `backend/mcp/`（**未启用**） | 见「差异」一节 |
| 5.11 State 与 Prompts | `agents/qa/state.py`、`agents/qa/prompts.py` | 问答子图状态与提示词 |
| 5.12–5.14 三个节点 | `agents/qa/nodes.py` | `rewrite` / `retrieve` / `answer` + `persist` |
| 5.15 图装配 | `agents/qa/graph.py` | 线性图 + `ask()` 便捷入口 |
| 5.16 HTTP 接口 | `api/v1/qa.py` | 建索引 / 问答 / 仓库列表 |
| 6.2 State 与 Prompts | `agents/testgen/state.py`、`agents/testgen/prompts.py` | 单测生成状态与提示词 |
| 6.3 Word 文件解析 | — | 本项目输入是 diff，不需要 |
| 6.4 题目元数据加载 | `agents/review/diff.py::added_python_symbols` | 从 diff 提取新增函数/类 |
| 6.5–6.7 三轨并行 | `agents/review/rules.py`（规则轨）+ `reviewers.py`（LLM 轨）+ `agents/testgen/nodes.py`（代码轨） | 三个能力都在，只是按研发场景重组 |
| 6.8–6.10 汇总 / HitL / 合并 | `agents/testgen/nodes.py::gate`、`core/review_service.py::resume_task` | `interrupt` + `Command(resume=...)` |
| 6.11 图装配 | `agents/testgen/graph.py` | generate → sandbox → 回灌 → gate |
| 6.12 HTTP 接口 | `api/v1/reviews.py::resume_review` | 人工确认恢复 |
| 7 模拟面试 Agent | — | **未实现**（简历没有这条） |
| 8.2 Schema 与单 Agent 直达 | `core/orchestrator.py` | 意图 → 直接进对应子图 |
| 8.3 多 Agent 串联 Pipeline | `core/orchestrator.py::review_branch` | 审查子图内部串了 testgen → sandbox → HitL |
| 8.4 前置拦截与 LLM 路由 | `core/orchestrator.py::rule_route/classify` | PR 链接、关键词零 token 拦截 |
| 8.5 SSE 分发 | `api/v1/unified_chat.py` | route / citations / token / result / done |
| 8.6 路由聚合与 main 集成 | `api/router.py`、`backend/main.py` | 一个 `api_router` 挂载全部 v1 |
| 8.7 端到端测试 | `scripts/demo_review.py` | 登录 → 提交 diff → 轮询报告 |
| 9 前端集成 | `frontend/` | Vue3 + Vite + SSE（FastAPI 同源托管） |
| 10.2 能力迁移方法论 | `docs/MAPPING.md` | 课件场景 → 研发场景的逐条映射 |

## 二、四种范式落在哪

| 范式 | 课件出处 | 本项目落地 |
|---|---|---|
| 并行评审 | 第 4 章 | `agents/review`：`Send` 四路 fan-out + reducer |
| RAG | 第 5 章 | `core/knowledge_base.py` + `agents/qa` |
| Human-in-the-Loop | 第 6 章 | `agents/testgen/nodes.py::gate` + `review_service.resume_task` |
| 路由 / 编排 | 第 8 章 | `core/orchestrator.py` + `api/router.py` |

## 三、目录职责

```
backend/
  main.py              FastAPI 入口（lifespan：建表 → checkpointer → 内嵌 worker）
  worker.py            队列消费者（独立进程，docker-compose 里是 worker 容器）
  config.py            pydantic-settings，全部环境变量
  dependencies.py      get_current_user（JWT Bearer）
  api/
    router.py          聚合 v1 路由（对应课件 8.6）
    v1/
      auth.py          登录 / me
      reviews.py       审查任务：创建、查询、HitL 恢复
      qa.py            知识库：建索引、问答（对应课件 5.16）
      unified_chat.py  统一入口 SSE（对应课件 8.5）
      webhooks.py      GitHub webhook（验签 + 幂等 + 入队）
      health.py        healthz / readyz / metrics
  agents/
    review/            并行审查 Agent（课件第 4 章范式）
      state.py  prompts.py  nodes.py  graph.py
      rules.py  diff.py  reviewers.py  persist.py
    qa/                代码库问答 Agent（课件第 5 章）
      state.py  prompts.py  nodes.py  graph.py
    testgen/           单测生成 Agent（课件第 6 章）
      state.py  prompts.py  nodes.py  graph.py
  core/                通用能力（对应课件 core/）
    logger.py exceptions.py llm_factory.py retry.py security.py
    knowledge_base.py chunking.py embedding.py reranker.py
    query_classifier.py memory.py orchestrator.py
    queue.py sandbox.py github.py rate_limit.py
    review_service.py metrics.py
  db/
    session.py         asyncpg 引擎 + AsyncPostgresSaver
    migrations.py      启动时幂等补丁
  mcp/                 预留目录（见下）
```

## 四、和课件的差异（主动说明，别硬凑）

1. **MCP（5.10）没有启用**：`backend/mcp/` 只有占位 `__init__.py`。课件的 MCP 是给 QA Agent 挂 web 搜索/知识库工具；本项目的检索已经内聚在 `core/knowledge_base.py`，没有额外拆 MCP Server。
2. **没有模拟面试 Agent（第 7 章）**：简历里没有这条能力，不做。
3. **没有 PDF / Word 解析**：输入从「简历 PDF / 试卷 Word」换成「git diff / 代码仓库」，解析层变成 `agents/review/diff.py`。
4. **多了一层队列 + worker**：课件是 API 内直接跑图；本项目要接 GitHub webhook（必须 202 立即返回），所以加了 Redis Streams + 独立 worker。单机调试可 `INLINE_WORKER=true` 回到课件那种单进程模式。
5. **六维 → 四维**：简历 Agent 的六维是「简历质量」，这里换成代码的「安全/性能/规范/测试」，机制（并行 + reducer + 加权）完全一样。

## 五、按课件复习的 30 分钟路线

| 步骤 | 看课件 | 看代码 | 验证命令 |
|---|---|---|---|
| 1 | 3.4 / 3.5 | `core/llm_factory.py`、`core/retry.py` | `curl localhost:8000/healthz` |
| 2 | 3.6 | `core/security.py`、`api/v1/auth.py` | `POST /api/v1/auth/login` |
| 3 | 4.2–4.7 | `agents/review/*` | `python -m scripts.demo_review` |
| 4 | 5.3–5.7 | `core/chunking.py`、`core/knowledge_base.py`、`core/reranker.py` | `POST /api/v1/rag/query` |
| 5 | 5.8–5.9 | `core/query_classifier.py`、`core/memory.py` | 同一个 `thread_id` 连问两次 |
| 6 | 6.9 | `agents/testgen/nodes.py::gate` | 有 LLM + Docker 时触发 `waiting_human` |
| 7 | 8.4–8.6 | `core/orchestrator.py`、`api/router.py` | `POST /api/v1/chat`（看 `route` 事件） |
| 8 | 9 | `frontend/src/` | 打开 <http://localhost:8000/> |
