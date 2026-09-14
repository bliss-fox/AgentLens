import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ApiError, api, type ExperimentStreamError } from './api'
import { AgentWorkspace } from './components/AgentWorkspace'
import { Header } from './components/Header'
import { SetupWorkspace } from './components/SetupWorkspace'
import { Sidebar, type PageKey } from './components/Sidebar'
import { SupportingPage } from './components/SupportingPages'
import type { Benchmark, Candidate, Experiment } from './types'

const EMPTY_CANDIDATES: Candidate[] = []
const EMPTY_BENCHMARKS: Benchmark[] = []

function operationErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return '请求未完成，请检查 API 状态后重试。'
  const experimentNote = error.experimentId ? `实验 ${error.experimentId} 已记录为失败。` : ''
  if (error.category === 'queue_unavailable') return '评测队列暂不可用。请检查 Redis 与 Worker。' + experimentNote
  if (error.category === 'executor_unavailable') return '本地测评执行器无法启动。请检查进程资源后重试。' + experimentNote
  if (error.category === 'candidate_unavailable') return '候选 Agent 未就绪，请检查 Endpoint 和 API Key。'
  if (error.category === 'invalid_execution_config') return '真实运行配置无效，请检查候选、任务集和环境快照。'
  if (error.category === 'judge_unavailable') return '语义 Judge 未配置，确定性失败证据保持不变。请先配置模型端点与 API Key。'
  if (error.category === 'judge_context_too_large') return '失败归因范围超过 Judge 上下文上限，未发送外部请求。请缩小事件负载或调整受控配置。'
  return error.retryable ? '请求暂时失败，可以重试。' : error.message || '请求失败，请检查 API 日志。'
}

function streamErrorMessage(error: ExperimentStreamError): string {
  if (error.category === 'worker_lost') return '测评 Worker 心跳已过期，实验已安全终止；请检查 Worker 后重新运行。'
  return error.message ? `测评执行失败：${error.message}` : '测评执行失败，请检查服务状态。'
}

