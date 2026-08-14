export type PlanStep = {
  key: string
  label: string
  status: 'completed' | 'active' | 'queued' | 'failed'
  detail?: string | null
}

export type Candidate = {
  id: string
  name: string
  version: string
  model: string
  prompt_hash: string
  scaffold_version: string
  tool_schema_hash: string
}

export type FailureEvidence = {
  category: string
  label: string
  severity: string
  event_range: [number, number]
  rule: string
  explanation: string
  judge_verdict: string
  confidence: string
}

export type TraceEvent = {
  run_id: string
  seq: number
  timestamp: string
  type: string
  payload: Record<string, unknown>
}

export type Run = {
  run_id: string
  task_id: string
  candidate_id: string
  seed: number
  success: boolean
  final_answer: string
  trajectory_score: number
  cost_cny: number | null
  cost_complete: boolean
  tool_calls: number
  duration_seconds: number
  failures: FailureEvidence[]
  events: TraceEvent[]
  trajectory: { name: string; arguments: Record<string, unknown>; seq: number }[]
}

export type TaskMetric = {
  success_rate: number
  interval: [number, number]
  runs: number
}

export type Experiment = {
  id: string
  status: 'queued' | 'running' | 'completed' | 'cancelled' | 'failed'
  prompt: string
  candidate: Candidate
  baseline: Candidate
  benchmark_name: string
  completed_runs: number
  total_runs: number
  plan: PlanStep[]
  runs: Run[]
  metrics: {
    success_rate: number
    success_interval: [number, number]
    average_cost_cny: number | null
    cost_complete: boolean
    average_tool_calls: number
    average_trajectory_score: number
    by_task: Record<string, TaskMetric>
  }
  comparison: {
    baseline_metrics: Experiment['metrics']
    difference: number
    interval: [number, number]
    significant: boolean
    message: string
  }
  judge_calibration: {
    total: number
    correct: number
    accuracy: number
    passed: boolean
    confusion_matrix: Record<string, number>
    disagreements: string[]
    mode: string
  }
  failures: Record<string, number>
  verdict: {
    passed: boolean
    label: string
    reason: string
    thresholds: Record<string, number>
  }
  created_at: string
}

export type BootstrapData = {
  experiment: Experiment
  candidates: Candidate[]
  tasks: Array<Record<string, unknown>>
  calibration: Experiment['judge_calibration']
}

