# AgentLens 开发与验证说明

本文面向希望快速核查实现质量的技术面试官和协作者，重点说明本地运行、可复现资产、关键模块与验证入口。

## 项目结构

```text
backend/
  agentlens/                 FastAPI、评测编排、统计与持久化
  scripts/                   离线 benchmark 生成脚本
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

浏览器打开 <http://localhost:5173>。本地默认使用进程内 SQLite；完整多进程链路请从仓库根目录运行 `docker compose up --build`。

## 验证命令

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check --no-cache .
.\.venv\Scripts\python.exe scripts\run_offline_benchmark.py

cd ..\frontend
pnpm build

cd ..
docker compose config --quiet
```

后端测试覆盖：

- FastAPI bootstrap、实验创建、健康检查与 fail-closed 工具网关；
- 实验 SSE 的正常完成、显式取消和断连行为；
- 被测 Agent SSE 的乱序、缺少终止事件、错误 Content-Type、坏 JSON 与超时归一化；
- 轨迹匹配模式、Wilson 区间、配对 bootstrap、确定性运行与失败归因；
- 不同实验间运行 ID 唯一性。

## 被测 Agent HTTP 合同

AgentLens 向 `POST /v1/agent-runs` 发送：任务输入、确定性种子、环境快照、工具网关 URL 和预算。响应必须是 `text/event-stream`，事件需满足以下不变量：

1. `seq` 从 0 开始并严格递增；
2. 第一条事件是 `run.started`；
3. 终止语义是 `final|error` 后紧跟 `run.completed`；
4. 终止后不接受新事件；
5. 每条事件的 `run_id` 必须和请求一致。

完整解析和校验逻辑位于 `backend/agentlens/agent_client.py` 与 `backend/agentlens/schemas.py`。

平台自身的实验进度通过 `GET /api/v1/experiments/{id}/events` 返回异步 SSE。正常完成以 `experiment.result` 终止，显式取消以 `experiment.cancelled` 终止，生产者异常以脱敏的 `experiment.error` 终止；客户端断开会停止生成且不伪造完成事件。

## 可复现性设计

- 候选身份由 model、parameters、prompt hash、scaffold version 与 tool schema hash 组成。
- 实验快照固定候选、任务集、cassette、Judge、评测器和价格表版本。
- 候选与基线使用相同任务、种子与快照，支持配对对比。
- 工具 replay 采用 fail-closed：cassette miss 被归类为环境错误，不回退真实网络。
- 参考轨迹默认只用于诊断，只有任务明确设置 `trajectory_required` 时才参与成败判定。
- 缺少 usage 事件时成本标记为不完整，不自动估算。

## 重建离线证据

从仓库根目录执行：

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\run_offline_benchmark.py
```

脚本固定使用 6 个任务、种子 1–10、bootstrap seed 2026 和 1,500 次采样。输出覆盖写入 `evidence/offline-benchmark.json`，其中包括环境、协议、候选/基线指标、失败类别、Judge 校准、持久化计数与规范化运行摘要。

## 适合继续扩展的方向

- 用 Testcontainers 增加 PostgreSQL、Redis 与 ARQ 的真实集成测试；
- 将任务集、Judge 校准集与 cassette 做版本化对象存储；
- 增加多租户、RBAC、审计日志和敏感字段脱敏；
- 加入 OpenTelemetry trace 与 Prometheus 运行指标；
- 使用全哈希 Python/Node lockfile、镜像 digest、SBOM 和依赖扫描增强供应链安全；
- 引入真实模型回归集，并按任务难度与业务风险分层报告统计功效。