export function App() {
  const [page, setPage] = useState<PageKey>('agent')
  const [experiment, setExperiment] = useState<Experiment | null>(null)
  const [showSetup, setShowSetup] = useState(false)
  const [candidateId, setCandidateId] = useState('')
  const [benchmarkId, setBenchmarkId] = useState('')
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState({ completed: 0, total: 0 })
  const [operationError, setOperationError] = useState<string | null>(null)
  const closeStream = useRef<(() => void) | null>(null)
  useEffect(() => () => closeStream.current?.(), [])
  const bootstrap = useQuery({ queryKey: ['bootstrap'], queryFn: api.bootstrap })
  const candidates = bootstrap.data?.candidates ?? EMPTY_CANDIDATES
  const benchmarks = bootstrap.data?.benchmarks ?? EMPTY_BENCHMARKS
  const current = experiment ?? bootstrap.data?.experiment ?? null
  const effectiveCandidateId = candidateId || current?.candidate.id || ''
  const effectiveBenchmarkId = benchmarkId || (current?.benchmark_name.includes('客服') ? 'customer-tools-v2' : 'coding-agent-core-v1')

  useEffect(() => {
    if (!candidateId && candidates.length) {
      setCandidateId(candidates.find((item) => item.id === 'coding-assistant-v1')?.id ?? candidates[0].id)
    }
    if (!benchmarkId && benchmarks.length) {
      setBenchmarkId(benchmarks.find((item) => item.id === 'coding-agent-core-v1')?.id ?? benchmarks[0].id)
    }
  }, [candidateId, benchmarkId, candidates, benchmarks])

  useEffect(() => {
    if (bootstrap.data && bootstrap.data.experiment === null) setShowSetup(true)
  }, [bootstrap.data])

  const create = useMutation({
    mutationFn: api.createExperiment,
    onMutate: () => setOperationError(null),
    onError: (error) => {
      setRunning(false)
      setOperationError(operationErrorMessage(error))
    },
    onSuccess: (result) => {
      setExperiment(result)
      setShowSetup(false)
      setProgress({ completed: 0, total: result.total_runs })
      setRunning(true)
      closeStream.current?.()
      closeStream.current = api.streamExperiment(result.id, {
        onProgress: (completed, total) => setProgress({ completed, total }),
        onResult: (completedExperiment) => {
          setExperiment(completedExperiment)
          setRunning(false)
          setOperationError(null)
        },
        onCancelled: () => setRunning(false),
        onError: (error) => {
          if (error.experiment) setExperiment(error.experiment)
          setRunning(false)
          setOperationError(streamErrorMessage(error))
        },
      })
    },
  })
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelExperiment(id),
    onError: (error) => setOperationError(operationErrorMessage(error)),
    onSuccess: (result) => {
      setExperiment(result)
      setRunning(false)
    },
  })
  const review = useMutation({
    mutationFn: ({ experimentId, runId, failureIndex, force }: { experimentId: string; runId: string; failureIndex: number; force: boolean }) =>
      api.reviewFailure(experimentId, runId, failureIndex, force),
    onError: (error) => setOperationError(operationErrorMessage(error)),
    onSuccess: (result) => setExperiment(result.experiment),
  })

  if (bootstrap.isError) return <div className="error-screen"><h1>无法连接 AgentLens API</h1><p>请确认 Docker Compose 服务和 8000 端口。</p></div>
  if (bootstrap.isLoading) return <div className="loading-screen"><span className="loading-logo">A</span><p>正在装载真实评测资产…</p></div>

  const run = (prompt: string, baselineId: string | null = null, repetitions = 3) => {
    create.mutate({
      prompt,
      candidate_id: effectiveCandidateId,
      baseline_candidate_id: baselineId,
      benchmark_id: effectiveBenchmarkId,
      execution_mode: 'http',
      repetitions,
    })
  }
  const setup = (
    <SetupWorkspace
      candidates={candidates}
      benchmarks={benchmarks}
      candidateId={effectiveCandidateId}
      benchmarkId={effectiveBenchmarkId}
      running={running}
      onRun={run}
      onSaveCandidate={async (candidate: Candidate) => {
        await api.saveCandidate(candidate)
        await bootstrap.refetch()
      }}
      onTestCandidate={async (id: string) => {
        try {
          const result = await api.testCandidate(id)
          await bootstrap.refetch()
          const provider = typeof result.status.provider === 'string' ? result.status.provider : 'unknown'
          const model = typeof result.status.model === 'string' ? result.status.model : result.candidate.model
          window.alert(`候选 Agent 连接成功：${provider} / ${model}。运行时指纹已同步。`)
        } catch (error) {
          setOperationError(operationErrorMessage(error))
        }
      }}
      onSaveBenchmark={async (benchmark: Benchmark) => {
        await api.saveBenchmark(benchmark)
        setBenchmarkId(benchmark.id)
        await bootstrap.refetch()
      }}
    />
  )

  return (
    <div className="app-shell">
      <Sidebar page={page} onPage={setPage} />
      <div className="app-main">
        {page === 'agent' ? (
          <>
            <Header candidates={candidates} benchmarks={benchmarks} candidateId={effectiveCandidateId} benchmarkId={effectiveBenchmarkId} onCandidate={setCandidateId} onBenchmark={setBenchmarkId} onNew={() => setShowSetup(true)} />
            {operationError && showSetup ? <div className="operation-error setup-error" role="alert">{operationError}</div> : null}
            {showSetup || !current ? setup : <AgentWorkspace experiment={current} running={running} progress={progress} errorMessage={operationError} onRun={(prompt) => run(prompt)} onCancel={() => cancel.mutate(current.id)} onReview={(runId, failureIndex, force) => review.mutate({ experimentId: current.id, runId, failureIndex, force })} reviewing={review.isPending} />}
          </>
        ) : page === 'tasks' || !current ? setup : <SupportingPage page={page} experiment={current} />}
      </div>
    </div>
  )
}
