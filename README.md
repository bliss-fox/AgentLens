# AgentLens：可复现、可解释的 AI Agent 评测平台

> 不只判断 Agent 的最终答案“看起来是否正确”，而是回答：**这个 Agent 是否稳定到可以上线，失败发生在哪一步，结论能否复现？**

AgentLens 是一个面向工具型 Agent 的全栈评测系统。它冻结任务、候选版本、工具环境和随机种子，重复运行候选与基线，通过严格的 HTTP/SSE 轨迹协议采集证据，并分别报告成功率、稳定性、轨迹质量、成本与失败归因。

[![CI](https://github.com/bliss-fox/AgentLens/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/bliss-fox/AgentLens/actions/workflows/ci.yml)

本仓库公开的数值结果来自**确定性的 scripted fixture（合成评测器自测）**，用于验证评测、统计、归因和报告链路能否稳定复现；它们不是 DeepSeek、OpenAI 或其他真实模型的效果 benchmark。真实模型候选与语义 Judge 的接入路径已经实现并由替身客户端测试，但当前仓库尚未发布可核验的真实模型实验 artifact，因此只声明“已实现 / 支持”，不声明“已验证 / 已达到”。

## 面试官 30 秒速览

| 关注点 | 项目中的实现 | 可验证结果 |
| --- | --- | --- |
| 评测器是否可复现 | 固定 6 个任务、10 个种子、工具 cassette、候选指纹与 bootstrap seed | 无模型 Key、无外网也可重放 120 次 scripted 候选/基线运行 |
| 合成结果是否稳定 | 分任务 Wilson 区间、分层 bootstrap、同任务同种子的配对 A/B bootstrap | scripted candidate fixture 成功率 81.7%，与 baseline fixture 的配对差值为 16.7 个百分点 |
| 失败是否可定位 | 保存 typed trace event，并用确定性规则定位事件区间、规则 ID 与严重级别 | 候选与基线共 120 次运行持久化 966 个事件，可回查双方原始轨迹 |
| Judge 结果是否可区分 | 离线演示使用 24 条固定校准 fixture；未运行语义 Judge 时明确标记 `not_run` | fixture agreement 为 22/24；该数字不是外部 Judge 模型准确率 |
| 工程链路实现到哪里 | React 19 控制台 + FastAPI + PostgreSQL + Redis/ARQ + OpenAI-compatible 候选适配器 + 受限工具容器 | 后端 203 项、候选/工具服务 10 项测试通过；前端 7 项测试、ESLint 与生产构建通过；Ubuntu Compose E2E 已使用 fake provider 公开验证 |

完整机器可读证据见 [`evidence/offline-benchmark.json`](evidence/offline-benchmark.json)，架构边界与演进顺序见 [`docs/architecture.md`](docs/architecture.md)，面试演示与追问索引见 [`docs/interview-guide.md`](docs/interview-guide.md)。

## 公开证据状态

| 能力 | 状态 | 公开证据 | 证据能证明什么 |
| --- | --- | --- | --- |
| 合成评测器自测 | 可复现 | `demo.py`、`evaluation.py`、自动化测试与固定运行摘要 | 评测、配对比较、失败归因和报告生成可以离线复现 |
| 真实模型候选接入 | 已实现、已使用替身测试 | OpenAI/DeepSeek-compatible 适配器、严格 SSE 与工具调用测试 | 接入路径和协议行为已经实现；不证明真实模型质量 |
| 语义 Judge 接入 | 已实现、已使用替身测试 | provider adapter、租约、引用和原子写回测试 | Judge 编排和安全合同已经实现；不证明外部 Judge 准确率 |
| 真实模型实验结果 | 未发布 | 暂无脱敏的运行配置、原始输出与报告 artifact | 本仓库不对真实模型成功率或版本提升作结果声明 |
| PostgreSQL + Redis/ARQ Compose E2E | 已公开验证（fake provider） | [CI run 35718661033](https://github.com/bliss-fox/AgentLens/actions/runs/35718661033) 与 `agentlens-compose-verification` artifact | 证明 Ubuntu 上的 migration、PostgreSQL、Redis/ARQ、HTTP/SSE、取消、Tool Gateway、Web 入口和清理链路；不证明真实模型质量 |

### 60 秒证据入口

1. [架构与数据流](#架构与数据流)：先确认 API、Worker、Candidate、Tool Runner、PostgreSQL 与 Redis 的进程边界。
2. [`evidence/offline-benchmark.json`](evidence/offline-benchmark.json)：检查固定任务、seed、统计参数、cassette 指纹和可复现 digest。
3. [公开 Compose E2E](https://github.com/bliss-fox/AgentLens/actions/runs/35718661033)：查看 Linux runner 上完整 fake-provider 栈的成功步骤与机器可读 artifact。

本文使用以下证据用词：**已实现 / 支持**表示源码路径存在；**已测试**表示自动化测试覆盖该路径，测试可能使用本地依赖或替身；**已验证**表示已针对所述真实依赖执行并公开保存 artifact；**已达到**只用于绑定了数据集、配置和 artifact 的测量结果。

## 为什么做这个项目

普通 Agent Demo 往往只展示一次“成功案例”，但上线判断还需要解决四个问题：

1. 同一个任务重复运行时，成功率是否稳定？
2. 新版本的提升来自真实能力，还是随机波动？
3. 最终答案失败时，是工具误用、上下文丢失、循环、超时，还是环境本身不可用？
4. 评测结论能否在没有线上模型与真实外部服务的环境中复现？

AgentLens 的核心取舍是：**将结果正确性、轨迹质量、稳定性、成本与失败证据分开呈现，不用一个不透明的总分掩盖弱项。**

## 架构与数据流

```mermaid
flowchart LR
    UI["React 评测工作台"] --> API["FastAPI：HTTP 202 + typed SSE"]
    API --> DB["PostgreSQL / SQLite\n实验事实来源"]
    API --> LOCAL["本地独立线程"]
    API --> QUEUE["Redis + ARQ"]
    QUEUE --> WORKER["ARQ Worker"]
    LOCAL --> TRIALS["候选与基线重复试验"]
    WORKER --> TRIALS
    TRIALS --> AGENT["被测 Agent：HTTP/SSE 协议"]
    AGENT --> MODEL["OpenAI / DeepSeek（按配置调用）"]
    AGENT --> GATEWAY["cassette 工具网关"]
    GATEWAY --> RUNNER["受限 Python 工具容器"]
    TRIALS --> EVAL["统计、归因与对比"]
    TRIALS --> DB
    EVAL --> DB
    DB --> STREAM["真实状态/进度 SSE"]
    STREAM --> UI
```

实验使用持久化状态机：`queued → running → completed|failed|cancelled`。汇总进度与 SSE 都按候选和基线的总工作量计数（默认 120），两侧统计样本则分别从各自 60 条运行列表计算，避免进度与样本口径混用。进度写入在数据库事务内拒绝负数、超额、总量变化和完成数回退，确保 REST/SSE 只传播单调可信状态。POST 只创建排队记录并返回 HTTP 202；本地开发由独立线程执行，Docker 由 Redis/ARQ Worker 执行。Worker 与 API 不共享进程内对象，均通过数据库读取请求、抢占状态和写入进度，因此跨进程查询、取消和 SSE 使用同一事实来源。Redis job 仅携带实验 ID；滚动升级期间即使收到旧 payload，Worker 也不会用它覆盖持久请求。入队事务会深拷贝并保存完整 `TaskSpec` 列表，同时记录 benchmark、任务集、离线校准 fixture、评测器和价格表版本；Worker 只执行该快照，并要求其与持久进度总量、排队摘要和当前受支持合同一致，否则以 `invalid_execution_state` fail-closed。每个 `running` 实验还会在数据库保存执行心跳：原子抢占与每条试验进度都会续期；300 秒未续期时，`/health` 或 SSE 会将其收敛为脱敏的 `worker_lost` 失败，避免进程硬中断后永久挂起。

ARQ 模式的 `/health` 同时校验数据库连接与 migration revision、Redis `PING` 和带 TTL 的 ARQ Worker 心跳；它还会分批锁定已到期 grant、只清理超过保留期的 `expired/revoked` 记录，并在 `storage.tool_grants` 返回状态计数、本次标记/清理数、保留期与批量上限。默认保留审计记录 7 天、每次最多处理 1000 条，未到期的 active grant 不进入删除候选集。数据库不可用、Redis 不可用或 Worker 心跳缺失时均返回脱敏的 HTTP 503 与 `status=degraded`，并通过 `database_status`、`queue_status`、`worker_status` 指出故障边界。本地线程启动失败时会回滚线程注册并返回结构化 `executor_unavailable` 503；入队连接失败或任务被队列拒绝时返回 `queue_unavailable|queue_rejected` 503；这些路径都会将已创建实验持久化为 `failed`；前端会显示可行动提示和失败实验 ID，SSE 仍可读取脱敏的 `experiment.error`；事件会附带可用的权威 `failed` 实验快照，前端在同一事件回调中更新证据面板，避免只停止动画却保留旧的 `queued/running` 状态。

### 关键设计

- **严格轨迹协议**：事件序号必须从 0 连续递增；首事件必须是 `run.started`；末尾必须是 `final|error → run.completed`。非精确 `text/event-stream` MIME、乱序、重复或缺失终止事件都会归一化为协议错误；墙钟、Token、成本、工具调用、事件数和响应字节均受预算限制。
- **环境隔离**：工具调用通过冻结 cassette 回放；入队时持久化环境版本以及 `mode + records` 的规范化 SHA-256，Worker 抢占前重算合同，并通过 `AgentRunRequest` 下发摘要。被测 Agent 每次 replay 都必须回传 `cassette_content_sha256`，网关在读取响应前复验；同名内容漂移、换绑或缺失会以 `invalid_execution_state` 或 HTTP 424 拒绝，绝不静默访问真实网络。
- **公平 A/B**：候选与基线共享任务、种子和环境快照，使用配对 bootstrap 估计差值区间，而不是只比较两个点估计。
- **证据优先的归因**：确定性规则输出事件范围、规则 ID、严重级别和解释；可选 Judge 只在用户显式触发时复核，引用必须落在真实持久化事件范围内，不把参考轨迹或 LLM 判断当作唯一真值。
- **诚实的成本统计**：只有收到 `usage` 事件才计算成本；数据不完整时明确标记，不做静默插补。`cached_tokens` 必须是 `input_tokens` 的子集，缓存输入只按缓存价计费，不与完整输入价重复累计。
- **跨进程异步执行**：本地线程与 ARQ Worker 使用同一数据库状态机，逐条运行写入候选与基线的持久化工作进度；重复 Worker 只能由 `queued` 原子抢占一次。合法最大评测的 ARQ timeout 按 50 次重复、6 个任务、候选/基线和单次 180 秒预算计算，不再受默认 300 秒任务超时误杀；Worker 协程取消会写入 `worker_interrupted` 终态，底层 `to_thread` runner 将任意非 `running` 权威状态作为协作停止信号，取消处理最多等待 2 秒完成线程回收。执行前会验证请求、候选、基线、完整任务快照、版本化评测合同与环境快照的一致性，损坏或不受支持的状态以脱敏错误落库，不会永久滞留在队列中。
- **流式与取消语义**：实验进度使用异步 SSE；同步 SQLAlchemy 读写通过工作线程卸载，取消、客户端断开和生产者异常都有独立终止语义，异常响应不会泄露内部 traceback；FastAPI 关闭时会先原子取消本地线程对应的实验，再限时等待线程退出。

## 可复现的合成评测器自测

> **证据边界：**以下结果来自仓库内的 deterministic scripted fixture。每个 candidate、task 和 seed 的失败模式由固定 fixture 定义，不涉及真实 DeepSeek/OpenAI 模型推理。这组结果用于回归验证轨迹分析、失败归因、配对统计、报告生成和运行摘要；不能用于证明某个真实模型或 Agent 版本的效果提升。

离线合成基准在 Windows 11、Python 3.12.13 上生成，外部 API 调用数为 0。

| scripted fixture 指标 | candidate fixture v1.4 | baseline fixture v1.3 |
| --- | ---: | ---: |
| 成功次数 | 49 / 60 | 39 / 60 |
| 成功率 | 81.7% | 65.0% |
| 95% 分层 bootstrap 区间 | [71.7%, 90.0%] | [53.3%, 76.7%] |
| 平均轨迹分 | 0.903 | 0.803 |
| 平均工具调用数 | 2.1 | 2.0 |
| 平均成本 | ¥0.039 / 任务 | ¥0.038 / 任务 |

scripted candidate fixture 与 baseline fixture 的配对差值为 **+16.7 个百分点**，95% 区间为 **[+0.8, +31.7] 个百分点**。在该固定 fixture 内，区间下界高于 0；这只验证配对统计与报告判定能够按预期工作，不表示任何真实模型或生产 Agent 获得了 16.7 个百分点的提升。

## Quick Start：三条独立验证路径

### A. Offline evaluator self-test

不需要 API Key、Docker 或外部网络。这条路径只验证 synthetic evaluator、统计、failure rule、报告和 digest，不代表真实模型效果。

PowerShell：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts\run_offline_benchmark.py `
  --output "$env:TEMP\agentlens-offline-benchmark.json"
```

Linux / macOS：

```bash
cd backend
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[dev]"
./.venv/bin/python scripts/run_offline_benchmark.py \
  --output "${TMPDIR:-/tmp}/agentlens-offline-benchmark.json"
```

输出应包含固定 6 个任务、候选/基线各 60 次运行、失败分类、配对 bootstrap、24 条校准 fixture、持久化计数和 `run_digest_sha256`，并明确记录 `external_api_calls=0`。

### B. Full-stack fake-provider demo

要求：Docker Desktop 的 Linux 容器引擎可用。在 Windows 上需要启用 WSL 与
`Virtual Machine Platform`；首次启用后必须重启 Windows。

从仓库内样例创建本地配置。`.env` 已被 Git 忽略；不要把 API Key 写入仓库或命令行历史。

```powershell
# PowerShell
Copy-Item .env.example .env
```

```bash
# Linux / macOS
cp .env.example .env
```

样例默认 `CANDIDATE_FAKE_MODEL=true`，因此不需要 API Key。启动前只做静默配置校验，避免把展开后的环境变量和 secret 打印到终端或 CI 日志：

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/health
```

Linux / macOS 可用 `curl --fail http://127.0.0.1:8000/health` 检查健康状态。Compose 会真实启动 Web、API、ARQ Worker、PostgreSQL 16、Redis 7、Candidate 和 Tool Runner。

运行仓库自带的跨进程验收器：

```powershell
docker compose exec -T `
  -e AGENTLENS_VERIFY_OUTPUT=/tmp/agentlens-compose-verification.json `
  api python scripts/verify_compose_stack.py
```

打开 <http://127.0.0.1:5173> 查看由 API 持久化的实验。这条路径验证 PostgreSQL、Redis/ARQ、HTTP/SSE、取消、Candidate readiness、工具授权和 Tool Runner 调用；fake adapter 的任务结果不代表真实模型质量。

### C. Real-model run

仍然只使用仓库根目录的 `.env`。将 fake 模式关闭，并只填写所选供应商的 Key；另一供应商的 Key 保持为空。

DeepSeek：

```dotenv
CANDIDATE_FAKE_MODEL=false
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
DEEPSEEK_API_KEY=<your-key>
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
JUDGE_PROVIDER=deepseek
JUDGE_MODEL=deepseek-chat
```

OpenAI 或 OpenAI-compatible endpoint：

```dotenv
CANDIDATE_FAKE_MODEL=false
LLM_PROVIDER=openai
LLM_MODEL=<model-name>
OPENAI_API_KEY=<your-key>
OPENAI_BASE_URL=https://api.openai.com/v1
JUDGE_PROVIDER=openai
JUDGE_MODEL=<judge-model>
```

`JUDGE_*` 只配置按需语义复核；后台评测不会自动调用 Judge。启动并同步 Candidate 运行时身份：

```powershell
docker compose config --quiet
docker compose up -d --build
Invoke-RestMethod `
  http://127.0.0.1:8000/api/v1/candidates/coding-assistant-v1/test `
  -Method Post
```

Linux / macOS：

```bash
docker compose config --quiet
docker compose up -d --build
curl --fail -X POST \
  http://127.0.0.1:8000/api/v1/candidates/coding-assistant-v1/test
```

readiness 响应会给出 provider、model、`prompt_hash`、`scaffold_version`、`tool_schema_hash` 和价格配置，并同步到持久化 Candidate。随后在 <http://127.0.0.1:5173> 选择 `AI Coding Assistant` 与 `Coding Agent 核心任务集 / v1`，先“测试连接”，再运行 smoke。实验快照会保存已同步的 Candidate 身份、任务、环境和运行配置；在另行发布脱敏 artifact 前，这仍只是本地 real-model run，不是公开 benchmark。

一次性 `migrate` 服务会先执行 `alembic upgrade head`；Worker 在迁移成功且 Redis
健康后启动，API 等待 Worker 健康后启动，Web 再等待 API 健康。ARQ 模式
`/health` 会校验数据库 revision、Redis readiness 与 Worker freshness。

### 本地开发

后端：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn agentlens.api:app --reload --port 8000
```

前端（另一个终端）：

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

打开 <http://localhost:5173>。本地模式使用共享内存 SQLite 与独立执行线程；Docker 模式切换为 PostgreSQL、Redis 与独立 ARQ Worker。可选配置见 [`.env.example`](.env.example)。

### 内置模型供应商适配器与自定义 HTTP Agent

Compose 中的 `candidate-agent` 实现了 OpenAI-compatible 适配器，支持 OpenAI 与
DeepSeek Tool Calling；自动化测试使用替身客户端，不构成真实模型效果验证。前端创建
的生产测评固定使用 `execution_mode=http`，不会静默切换 scripted fixture。适配器把模型
工具调用路由到 AgentLens 授权网关，并输出严格的 typed SSE；DeepSeek 请求不会发送未
文档化的 `seed` 参数，但 trial seed 仍会进入实验快照与轨迹，供重复运行分组和统计使用。

要验证其他自定义 HTTP/SSE Agent，可先在仓库根目录启动合同示例：

```powershell
.\backend\.venv\Scripts\python.exe -m uvicorn scripted_agent:app --app-dir examples --port 8001
```

启动 API 前配置候选端点；候选与基线可以指向不同服务：

```powershell
$env:AGENT_ENDPOINTS='{"support-v1.4":"http://127.0.0.1:8001","support-v1.3":"http://127.0.0.1:8001"}'
cd backend
.\.venv\Scripts\python.exe -m uvicorn agentlens.api:app --port 8000
```

`AGENT_ENDPOINTS` 是给内置候选补端点的简写。评估任意候选时，应通过 `CANDIDATE_OVERRIDES` 提供完整、可审计的身份元数据；映射键必须和内部 `id` 一致：

```powershell
$env:CANDIDATE_OVERRIDES=@'
{"remote-v2":{"id":"remote-v2","name":"Remote Support Agent","version":"2026.08","model":"provider/model-v2","model_parameters":{"temperature":0.1},"prompt_hash":"sha256:replace-with-real-hash","scaffold_version":"support-flow@2.0.0","tool_schema_hash":"sha256:replace-with-real-hash","endpoint":"http://127.0.0.1:8001"}}
'@
```

动态候选只允许用于 `http` 模式；scripted fixture 仅保留给离线回归和可复现证据，
不是产品界面的默认数据源。端点必须是无内嵌凭据、query 或 fragment 的绝对
HTTP(S) URL。

提交一轮 HTTP 模式实验：

```powershell
$body = @{ prompt='HTTP Agent 合同验证'; execution_mode='http'; repetitions=1 } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/v1/experiments -Method Post -ContentType application/json -Body $body
```

缺失或非法端点会在入队前返回结构化 HTTP 422。运行中的网络、HTTP、协议、预算和超时错误会保存为失败轨迹；取消实验会终止正在等待的 Agent 请求。

## 按需语义 Judge 复核

确定性规则始终先生成失败类别、事件范围和解释，语义 Judge 不会在创建实验、后台执行或页面加载时自动运行。只有用户在失败证据面板点击“语义复核”，或显式调用下列 API，系统才会把指定失败及其归因范围内的持久化事件发送到配置的 OpenAI-compatible endpoint；这可能产生模型费用，并应按所用端点的数据政策处理评测内容。

```powershell
$env:OPENAI_BASE_URL='https://api.openai.com/v1'
$env:OPENAI_API_KEY='在本地环境安全设置，不要写入仓库'
$env:JUDGE_MODEL='gpt-4.1-mini'
$env:JUDGE_MAX_CONTEXT_BYTES='131072'
Invoke-RestMethod `
  'http://127.0.0.1:8000/api/v1/experiments/exp-demo-0248/runs/<run-id>/failures/0/review' `
  -Method Post
```

`failure_index` 从 0 开始。数据库租约会在同一事务中核对 `ExperimentRecord.metrics.summary`、`RunRecord.result` 与独立 `EventRecord` 中该失败范围的完整事件（包括 span lineage）；API 只从核验后的租约上下文组装事件，并在创建外部客户端前递归脱敏 `authorization`、`api_key`、`password`、`token`、cookie 等凭据形态字段，再按 UTF-8 字节数执行 `JUDGE_MAX_CONTEXT_BYTES` 上限；其他评测内容仍会发送。Judge 必须引用输入中真实存在的事件序号；引用会去重排序。`supported=true` 且类别一致时记为 `support`，否则记为 `conflict`。确定性 `explanation` 永远保留，Judge 的解释与事件引用写入独立字段；实验摘要和单次运行审计记录在同一数据库事务内同步更新，失败类别计数不会被语义复核改写。

未配置 Key 时返回 `judge_unavailable` 503 且不创建外部客户端；实验未完成时返回 `review_not_ready` 409；上下文超限在外部调用前返回 `judge_context_too_large` 413；超时返回 `judge_timeout` 504；非法结构化结果或事件引用返回 `judge_invalid_response` 502。上述失败都不会覆盖原有 `not_run`，外部异常详情也不会回传给客户端。

每次可能产生费用的调用都会先核对实验摘要、独立运行记录和事件审计行一致，再在实验记录中原子领取带过期时间的复核租约。OpenAI 客户端显式设置 `max_retries=0`，一次复核请求不会在 SDK 内部扩张成多次外部请求；可重试错误只通过结构化 `retryable` 返回给调用方。同一失败已有活动请求时返回 `review_in_progress` 409，不会调用第二次模型；已有复核结果时，普通 POST 返回 `review_already_exists` 409。只有显式追加 `?force=true` 才会重新调用并更新 Judge 字段，前端会再次确认费用。模型失败或引用无效会释放租约，进程崩溃遗留租约则在 Judge 超时加 30 秒后允许接管；旧请求的 token 不能释放或提交后来请求的租约。

## 被测 Agent 接入协议

AgentLens 向被测服务发送 `POST /v1/agent-runs`，请求包含任务输入、随机种子、环境快照、cassette 内容摘要、工具网关地址和运行预算。关键环境字段如下：

```json
{
  "environment_snapshot": "customer-tools-v2:cassette-2026-08-12",
  "cassette_content_sha256": "35e9f0c3c6ad9da38aa2ee80c0f597c514269ee31bd8c4b6aaba608e279de7c5",
  "tool_grant_token": "<opaque-run-scoped-token>",
  "tool_gateway_url": "http://127.0.0.1:8000/api/v1/tools/invoke"
}
```

被测 Agent 调用 `tool_gateway_url` 时必须把收到的 `run_id`、`tool_grant_token` 与 `cassette_content_sha256` 原样放入每个 replay 请求。Worker 为每条 HTTP trial 生成 256-bit opaque token，数据库只保存 SHA-256，并绑定 run、cassette 内容、任务 allowlist、`max_tool_calls` 与墙钟预算加 30 秒宽限；每次调用在行锁事务中扣减，trial 成功、失败或取消都会撤销。撤销和到期记录默认保留 7 天供审计，随后由健康维护路径以行锁和有界批量清理；可通过 `TOOL_GRANT_RETENTION_SECONDS`（3600–31536000）与 `TOOL_GRANT_CLEANUP_BATCH_SIZE`（1–10000）调整。缺失或格式错误返回脱敏 422，跨 run、错摘要、越权工具和撤销/过期 token 返回 403，预算耗尽返回 429，cassette miss 返回 424。被测服务以 `text/event-stream` 返回轨迹：

```text
event: trace
data: {"run_id":"run-42","seq":0,"type":"run.started","payload":{"seed":7}}

event: trace
data: {"run_id":"run-42","seq":1,"type":"tool.call","payload":{"name":"get_order","arguments":{"order_id":"O-8891"}}}

event: trace
data: {"run_id":"run-42","seq":2,"type":"tool.result","payload":{"name":"get_order","result":{"status":"paid"}}}

event: trace
data: {"run_id":"run-42","seq":3,"type":"usage","payload":{"input_tokens":611,"output_tokens":115,"cached_tokens":120}}

event: trace
data: {"run_id":"run-42","seq":4,"type":"final","payload":{"answer":"Eligible for refund","verified":true}}

event: trace
data: {"run_id":"run-42","seq":5,"type":"run.completed","payload":{"status":"success"}}
```

可运行实现见 [`examples/scripted_agent.py`](examples/scripted_agent.py)，其六个任务的 HTTP/SSE 合同由后端自动化协议测试覆盖。

## 验证与证据生成

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\verify_repository_hygiene.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check --no-cache .
.\.venv\Scripts\python.exe scripts\run_offline_benchmark.py

cd ..\frontend
pnpm lint
pnpm test
pnpm build

cd ..
docker compose config --quiet
```

运行 Compose 跨进程验收器需要可用的 Docker Linux Engine。脚本设计为等待 API 就绪，并检查 fail-closed 工具 miss、API → Redis/ARQ Worker → PostgreSQL 的完成路径、唯一 SSE 终止事件、取消不被覆盖以及审计计数增量；只有脚本成功完成并保存 JSON，才构成该环境的一次验证证据：

```powershell
# 在仓库根目录执行
try {
  docker compose --env-file .env.example -p agentlens_verify up -d --build
  docker compose --env-file .env.example -p agentlens_verify exec -T -e AGENTLENS_VERIFY_OUTPUT=/tmp/agentlens-compose-verification.json api python scripts/verify_compose_stack.py
  docker compose --env-file .env.example -p agentlens_verify cp api:/tmp/agentlens-compose-verification.json evidence/compose-verification.local.json
} finally {
  docker compose --env-file .env.example -p agentlens_verify down -v --remove-orphans
}
```

GitHub Actions 工作流 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 定义了后端卫生扫描/Ruff/测试/离线摘要、前端冻结安装/ESLint/Vitest/生产构建，以及 Ubuntu runner 上的 PostgreSQL + Redis/ARQ Compose E2E。工作流不注入模型 Key；Compose job 使用仓库内 `.env.example` 与 fake provider，通过 `execution_mode=http` 验证候选 SSE、工具网关、跨进程持久化、Web 入口和清理链路。[公开运行 35718661033](https://github.com/bliss-fox/AgentLens/actions/runs/35718661033) 的三个 job 均通过；Compose job 上传的 `agentlens-compose-verification` artifact 记录 `schema_version=agentlens-compose-verification/v7`、`status=passed`、Alembic revision `0005_evaluation_assets`、ARQ/数据库/Worker 健康状态、6 条候选运行、取消 SSE 和 fail-closed Tool Gateway 证据。artifact 保留期为 14 天，运行步骤与日志继续保留；该证据验证工程链路，不是任何真实模型的效果 benchmark。

离线合成基准记录运行环境、固定种子、bootstrap 配置、成功/失败计数、fixture agreement、持久化数量、耗时、版本化评测合同、完整 `TaskSpec` SHA-256、冻结 cassette 内容 SHA-256 与规范化运行 SHA-256。CI 同时断言任务指纹、cassette 合同和运行摘要。`elapsed_seconds` 仅表示本地确定性数据生成时间，不代表外部模型延迟。

## 代码导览

| 模块 | 职责 |
| --- | --- |
| [`backend/agentlens/api.py`](backend/agentlens/api.py) | `create_app()` 组合根、lifespan、异常处理和领域 router 装配 |
| [`backend/agentlens/routers`](backend/agentlens/routers) | health、experiments、reviews、tools 的独立 REST/SSE 传输边界 |
| [`backend/agentlens/store.py`](backend/agentlens/store.py) | 数据库驱动状态机、冻结任务/版本合同校验、registry 注入、线程执行与 SSE |
| [`backend/agentlens/worker.py`](backend/agentlens/worker.py) | ARQ ctx 生命周期依赖、跨进程任务入口与线程卸载 |
| [`backend/scripts/verify_compose_stack.py`](backend/scripts/verify_compose_stack.py) | Compose 跨进程完成、取消、grant retention、离线 Judge 租约、SSE 与审计增量验收 |
| [`backend/scripts/verify_repository_hygiene.py`](backend/scripts/verify_repository_hygiene.py) | 脱敏检查秘密文件、常见 token 与本地绝对路径 |
| [`backend/agentlens/agent_client.py`](backend/agentlens/agent_client.py) | 被测 Agent HTTP/SSE 客户端、取消清理与严格协议校验 |
| [`backend/agentlens/runtime.py`](backend/agentlens/runtime.py) | scripted/HTTP 统一运行结果适配、错误归一化与状态断言 |
| [`backend/agentlens/execution_contract.py`](backend/agentlens/execution_contract.py) | 冻结评测快照的集中构造、强类型恢复与版本/内容漂移拒绝 |
| [`backend/agentlens/evaluation.py`](backend/agentlens/evaluation.py) | 轨迹匹配、统计区间、成本计算和失败归因 |
| [`backend/agentlens/cassette.py`](backend/agentlens/cassette.py) | 工具环境 record/replay、规范化内容指纹与 fail-closed 行为 |
| [`backend/agentlens/database_core.py`](backend/agentlens/database_core.py) | 共享 engine/session、进程内事务锁和 UTC 规范化 |
| [`backend/agentlens/tool_grant_repository.py`](backend/agentlens/tool_grant_repository.py) | run-scoped grant 签发、原子消费、撤销和有界 retention |
| [`backend/agentlens/experiment_repository.py`](backend/agentlens/experiment_repository.py) | 实验、运行、事件持久化与状态机事务 |
| [`backend/agentlens/review_repository.py`](backend/agentlens/review_repository.py) | Judge 三层证据核验、租约与原子双写事务 |
| [`backend/agentlens/database.py`](backend/agentlens/database.py) | 旧导入路径的纯兼容 facade，不承载事务实现 |
| [`frontend/src/App.tsx`](frontend/src/App.tsx) | 实验创建、SSE 订阅、取消与页面状态 |
| [`frontend/src/components`](frontend/src/components) | 稳定性图表、失败证据、完整轨迹和辅助页面 |

## 安全边界与已知限制

- 内置 `tool-runner` 会执行任务中的 Python 代码，但它只是 **受限执行 / best-effort isolation**，不是能安全运行恶意代码的沙箱。它使用静态策略、独立子进程、隔离模式、超时后进程组终止、输出上限和精简子进程环境；Compose 另外使用非 root 用户、只读文件系统、tmpfs、capability drop、`no-new-privileges`、CPU/内存/PID 限制。它不能替代专用沙箱、VM、seccomp 或微虚拟机；不要把不可信攻击代码交给本地部署执行。
- 测试不可信候选时，应将候选部署在单独容器或 VM 中。Agent 与工具网关端点只能通过受控环境配置提供，且拒绝内嵌凭据、query 和 fragment；工具网关 URL 还会在运行协议和持久化恢复边界复验。run-scoped 工具 token 只在 trial 请求中下发，服务端仅保存哈希；422 验证错误删除原始输入，授权错误不回显 token。被测 Agent 仍属于 token 持有者，应避免记录请求正文。
- record 模式只应连接操作者明确授权的服务；replay 模式不会在 cassette miss 后访问真实网络。
- `judge.py` 提供显式按需的 OpenAI-compatible 语义复核；后台主链路和 CI 始终保持离线 `not_run`。只有用户点击或调用复核 API 且配置 Key 时才发送失败归因范围内的事件并产生潜在费用；凭据键脱敏和上下文限额降低暴露面，但不构成通用 PII 清洗。租约独占、过期接管、默认幂等、显式重复确认、三层证据核验、span lineage、禁用隐式重试、引用校验、严格响应验证、超时、客户端关闭、脱敏错误和原子双写已有测试。真实人工标注校准集仍是后续工作。
- 当前是本地单用户 MVP，没有 RBAC、托管控制面、训练或自动优化能力；本地服务不应直接暴露到不可信网络。
- 离线 scripted 候选只用于测试评测器自身的可复现性。配置有效供应商与 Key 后，HTTP 候选路径支持调用真实模型，但结果受模型版本、网络和供应商服务状态影响；当前仓库没有发布可核验的真实模型实验 artifact。
- Python 直接依赖声明了最低版本和兼容主版本上界，Node 使用冻结 lockfile，容器镜像固定主版本标签；但 Python 尚无全哈希 lockfile，镜像也未固定 digest，进一步加固仍需加入哈希锁、镜像 digest 与 SBOM。

开发细节见 [`docs/development.md`](docs/development.md)。
