# AgentLens 面试导览

这份文档提供一条 3 分钟演示主线，并保留 5–10 分钟的深入核查入口。所有数字都能从仓库中的代码、测试或固定 benchmark 证据复算，不依赖口头描述。

## 1. 一句话判断

AgentLens 把“展示一次成功的 Agent Demo”升级为“可复现的工程评测”：固定候选与环境，重复运行，严格采集轨迹，对结果、稳定性、成本与失败原因分别给出证据。

## 2. 建议演示路径

1. **0:00–0:30，定位**：用主工作台说明 AgentLens 解决“Agent 是否稳定、为何失败、结论能否复现”。
2. **0:30–1:10，对比**：展示候选 v1.4 与基线 v1.3 的成功率、95% 区间，以及相同任务、种子和环境快照。
3. **1:10–1:50，证据**：打开一次失败归因和完整轨迹，定位规则 ID、事件区间与 `run.started → tool.call/result → usage → final → run.completed`。
4. **1:50–2:30，运行语义**：触发新实验观察 SSE 进度，再取消一次，确认终态不会被覆盖。
5. **2:30–3:00，可复现性**：展示 `evidence/offline-benchmark.json` 的固定配置与 digest，再打开[公开 Compose E2E 运行](https://github.com/bliss-fox/AgentLens/actions/runs/35718661033)及其 `agentlens-compose-verification` artifact。

## 3. 三个最值得追问的设计点

### 为什么不能只看成功率？

单次成功可能来自随机性，整体点估计也会掩盖任务差异。因此项目同时报告每个任务的 Wilson 区间、跨任务分层 bootstrap 区间，以及候选/基线在相同 seed 下的配对差值区间。当前固定样本中，v1.4 相对 v1.3 提升 16.7 个百分点，95% 区间为 0.8–31.7 个百分点。

### 为什么 Judge 不能直接决定对错？

LLM Judge 本身也会漂移。当前离线演示使用 24 条脚本校准 fixture，得到 22/24 一致；这只验证校准与展示链路，不代表真实模型准确率。默认主链路未运行语义 Judge，因此证据明确标记 `not_run`。用户可以显式调用按需复核 API；API 只发送确定性归因范围内的事件，并在调用前执行凭据键脱敏和上下文字节上限；每次付费调用先领取可过期的数据库租约，避免多进程重复请求；已有结果只有显式 force 才复核。只有引用这些真实持久化事件的严格结构化结果才会原子写回，原始解释仍保留。接入真实标注集后，Judge 仍只应复核确定性规则产生的证据。

### 如何保证环境差异不污染 A/B？

工具调用通过版本化 cassette record/replay。入队事务保存环境版本及内容 SHA-256；Worker 抢占前重算合同，并为每条 HTTP trial 签发只保存哈希、绑定 run/摘要/allowlist/预算/TTL 的 opaque grant。被测 Agent 每次 replay 原样回传 run、token 与摘要，网关用行锁逐次核验和扣减，trial 结束撤销。同名响应漂移、跨 run 复用、越权、过期、撤销、预算耗尽或 cassette miss 都 fail closed，且不访问真实网络。

## 4. 代码核查入口

| 想核查的问题 | 文件 | 重点 |
| --- | --- | --- |
| 轨迹是不是严格校验 | `backend/agentlens/schemas.py` | `EventStreamValidator` 的顺序与终止不变量 |
| HTTP/SSE 错误如何归一化 | `backend/agentlens/agent_client.py`、`runtime.py` | Content-Type、坏 JSON、超时、取消、大小预算和状态码处理 |
| 统计结论怎么算 | `backend/agentlens/evaluation.py` | Wilson、分层 bootstrap 与 paired bootstrap |
| 失败证据是不是可定位 | `backend/agentlens/evaluation.py` | `FailureEvidence`、事件范围、规则 ID 和严重级别 |
| 工具环境是否 fail closed | `backend/agentlens/cassette.py`、`database.py`、`tool_gateway.py`、`runtime.py` | 内容冻结加 run-scoped grant；跨 run、越权、过期、撤销、预算耗尽或 miss 均拒绝 |
| 异步流程是否可审计 | `backend/agentlens/store.py`、`database.py`、`worker.py` | 数据库状态机、原子抢占、真实进度、取消与 ARQ 跨进程执行 |
| 前端是否真的消费流式事件 | `frontend/src/api.ts`、`App.test.tsx` | progress/result/cancelled/error 四类事件；失败快照替换旧运行状态 |
| 结论能否重建 | `backend/scripts/run_offline_benchmark.py` | 固定种子、采样参数与证据写入 |

## 5. 现场验证

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check --no-cache .
.\.venv\Scripts\python.exe scripts\run_offline_benchmark.py
```

期望看到后端测试全部通过、Ruff 无错误，并重新生成 `evidence/offline-benchmark.json`。证据中的 `task_snapshot_sha256` 用于确认完整任务定义一致，`cassette_contract.cassette.content_sha256` 用于确认工具响应内容一致，`run_digest_sha256` 用于确认规范化运行内容一致；生成时间与本地耗时允许变化。

## 6. 边界与诚实说明

- 内置候选是确定性脚本，只用于验证评测系统，不代表真实大模型效果。
- 当前 6 个任务 × 10 个 seed 的样本能演示统计方法，但不足以代表复杂生产分布。
- 当前 ARQ 以整个实验为一个幂等抢占任务；更大规模场景可进一步拆成按 run 分片、可重试的 Worker 任务。
- 仓库提供 `verify_compose_stack.py` 验证 PostgreSQL、Redis/ARQ、SSE、取消和 grant 有界保留的跨进程链路，并用 MockTransport 测试验收判定；本地真实容器运行需要 Docker Linux Engine；[GitHub Actions run 35718661033](https://github.com/bliss-fox/AgentLens/actions/runs/35718661033) 已在 Ubuntu runner 上使用 fake provider 完成该基础设施验收并上传机器可读 artifact。它不证明真实模型质量。
- 系统不负责沙箱化被测 Agent；不可信候选应在容器或 VM 中运行。

## 7. 下一阶段优先级

1. 加入真实模型与真实失败样本，按业务风险扩充并分层任务集。
2. 用 Testcontainers 覆盖 PostgreSQL、Redis、Worker 和迁移链路。
3. 将快照、cassette、校准集与报告做不可变版本管理。
4. 增加多租户权限、敏感字段脱敏、OpenTelemetry 与运行告警。
5. 增加统计功效分析，避免在样本不足时给出过强的“显著提升”结论。
