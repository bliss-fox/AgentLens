# AgentLens 开发与验证说明

系统分层、状态所有权、依赖方向和后续重构顺序见 [`architecture.md`](architecture.md)。

本文面向希望快速核查实现质量的技术面试官和协作者，重点说明本地运行、可复现资产、关键模块与验证入口。

## 项目结构

```text
backend/
  agentlens/                 组合根、领域 routers、评测编排、统计与持久化
  scripts/                   离线 benchmark 与 Compose 跨进程验收脚本
  tests/                     API、协议和评测逻辑测试
  alembic/                   PostgreSQL schema migration
frontend/
  src/components/            指标、稳定性、失败证据与轨迹 UI
evidence/
  offline-benchmark.json     机器可读的固定实验结果
examples/
  scripted_agent.py          被测 Agent 协议最小实现
```

## 本地启动

后端：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn agentlens.api:app --reload --port 8000
```

前端：

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

浏览器打开 <http://localhost:5173>。本地默认使用进程内 SQLite；完整多进程链路请从仓库根目录运行 `docker compose up --build`。Compose 的一次性 `migrate` 服务先运行 `alembic upgrade head`；Worker 在迁移和 Redis 健康后启动并发布带 TTL 的 ARQ 心跳，API 依赖 Worker 健康，Web 再依赖 API 健康。`0001_initial` 保持历史不可变，`0002_schema_parity` 会回填旧记录的空时间戳并将字段收紧为 `NOT NULL`，`0003_event_span_lineage` 再为独立事件审计行增加可空的 span lineage 字段，`0004_tool_grants` 新增只保存 token 哈希的运行级工具授权、allowlist、调用余量、到期与撤销审计字段。

外部 Agent 模式通过 `ExperimentRequest.execution_mode=http` 启用。`AGENT_ENDPOINTS` 可为内置 fixture ID 补充端点；`CANDIDATE_OVERRIDES` 用于提供任意 HTTP 候选的完整 `CandidateSpec`。配置在 Settings 加载时校验嵌套 ID 与目录键一致，并拒绝候选或工具网关端点中的凭据、query 和 fragment；工具网关 URL 还会在运行协议与持久化快照恢复时复验。工具授权审计的 `TOOL_GRANT_RETENTION_SECONDS` 默认 604800 秒且限制在 3600–31536000 秒，`TOOL_GRANT_CLEANUP_BATCH_SIZE` 默认 1000 且限制在 1–10000；`/health` 会将到期 active 记录标为 expired，并仅分批删除超过保留期的 expired/revoked 记录，同时公开不含原始 token 的状态计数。API 在持久化 queued 记录前再次校验候选与基线；scripted 模式不会读取动态候选。Worker 将每条 SSE 轨迹归一化为同一 `RunResult`，因此两种模式共享断言、归因、统计和持久化逻辑。可运行合同服务见 `examples/scripted_agent.py`。可选 Judge 仅在配置 API Key 且用户显式调用复核 API 后启用，`JUDGE_TIMEOUT_SECONDS` 默认 60 秒且限制在 1–600 秒，`JUDGE_MAX_CONTEXT_BYTES` 默认 131072 且限制在 1024–1048576 字节；每次复核都会关闭独立异步客户端并设置 `max_retries=0`，后台实验与页面装载不触发外部请求。OpenAI base URL 使用与候选端点相同的绝对 HTTP(S) 校验，并拒绝内嵌凭据、query 与 fragment。

## 按需 Judge 合同

语义 Judge 默认关闭，不属于实验后台主链路。显式调用：

```text
POST /api/v1/experiments/{experiment_id}/runs/{run_id}/failures/{failure_index}/review
```

API 只读取已完成实验中指定的失败，并仅发送确定性 `event_range` 内的持久化事件。调用前会递归替换常见凭据键并按 UTF-8 序列化字节执行上下文硬上限；该脱敏不承诺识别通用 PII。模型返回对象会再次经过 Pydantic 校验，引用必须真实存在于发送的事件中；有效复核会在一个事务中同步更新 `ExperimentRecord.metrics.summary` 和 `RunRecord.result`，同时保留原始确定性解释。类别一致且模型声明支持时写入 `support`，其余有效意见写入 `conflict`。未配置 Key 返回非重试型 `judge_unavailable` 503；非完成实验和缺失上下文在模型调用前拒绝，上下文超限返回 413；外部不可用、超时、非法结构化响应或引用返回结构化脱敏错误，且保持 `not_run` 不变。每次模型调用前会在同一事务中校验实验摘要、`RunRecord.result` 与独立 `EventRecord` 中 failure 范围的完整事件（包括 span lineage）一致，再通过数据库行锁领取携带规范上下文的 failure 级租约；API 只发送该租约上下文，OpenAI SDK 不执行隐式重试；活动租约返回 `review_in_progress`，已有结果默认返回 `review_already_exists`，均不会调用模型。显式 `force=true` 才允许重复复核；租约 token 必须匹配才能提交或释放，失败路径主动释放，崩溃遗留租约在 Judge timeout 加 30 秒后过期。CI 不配置 Key，测试通过 fake client/verdict 覆盖客户端关闭、超时脱敏、引用校验与原子双写。

界面中的“语义复核”是唯一自动组装该请求的入口，只在实验完成后启用，并在按钮旁提示归因范围事件会发送到外部端点且可能产生费用；已有结果的重新复核需要浏览器确认并发送 `force=true`，页面装载和实验运行不会隐式调用。

## 验证命令

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

CI 定义在 `.github/workflows/ci.yml`，以三个并行 job 复用上述门禁：后端运行脱敏仓库卫生扫描、全仓 Ruff、后端全量测试、候选适配器/受限执行器服务测试和摘要断言；前端使用 pnpm 11.19.0 冻结安装，运行 ESLint、Vitest 并构建；Compose job 解析配置后构建并启动 PostgreSQL、Redis、migrate、候选适配器、受限执行器、Worker 与 API，在 API 容器中运行真实跨进程验收，失败时输出日志并通过 `always()` 清理 volume。CI 不注入模型凭据，并显式设置 `CANDIDATE_FAKE_MODEL=true`；fake 只用于验证候选协议与基础设施，不用于产品测评。

Compose 跨进程验收：

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

验收器等待 API 就绪后，分别创建完成实验和立即取消实验；它要求 API 报告 `execution_backend=arq`、`database_status=ok`、Alembic revision `0005_evaluation_assets`、`queue_status=ok` 与 `worker_status=ok`，验证健康响应包含 grant 状态、7 天保留期和有界批量配置，并回填一条超过保留期的终态 grant，要求下一次健康维护实际清理；随后读取公开 cassette 摘要并直接在数据库签发短期验证 grant，再验证正确授权的 replay miss 返回 424、错摘要与跨 run 复用返回 403、撤销后仍返回 403，且所有错误不泄漏 traceback 或原始 token，然后检查唯一 SSE 终止事件、6 条 HTTP 候选运行、取消状态不可覆盖，以及 PostgreSQL 审计计数增量。完成实验后，验收器会选取同一条确定性失败连续执行两次无 Key Judge 复核；两次都必须返回非重试型 `judge_unavailable` 503，第二次不能变成 `review_in_progress`，重新读取实验时失败证据必须完全不变。这一合同验证失败路径会释放数据库租约，不会污染审计证据。默认单元测试使用 MockTransport 验证成功判定和租约泄漏失败判定，本地运行单元测试不需要 Docker；CI 的独立 Compose job 则使用 Ubuntu runner 的真实 Linux Engine 执行同一验收器。验收器只在全量通过后原子写入 schema 为 `agentlens-compose-verification/v7`、包含 Python/平台信息、`tool_grant_retention`、耗时及 `completed.offline_judge` 证据的 JSON，CI 将其上传为 `agentlens-compose-verification` artifact。

后端测试覆盖：

- 版本控制候选文件中的秘密 `.env`、常见 token、本地绝对路径，以及直接依赖上下界合同；
- FastAPI bootstrap、实验创建、健康检查与 fail-closed 工具网关；
- Alembic 从空库升级到 head、事件序号唯一约束、span lineage 迁移以及 ORM schema 零漂移；
- 数据库驱动的 queued → running → completed 状态迁移、跨 Store 读取、HTTP 202 创建语义、完整任务快照与版本合同、任务目录及 cassette 内容的滚动升级漂移拒绝、guarded completion、候选+基线统一进度口径、进度范围/总量/单调事务不变量，以及 running 心跳续期/过期回收；
- 实验 SSE 的真实进度、唯一结果终止、显式取消、失败脱敏和断连行为；
- 本地线程启动失败的注册回滚与结构化 503、运行中取消向执行器边界传播，FastAPI 关闭时取消并限时等待本地线程，Worker 协程取消落为 worker_interrupted，并在重新抛出取消前最多等待 2 秒回收底层线程，重复或终态任务不会再次抢占；
- 冻结评测合同的集中构造、适配器数据深拷贝、强类型恢复、版本/总量/模式/cassette 漂移拒绝，以及持久化层不反向依赖工具网关或 Store 的静态架构边界；
- 共享 database core、experiment/review/tool grant repositories、纯兼容 facade 的对象身份，生产调用方直接依赖 bounded-context repository，以及 repository 不反向依赖 facade/API/Store/工具网关的静态架构边界；`create_app()` 延迟默认依赖、显式实例身份、Store registry 注入、工具 registry 延迟兼容代理和 ARQ ctx startup/shutdown 所有权也有动态及 AST 防回归测试；组合根无领域仓储/Judge 导入、四个 router 的端点归属及 router 不反向依赖 API 同样由 AST 守卫锁定；
- 被测 Agent SSE 的乱序、缺少终止事件、伪装或错误 Content-Type、坏 JSON、HTTP/连接失败与超时归一化；
- 真实 HTTP Agent 的正常结果转换、运行中取消、事件/字节预算、候选/工具网关端点安全校验与示例服务 E2E；run-scoped 工具 grant 还覆盖 token 哈希存储、allowlist、TTL、撤销、跨 run 拒绝、并发原子调用预算、活动授权与清理并发保护、UTC 保留边界、有界批量、健康清理证据和 422 输入脱敏；
- 轨迹匹配模式、Wilson 区间、配对 bootstrap、确定性运行与失败归因，以及缓存 token 不变量和非重复成本计费；
- 不同实验间运行 ID 唯一性。

## 被测 Agent HTTP 合同

AgentLens 向 `POST /v1/agent-runs` 发送：任务输入、确定性种子、环境快照、`cassette_content_sha256`、一次运行级 `tool_grant_token`、工具网关 URL 和预算。被测 Agent 必须在每次 replay 请求中原样回传 run ID、token 与摘要；网关先在数据库行锁事务中核对 run、摘要、allowlist、到期、撤销状态并扣减调用预算，再读取 cassette 响应。响应的 MIME 主类型必须精确为 `text/event-stream`（允许 `charset` 等参数），事件需满足以下不变量：

1. `seq` 从 0 开始并严格递增；
2. 第一条事件是 `run.started`；
3. 终止语义是 `final|error` 后紧跟 `run.completed`；
4. 终止后不接受新事件；
5. 每条事件的 `run_id` 必须和请求一致；
6. 事件数、单事件字节和整条流字节不得超过请求预算。

完整解析和校验逻辑位于 `backend/agentlens/agent_client.py` 与 `backend/agentlens/schemas.py`。

创建实验的 `POST /api/v1/experiments` 先持久化 `queued` 记录并返回 HTTP 202。`completed_runs/total_runs` 与 SSE 均表示候选+基线总工作量；候选和基线指标则直接按各自运行列表统计，默认分别为 60 条，避免把 120 条进度误作单侧样本数。数据库更新还要求整数进度满足 `0 ≤ completed ≤ total`，并拒绝总量漂移或完成数回退。本地 `EXECUTION_BACKEND=local` 使用独立线程；Docker 设置 `EXECUTION_BACKEND=arq`，API 只将实验 ID 入队，Worker 跨进程从 PostgreSQL 读取持久请求并原子迁移为 `running`；旧版本 job 中的冗余 payload 仅为滚动升级兼容参数，不参与执行。入队事务会深拷贝并保存完整 `TaskSpec` 列表，以它计算候选/基线总工作量，并记录 benchmark、任务集、离线校准 fixture、评测器和价格表版本。Worker 不重新读取运行时任务目录，只使用持久快照；任务 ID、进度总量、排队摘要或版本合同不一致时在抢占前以 `invalid_execution_state` fail-closed。readiness 会并行探测数据库 revision 与审计计数，数据库异常返回不泄漏连接信息的 503；ARQ 模式还会执行 Redis `PING`，并要求 Worker 每 10 秒刷新一次自动过期的心跳；Redis 可连但 Worker 停止消费时，API 仍会返回 503。本地线程启动失败会先回滚线程注册，再把实验落为 `executor_unavailable`；入队异常则落为 `queue_unavailable|queue_rejected`。两类创建失败都返回包含实验 ID 和重试标记的脱敏 HTTP 503，前端会显示对应的可行动提示。执行器在抢占前校验持久请求、候选/基线、执行模式、完整任务快照、版本化评测合同、环境快照、工具网关，以及冻结 cassette 的 ID、模式、条目/key 和 `mode + records` 规范化 SHA-256；同名响应内容漂移、换绑或缺失时写入 `invalid_execution_state` 并通过 SSE 返回脱敏错误。候选和基线每完成一条运行都会写入进度并续期数据库执行心跳，取消也从数据库传播到下一条运行边界。心跳超过 300 秒未更新时，健康检查或 SSE 轮询会原子写入 `worker_lost` 终态；ARQ job timeout 则覆盖 `50 × 6 × 2` 条合法最大评测的单次 180 秒预算并预留收尾时间。Worker 中断会先写入 `worker_interrupted`；仍在 `asyncio.to_thread` 中执行的 runner 轮询到任意非 `running` 权威状态后协作退出，取消处理最多等待 2 秒，不会因不配合的 runner 无限阻塞 Worker 停机。

异步 API 中的同步 SQLAlchemy 写入和 SSE 状态轮询均通过 `asyncio.to_thread` 卸载，避免 PostgreSQL I/O 阻塞 FastAPI 事件循环。

平台自身的实验进度通过 `GET /api/v1/experiments/{id}/events` 返回异步 SSE。正常完成以 `experiment.result` 终止，显式取消以 `experiment.cancelled` 终止，生产者异常以脱敏的 `experiment.error` 终止；失败事件在可用时携带权威 `failed` 实验快照，React 在 SSE 回调中直接同步状态，不额外发起刷新请求；客户端断开会停止生成且不伪造完成事件。

## 可复现性设计

- 候选身份由 model、parameters、prompt hash、scaffold version 与 tool schema hash 组成。
- 实验快照固定执行模式、候选端点、完整 `TaskSpec` 列表、环境快照、工具网关、离线校准 fixture、评测器和价格表版本；语义 Judge 是完成后的显式按需复核，不属于后台实验快照。
- 候选与基线使用相同任务、种子与快照，支持配对对比。
- 工具 replay 采用 fail-closed：入队事务保存不含原始响应的 cassette 合同，Worker 抢占前按内容指纹复验；每条 HTTP trial 另签发只落哈希、绑定 run/摘要/allowlist/预算/TTL 的 opaque grant，网关逐次原子消费，结束必撤销。错摘要、跨 run、越权、过期、撤销和预算耗尽都有独立结构化错误，cassette miss 不回退真实网络。
- 参考轨迹默认只用于诊断，只有任务明确设置 `trajectory_required` 时才参与成败判定。
- 缺少 usage 事件时成本标记为不完整，不自动估算。

## 重建离线证据

从仓库根目录执行：

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\run_offline_benchmark.py
```

脚本固定使用 6 个任务、种子 1–10、bootstrap seed 2026 和 1,500 次采样。输出覆盖写入 `evidence/offline-benchmark.json`，其中包括环境、协议、候选/基线指标、失败类别、脚本校准 fixture、持久化计数、版本化评测合同、完整 `TaskSpec` SHA-256、冻结 cassette 内容 SHA-256 与规范化运行摘要；CI 同时断言任务指纹、cassette 合同和运行摘要。

## 适合继续扩展的方向

- 用 Testcontainers 增加 PostgreSQL、Redis 与 ARQ 的真实集成测试；
- 将任务集、Judge 校准集与 cassette 做版本化对象存储；
- 增加多租户、RBAC、审计日志和敏感字段脱敏；
- 加入 OpenTelemetry trace 与 Prometheus 运行指标；
- 使用全哈希 Python/Node lockfile、镜像 digest、SBOM 和依赖扫描增强供应链安全；
- 引入真实模型回归集，并按任务难度与业务风险分层报告统计功效。
