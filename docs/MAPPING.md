# 从 EduAgent 到 CodePilot：能力迁移对照表

> 这份文档解决一个问题：**简历上写的每一条，代码里到底在哪、怎么跑、怎么讲。**
> 原则：只写代码里真实存在的实现；指标必须自己跑出来（见文末「指标怎么算」）。

## 一、课件 10.2.3「研发协作与代码审查平台」→ 本项目

| 课件 Agent | 范式 | 本项目实现 | 代码位置 |
|---|---|---|---|
| ① PR 审查 Agent | 并行评审（第 4 章） | 四维 Send 并行 + 规则引擎 + 证据校验 + 分级聚合 | `backend/agents/review/` |
| ② 代码库 RAG Agent | RAG（第 5 章） | 函数级切分 + 混合召回 + RRF + 精排 + 引用校验 | `backend/agents/qa/` + `backend/core/knowledge_base.py` |
| ③ 测试生成 Agent | 沙箱 + HitL（第 6 章） | 生成 → 沙箱真跑 → 失败回灌 ≤2 轮 → interrupt 人工确认 | `backend/agents/testgen/`、`backend/core/sandbox.py` |
| 意图路由 | 路由分发（8.4） | 规则前置 + LLM 分类 → 主图分支 | `backend/core/orchestrator.py` |
| 统一入口 SSE | 8.5 | 事件流 route/citations/token/stage/result/done | `backend/api/v1/chat.py` |

> 课件里的「④ 缺陷定位 Agent」本项目**没有实现**：简历上没有这条，就不放进来充数。
> 原因也说得通——缺陷定位本质是「栈帧 + 检索 + 排序」，和 RAG 子图高度重合，单独拆一个 Agent 收益不大。

## 二、EduAgent 的地基怎么复用

| EduAgent（第 3 章） | CodePilot | 改动点 |
|---|---|---|
| `config.py`（pydantic-settings） | `backend/config.py` | 加 Redis/Milvus/沙箱/预算/Embedding 配置 |
| `logger.py` | `backend/core/logger.py` | 改成单行 JSON，便于采集 |
| `exceptions.py` | `backend/core/exceptions.py` | 加 `retryable` / `code`，区分可重试 |
| `llm_factory.py` | `backend/core/llm_factory.py` | 加 mock 模式、成本估算、流式 |
| `dependencies.py`（JWT） | `backend/core/security.py` + `dependencies.py` | 拆出 sign/verify，加 webhook HMAC、路径白名单 |
| `init_db.sql` | `scripts/init_db.sql` | 教育表 → 仓库/PR/任务/发现/测试/HitL/索引表 |
| 简历 Agent 六维并行 | 四维并行审查 | 六维打分 → 四级严重度 + 证据 + 阻断门 |

> 你原来的 EduAgent 项目可以原样保留当「课程作业」，CodePilot 是从它长出来的「简历项目」。
> 面试被问「这两个项目什么关系」时，标准答法：先讲 EduAgent 学的四种范式，再讲怎么把它们迁移到研发场景。

## 三、简历每条 → 代码证据

### 1. 多 Agent 编排
> 「LangGraph『主图 + 3 子图』+ Send 并行扇出 + 条件边路由 + checkpoint/interrupt 断点恢复…结构化信封协议、单写者约束、预算限制」

| 简历说法 | 代码 | 怎么演示 |
|---|---|---|
| 主图 + 3 子图 | `orchestrator/graph.py`（route → review/qa/test） | `POST /api/v1/chat` 发不同消息看 `route` 事件 |
| Send 并行扇出 | `review/nodes.py::fanout_dimensions` | 日志里 4 个维度几乎同时开始 |
| 条件边路由 | `route_after_aggregate` / `route_after_sandbox` | 有/无新增函数走不同分支 |
| checkpoint + interrupt | `review/nodes.py::human_gate`、`db/session.py` | `status=waiting_human` → `/resume` 继续 |
| 单写者 + reducer | `state.py::merge_findings` | `tests/test_state.py` |
| 预算 / 无进展检测 | `budget`、`recursion_limit`、`testgen_rounds` | 超预算降级日志 `degraded:budget` |

