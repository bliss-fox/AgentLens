import { CheckCircle2, CircleDashed, CircleX, Clock3 } from 'lucide-react'
import type { Experiment, PlanStep } from '../types'

const toolNames: Record<string, string> = {
  snapshot: 'snapshot_environment',
  calibrate: 'calibrate_judge',
  trials: 'run_trials',
  analyze: 'analyze_trajectories',
  attribute: 'attribute_failures',
  compare: 'compare_candidates',
  report: 'generate_report',
}

const statusLabels: Record<PlanStep['status'], string> = {
  completed: '已完成',
  active: '运行中',
  queued: '排队中',
  failed: '失败',
}

function formatDuration(seconds: number) {
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${(seconds % 60).toFixed(0)}s`
}

export function ToolActivity({ experiment }: { experiment: Experiment }) {
  const trialDuration = [...experiment.runs, ...experiment.baseline_runs]
    .reduce((total, run) => total + run.duration_seconds, 0)

  return (
    <section className="tool-activity">
      <h3>工具调用</h3>
      <div className="tool-table tool-table--head"><span>工具</span><span>状态</span><span>运行证据</span><span>累计耗时</span></div>
      {experiment.plan.map((step) => {
        const status = statusLabels[step.status]
        const Icon = step.status === 'completed' ? CheckCircle2 : step.status === 'active' ? CircleDashed : step.status === 'failed' ? CircleX : Clock3
        const evidence = step.detail ?? (step.key === 'trials' ? `${experiment.completed_runs} / ${experiment.total_runs}` : '状态已持久化')
        const duration = step.key === 'trials' && trialDuration > 0 ? formatDuration(trialDuration) : '未记录'
        return (
          <div className={`tool-table ${step.status === 'active' ? 'tool-table--active' : ''}`} key={step.key}>
            <code>{toolNames[step.key] ?? step.key}</code>
            <span className={`status-text status-text--${step.status === 'active' ? 'active' : step.status === 'completed' ? 'ok' : step.status === 'failed' ? 'fail' : 'muted'}`}>
              <Icon size={13} />{status}
            </span>
            <span>{evidence}</span><span>{duration}</span>
          </div>
        )
      })}
    </section>
  )
}
