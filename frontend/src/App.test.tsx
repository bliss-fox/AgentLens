import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import {
  ApiError,
  api,
  parseExperimentStreamError,
  type ExperimentStreamError,
} from './api'
import type { Experiment } from './types'

vi.mock('./components/AgentWorkspace', () => ({
  AgentWorkspace: ({
    experiment,
    errorMessage,
    onRun,
    onReview,
  }: {
    experiment: Experiment
    errorMessage: string | null
    onRun: (prompt: string) => void
    onReview: (runId: string, failureIndex: number, force: boolean) => void
  }) => (
    <div>
      <div data-testid="experiment-status">{experiment.status}</div>
      <div data-testid="completed-runs">{experiment.completed_runs}</div>
      {errorMessage ? <div role="alert">{errorMessage}</div> : null}
      <button type="button" onClick={() => onRun('重新运行')}>运行测试</button>
      <button type="button" onClick={() => onReview('run-failed', 0, false)}>复核测试</button>
    </div>
  ),
}))
vi.mock('./components/Header', () => ({ Header: () => <header>AgentLens</header> }))
vi.mock('./components/Sidebar', () => ({ Sidebar: () => <nav>导航</nav> }))
vi.mock('./components/SupportingPages', () => ({ SupportingPage: () => <main>支持页</main> }))

function experiment(status: Experiment['status'], completedRuns: number): Experiment {
  return {
    id: 'exp-stream-error',
    status,
    prompt: '评测失败状态同步',
    candidate: {
      id: 'support-v1.4',
      name: 'candidate',
      version: '1.4',
      model: 'fixture',
      prompt_hash: 'sha256:candidate',
      scaffold_version: 'fixture@1',
      tool_schema_hash: 'sha256:tools',
    },
    baseline: {
      id: 'support-v1.3',
      name: 'baseline',
      version: '1.3',
      model: 'fixture',
      prompt_hash: 'sha256:baseline',
      scaffold_version: 'fixture@1',
      tool_schema_hash: 'sha256:tools',
    },
    benchmark_name: 'customer-tools-v2',
    completed_runs: completedRuns,
    total_runs: 12,
    plan: [],
    runs: [],
    baseline_runs: [],
  } as unknown as Experiment
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('experiment error reconciliation', () => {
  it('parses a failed experiment snapshot from an SSE error', () => {
    const failed = experiment('failed', 3)
    expect(parseExperimentStreamError(JSON.stringify({
      category: 'worker_lost',
      message: 'experiment worker heartbeat expired',
      experiment: failed,
    }))).toEqual({
      category: 'worker_lost',
      message: 'experiment worker heartbeat expired',
      experiment: failed,
    })
    expect(parseExperimentStreamError('{broken')).toEqual({
      category: 'invalid_event',
      message: '测评失败事件格式无效，请检查 API 日志。',
    })
  })

  it('replaces the queued view with the authoritative failed snapshot', async () => {
    const completed = experiment('completed', 12)
    const queued = experiment('queued', 0)
    const failed = experiment('failed', 3)
    let streamHandlers: Parameters<typeof api.streamExperiment>[1] | undefined

    vi.spyOn(api, 'bootstrap').mockResolvedValue({
      experiment: completed,
      candidates: [],
      tasks: [],
      calibration: completed.judge_calibration,
    })
    vi.spyOn(api, 'createExperiment').mockResolvedValue(queued)
    vi.spyOn(api, 'streamExperiment').mockImplementation((_id, handlers) => {
      streamHandlers = handlers
      return () => undefined
    })

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>,
    )

    expect(await screen.findByTestId('experiment-status')).toHaveTextContent('completed')
    fireEvent.click(screen.getByRole('button', { name: '运行测试' }))
    await waitFor(() => expect(streamHandlers).toBeDefined())
    expect(screen.getByTestId('experiment-status')).toHaveTextContent('queued')

    const error: ExperimentStreamError = {
      category: 'worker_lost',
      message: 'experiment worker heartbeat expired',
      experiment: failed,
    }
    act(() => streamHandlers?.onError(error))

    expect(screen.getByTestId('experiment-status')).toHaveTextContent('failed')
    expect(screen.getByTestId('completed-runs')).toHaveTextContent('3')
    expect(screen.getByRole('alert')).toHaveTextContent(
      '测评 Worker 心跳已过期，实验已安全终止；请检查 Worker 后重新运行。',
    )
  })

  it('keeps deterministic evidence unchanged when on-demand Judge is not configured', async () => {
    const completed = experiment('completed', 12)
    vi.spyOn(api, 'bootstrap').mockResolvedValue({
      experiment: completed,
      candidates: [],
      tasks: [],
      calibration: completed.judge_calibration,
    })
    const review = vi.spyOn(api, 'reviewFailure').mockRejectedValue(
      new ApiError(
        'semantic judge is not configured',
        503,
        'judge_unavailable',
        undefined,
        false,
      ),
    )

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '复核测试' }))

    await waitFor(() => expect(review).toHaveBeenCalledWith('exp-stream-error', 'run-failed', 0, false))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '语义 Judge 未配置，确定性失败证据保持不变。请先配置模型端点与 API Key。',
    )
  })

  it('explains when the Judge context limit prevents an external request', async () => {
    const completed = experiment('completed', 12)
    vi.spyOn(api, 'bootstrap').mockResolvedValue({
      experiment: completed,
      candidates: [],
      tasks: [],
      calibration: completed.judge_calibration,
    })
    vi.spyOn(api, 'reviewFailure').mockRejectedValue(
      new ApiError(
        'semantic judge context exceeds configured size limit',
        413,
        'judge_context_too_large',
        undefined,
        false,
      ),
    )

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '复核测试' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '失败归因范围超过 Judge 上下文上限，未发送外部请求。请缩小事件负载或调整受控配置。',
    )
  })

  it('renders an actionable local executor start failure', async () => {
    const completed = experiment('completed', 12)
    vi.spyOn(api, 'bootstrap').mockResolvedValue({
      experiment: completed,
      candidates: [],
      tasks: [],
      calibration: completed.judge_calibration,
    })
    vi.spyOn(api, 'createExperiment').mockRejectedValue(
      new ApiError(
        'local experiment executor unavailable',
        503,
        'executor_unavailable',
        'exp-local-failed',
        true,
      ),
    )

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>,
    )

    expect(await screen.findByTestId('experiment-status')).toHaveTextContent('completed')
    fireEvent.click(screen.getByRole('button', { name: '运行测试' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '本地测评执行器无法启动。请检查进程资源后重试。实验 exp-local-failed 已记录为失败',
    )
  })
})
