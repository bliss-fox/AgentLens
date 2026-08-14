# AgentLens 面试导览

这份文档提供一条 5–10 分钟的项目核查路径。所有数字都能从仓库中的代码、测试或固定 benchmark 证据复算，不依赖口头描述。

## 1. 一句话判断

AgentLens 把“展示一次成功的 Agent Demo”升级为“可复现的工程评测”：固定候选与环境，重复运行，严格采集轨迹，对结果、稳定性、成本与失败原因分别给出证据。

## 2. 建议演示路径

1. 打开主工作台，查看候选 v1.4 与基线 v1.3 的成功率和 95% 区间。
2. 切换到“实验对比”，确认两侧使用相同任务、种子和环境快照。
3. 打开失败证据，查看一次循环调用如何关联到具体 trace event 区间和归因规则。
4. 打开完整轨迹，检查 `run.started → tool.call/result → usage → final → run.completed`。
5. 触发新实验并观察 SSE 进度；中途取消，确认终止状态不会被误报为完成。
6. 展示 `evidence/offline-benchmark.json`，说明结果如何离线重建。

## 3. 三个最值得追问的设计点

### 为什么不能只看成功率？

单次成功可能来自随机性，整体点估计也会掩盖任务差异。因此项目同时报告每个任务的 Wilson 区间、跨任务分层 bootstrap 区间，以及候选/基线在相同 seed 下的配对差值区间。当前固定样本中，v1.4 相对 v1.3 提升 16.7 个百分点，95% 区间为 0.8–31.7 个百分点。

### 为什么 Judge 不能直接决定对错？

LLM Judge 本身也会漂移。项目先用 24 条人工标注样本校准，当前 22/24 一致；Judge 只复核确定性规则产生的证据。当 Judge 与规则冲突或置信度低时，保留人工复核，而不是覆盖事实断言。

### 如何保证环境差异不污染 A/B？

工具调用通过版本化 cassette record/replay。replay miss 直接返回环境错误，不访问真实网络。候选和基线共用任务、seed、快照与价格表，因此差异更接近候选版本变化，而不是外部服务数据或延迟变化。

## 4. 代码核查入口

| 想核查的问题 | 文件 | 重点 |
| --- | --- | --- |
| 轨迹是不是严格校验 | `backend/agentlens/schemas.py` | `EventStreamValidator` 的顺序与终止不变量 |
| HTTP/SSE 错误如何归一化 | `backend/agentlens/agent_client.py` | Content-Type、坏 JSON、超时和状态码处理 |
| 统计结论怎么算 | `backend/agentlens/evaluation.py` | Wilson、分层 bootstrap 与 paired bootstrap |
| 失败证据是不是可定位 | `backend/agentlens/evaluation.py` | `FailureEvidence`、事件范围、规则 ID 和严重级别 |
| 工具环境是否 fail closed | `backend/agentlens/cassette.py` | replay miss 不允许真实调用 |
| 评测流程是否可审计 | `backend/agentlens/orchestrator.py` | 固定 LangGraph 节点与状态转换 |
| 前端是否真的消费流式事件 | `frontend/src/api.ts` | progress/result/cancelled/error 四类事件 |
| 结论能否重建 | `backend/scripts/run_offline_benchmark.py` | 固定种子、采样参数与证据写入 |

## 5. 现场验证

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check --no-cache .
.\.venv\Scripts\python.exe scripts\run_offline_benchmark.py
```

期望看到后端测试全部通过、Ruff 无错误，并重新生成 `evidence/offline-benchmark.json`。证据中的 `run_digest_sha256` 用于确认规范化运行内容是否一致；生成时间与本地耗时允许变化。

## 6. 边界与诚实说明

- 内置候选是确定性脚本，只用于验证评测系统，不代表真实大模型效果。
- 当前 6 个任务 × 10 个 seed 的样本能演示统计方法，但不足以代表复杂生产分布。
- LangGraph 节点中分析与报告步骤目前共享已生成的离线实验对象；生产化后应拆为可重试、幂等的 Worker 任务。
- Docker 方案包含 PostgreSQL 与 Redis/ARQ，但现有自动化测试以进程内 SQLite 为主；完整基础设施集成测试仍需补充。
- 系统不负责沙箱化被测 Agent；不可信候选应在容器或 VM 中运行。

## 7. 下一阶段优先级

1. 加入真实模型与真实失败样本，按业务风险扩充并分层任务集。
2. 用 Testcontainers 覆盖 PostgreSQL、Redis、Worker 和迁移链路。
3. 将快照、cassette、校准集与报告做不可变版本管理。
4. 增加多租户权限、敏感字段脱敏、OpenTelemetry 与运行告警。
5. 增加统计功效分析，避免在样本不足时给出过强的“显著提升”结论。
