import type { Experiment } from '../types'

const taskLabels: Record<string, string> = {
  'task-1': '查询客户订单', 'task-2': '核验退款资格', 'task-3': '创建售后工单',
  'task-4': '修改收货地址', 'task-5': '解释账单差异', 'task-6': '升级高优投诉',
}

function Range({ rate, interval, baseline = false }: { rate: number; interval: [number, number]; baseline?: boolean }) {
  return (
    <div className={`range ${baseline ? 'range--baseline' : ''}`} aria-label={`${(rate * 100).toFixed(0)}%，区间 ${(interval[0] * 100).toFixed(0)} 至 ${(interval[1] * 100).toFixed(0)}`}>
      <span className="range__line" style={{ left: `${interval[0] * 100}%`, width: `${(interval[1] - interval[0]) * 100}%` }} />
      <span className="range__dot" style={{ left: `${rate * 100}%` }} />
    </div>
  )
}

export function StabilityChart({ experiment }: { experiment: Experiment }) {
  return (
    <section className="stability">
      <div className="section-title"><h3>重复运行稳定性</h3><div className="legend"><span className="legend__candidate">v1.4</span><span className="legend__baseline">v1.3</span></div></div>
      <div className="stability__head"><span>任务</span><span>v1.4 成功率（95% 区间）</span><span>v1.3 成功率（95% 区间）</span></div>
      {Object.entries(experiment.metrics.by_task).map(([taskId, metric]) => {
        const baseline = experiment.comparison.baseline_metrics.by_task[taskId]
        return (
          <div className="stability__row" key={taskId}>
            <span>{taskLabels[taskId] ?? taskId}</span>
            <div className="range-cell"><b>{(metric.success_rate * 100).toFixed(0)}%</b><Range rate={metric.success_rate} interval={metric.interval} /></div>
            <div className="range-cell"><b>{(baseline.success_rate * 100).toFixed(0)}%</b><Range baseline rate={baseline.success_rate} interval={baseline.interval} /></div>
          </div>
        )
      })}
      <p className="stability__note">{experiment.comparison.message}</p>
    </section>
  )
}

