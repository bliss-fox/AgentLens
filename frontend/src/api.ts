import type { BootstrapData, Experiment } from './types'

const API_URL = import.meta.env.VITE_API_URL ?? ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, init)
  if (!response.ok) throw new Error(`API ${response.status}: ${await response.text()}`)
  return response.json() as Promise<T>
}

export const api = {
  bootstrap: () => request<BootstrapData>('/api/v1/bootstrap'),
  experiments: () => request<Experiment[]>('/api/v1/experiments'),
  createExperiment: (payload: { prompt: string; candidate_id: string; baseline_candidate_id: string; benchmark_id: string; repetitions: number }) =>
    request<Experiment>('/api/v1/experiments', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    }),
  cancelExperiment: (id: string) => request<Experiment>(`/api/v1/experiments/${id}/cancel`, { method: 'POST' }),
  streamExperiment: (
    id: string,
    handlers: {
      onProgress: (completed: number, total: number) => void
      onResult: (experiment: Experiment) => void
      onCancelled: () => void
      onError: () => void
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
    source.addEventListener('experiment.error', () => {
      handlers.onError()
      source.close()
    })
    source.onerror = () => {
      handlers.onError()
      source.close()
    }
    return () => source.close()
  },
}
