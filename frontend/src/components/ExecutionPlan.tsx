import { Check, CircleDashed, Clock3, X } from 'lucide-react'
import type { PlanStep } from '../types'

const icons = { completed: Check, active: CircleDashed, queued: Clock3, failed: X }

export function ExecutionPlan({ steps, progress }: { steps: PlanStep[]; progress?: { completed: number; total: number } }) {
  return (
    <section className="execution-panel">
      <h3>执行计划</h3>
      <ol>
        {steps.map((step, index) => {
          const status = step.key === 'trials' && progress && progress.completed < progress.total ? 'active' : step.status
          const Icon = icons[status]
          const detail = step.key === 'trials' && progress ? `${progress.completed} / ${progress.total}` : step.detail
          return (
            <li key={step.key} className={`plan-row plan-row--${status}`}>
              <span className="plan-row__icon"><Icon size={15} /></span>
              <b>{index + 1}.</b><span>{step.label}</span>
              <span className="plan-row__detail">{detail ?? (status === 'completed' ? '已完成' : status === 'active' ? '运行中' : '排队中')}</span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

