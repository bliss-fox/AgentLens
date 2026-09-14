import type { Experiment } from '../types'
import { formatCostCny } from '../format'

const percentage = (value: number) => `${(value * 100).toFixed(1)}%`

export function MetricRail({ experiment }: { experiment: Experiment }) {
  const metrics = experiment.metrics
  return (
    <div className="metric-rail">
      <div><span>任务成功率</span><strong>{percentage(metrics.success_rate)}</strong></div>
      <div><span>95% 稳定性区间</span><strong>{percentage(metrics.success_interval[0])}–{percentage(metrics.success_interval[1])}</strong></div>
      <div><span>平均成本</span><strong>{formatCostCny(metrics.average_cost_cny)}</strong></div>
      <div><span>工具调用</span><strong>{metrics.average_tool_calls.toFixed(1)} 次</strong></div>
    </div>
  )
}