> 注意：本项目用「预算 + 步数 + 回灌轮次」实现防死循环，比「无进展检测」更简单直接。
> 面试别硬说「无进展检测算法」，说清楚你实际用了哪三种闸门即可。

### 2. PR 审查
> 「Send 四路并行、结构化输出、diff 证据校验与分级聚合…block 级误报 23% → 12%、召回 79%」

| 简历说法 | 代码 |
|---|---|
| 四路并行 | `review/graph.py` + `fanout_dimensions` |
| 结构化输出 | `FindingList` + `with_structured_output` |
| diff 证据校验 | `rules.py::validate_evidence`（丢弃幻觉证据并计数） |
| 分级聚合 + block 阻断 | `state.py::summarize` → `mergeable` |
| 误报治理 | 规则去重 + 跨维仲裁 `_arbitrate` + 置信度 |

### 3. 代码库 RAG
> 「tree-sitter 函数级切分 + BGE-M3 混合召回 + RRF/Rerank + Contextual RAG…Recall@5 0.87、引用准确率 96%」

| 简历说法 | 代码 | 说明 |
|---|---|---|
| tree-sitter 函数级切分 | `rag/chunking.py::_ts_chunks` | 未装依赖自动退回正则，两条路径同结构 |
| 混合召回 | `rag/retrieval.py`（dense + PG 全文） | Milvus 挂了自动降级 |
| RRF | `_rrf` / fused dict | k=60 |
| Rerank | `_rerank`（cross-encoder / lexical） | 默认 lexical，装模型后切 cross-encoder |
| 引用可溯源 | `_citations` + `citation()` | 引用不存在的编号计入 `invalid_citations` |

> Contextual RAG（chunk 前拼接文件/类上下文）在本项目里用「embedding 文本 = path + symbol + content」近似实现；
> 如果你要在简历保留「Contextual RAG」这个词，建议补一个 LLM 生成 chunk 摘要的步骤，或在面试里如实说明这是轻量版。

### 4. 沙箱测试
> 「diff 识别新增函数 + LLM 生成单测 + Docker 沙箱断网执行 + 覆盖率校验 + 失败回灌（≤2 轮）+ 人工确认合入」

全部有对应实现：`added_python_symbols` / `testgen` / `SandboxRunner` / `_collect`（覆盖率解析）/ `route_after_sandbox` / `human_gate`。
覆盖率数字来自沙箱内 `pytest --cov`，不是估算。

## 四、指标怎么算（别抄简历上的数字）

简历上的 `3.8h → 1.2h`、`召回 79%`、`误报 12%` 是「面试稿」里的设定值。**真实项目必须自己跑**：

```bash
# 1) 规则/LLM 审查的 precision / recall（需要人工标注 diff）
python -m scripts.eval_review --cases eval/cases.json --out eval/report.json

# 2) 检索质量（Recall@K）：准备 20 个「问题 → 正确文件」样本
#    在 /api/v1/rag/query 返回的 citations 里看目标文件是否在 Top-5

# 3) 成本：review_tasks.cost_usd 汇总，按 PR 数取平均
SELECT ROUND(AVG(cost_usd), 4), COUNT(*) FROM review_tasks WHERE status = 'done';

# 4) 评审时长：webhook 创建时间 → finished_at
SELECT ROUND(AVG(EXTRACT(EPOCH FROM (finished_at - created_at)) / 60), 2) AS minutes
FROM review_tasks WHERE status = 'done';
```

建议在 README 里写「本地 30 个 PR 样本实测：召回 X%、误报 Y%、单 PR 成本 Z 元」，
并附上 `eval/report.json`。**没有数据集就不要写百分比**，写「显著降低」也比编数字安全。

## 五、面试被追问时的三个诚实点

1. **「4 维并行」不是 4 个独立模型**：同一模型 + 不同 prompt + 不同规则子集，并行是为了延迟与隔离，不是为了「多模型投票」。
2. **没有接 GitLab**：`core/github.py` 只有 GitHub 实现，`repos.provider` 留了 gitlab 枚举但没有适配器。被问就说「抽象了 provider 字段，GitLab 适配器是下一步」。
3. **没有 K8s / 多租户硬隔离**：`tenant_id` 贯穿所有表，但隔离靠查询条件，不是行级安全（RLS）。生产化时应该加 RLS。
