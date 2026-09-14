import type { Benchmark, BootstrapData, Candidate, Experiment, FailureEvidence } from './types'

const API_URL = import.meta.env.VITE_API_URL ?? ''

type ErrorDetail = {
  category?: unknown
  message?: unknown
  experiment_id?: unknown
  retryable?: unknown
}

export type FailureReviewResponse = {
  experiment: Experiment
  failure: FailureEvidence
  review: {
    supported: boolean
    confidence: string
    category: string
    evidence_sequences: number[]
    explanation: string
  }
}

export type ExperimentStreamError = {
  category?: string
  message?: string
  experiment?: Experiment
}

export function parseExperimentStreamError(data: string): ExperimentStreamError {
  try {
    const event = JSON.parse(data) as {
      category?: unknown
      message?: unknown
      experiment?: unknown
    }
    const experiment = event.experiment
    return {
      category: typeof event.category === 'string' ? event.category : undefined,
      message: typeof event.message === 'string' ? event.message : undefined,
      experiment: (
        experiment
        && typeof experiment === 'object'
        && 'id' in experiment
        && typeof experiment.id === 'string'
        && 'status' in experiment
        && experiment.status === 'failed'
      ) ? experiment as Experiment : undefined,
    }
  } catch {
    return {
      category: 'invalid_event',
      message: '测评失败事件格式无效，请检查 API 日志。',
    }
  }
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly category?: string,
    readonly experimentId?: string,
    readonly retryable = false,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

function parseError(response: Response, body: string): ApiError {
  let detail: ErrorDetail | string | undefined
  try {
    const payload = JSON.parse(body) as { detail?: ErrorDetail | string }
    detail = payload.detail
  } catch {
    detail = undefined
  }

  if (detail && typeof detail === 'object') {
    return new ApiError(
      typeof detail.message === 'string' ? detail.message : 'API request failed (' + response.status + ')',
      response.status,
      typeof detail.category === 'string' ? detail.category : undefined,
      typeof detail.experiment_id === 'string' ? detail.experiment_id : undefined,
      detail.retryable === true,
    )
  }

  return new ApiError(
    typeof detail === 'string' ? detail : 'API request failed (' + response.status + ')',
    response.status,
  )
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, init)
  if (!response.ok) throw parseError(response, await response.text())
  return response.json() as Promise<T>
}

export const api = {
  bootstrap: () => request<BootstrapData>('/api/v1/bootstrap'),
  experiments: () => request<Experiment[]>('/api/v1/experiments'),
  createExperiment: (payload: { prompt: string; candidate_id: string; baseline_candidate_id: string | null; benchmark_id: string; execution_mode?: 'scripted' | 'http'; repetitions: number }) =>
    request<Experiment>('/api/v1/experiments', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    }),
  saveCandidate: (candidate: Candidate) =>
    request<Candidate>(`/api/v1/candidates/${candidate.id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(candidate),
    }),
  testCandidate: (candidateId: string) =>
    request<{ ok: boolean; status: Record<string, unknown>; candidate: Candidate }>(`/api/v1/candidates/${candidateId}/test`, { method: 'POST' }),
  saveBenchmark: (benchmark: Benchmark) =>
    request<Benchmark>(`/api/v1/benchmarks/${benchmark.id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(benchmark),
    }),
  cancelExperiment: (id: string) => request<Experiment>(`/api/v1/experiments/${id}/cancel`, { method: 'POST' }),
  reviewFailure: (experimentId: string, runId: string, failureIndex: number, force = false) =>
    request<FailureReviewResponse>(
      `/api/v1/experiments/${experimentId}/runs/${runId}/failures/${failureIndex}/review${force ? '?force=true' : ''}`,
      { method: 'POST' },
    ),
  streamExperiment: (
    id: string,
    handlers: {
      onProgress: (completed: number, total: number) => void
      onResult: (experiment: Experiment) => void
      onCancelled: () => void
      onError: (error: ExperimentStreamError) => void
    },
  ) => {
    const source = new EventSource(`${API_URL}/api/v1/experiments/${id}/events`)
    source.addEventListener('experiment.progress', (raw) => {
      const event = JSON.parse((raw as MessageEvent<string>).data) as { completed: number; total: number }
      handlers.onProgress(event.completed, event.total)
    })
    source.addEventListener('experiment.result', (raw) => {
      const event = JSON.parse((raw as MessageEvent<string>).data) as { experiment: Experiment }
      handlers.onResult(event.experiment)
      source.close()
    })
    source.addEventListener('experiment.cancelled', () => {
      handlers.onCancelled()
      source.close()
    })
    source.addEventListener('experiment.error', (raw) => {
      handlers.onError(parseExperimentStreamError((raw as MessageEvent<string>).data))
      source.close()
    })
    source.onerror = () => {
      handlers.onError({
        category: 'stream_disconnected',
        message: '实时进度连接已中断，请检查 API 与网络状态。',
      })
      source.close()
    }
    return () => source.close()
  },
}
