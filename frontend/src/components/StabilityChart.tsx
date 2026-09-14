import type { Experiment } from '../types'

const taskLabels: Record<string, string> = {
  'task-1': '查询客户订单', 'task-2': '核验退款资格', 'task-3': '创建售后工单',
  'task-4': '修改收货地址', 'task-5': '解释账单差异', 'task-6': '升级高优投诉',
  'generate-and-verify': '代码生成并验证', 'repair-and-verify': '缺陷修复并验证',
  'static-analysis': '静态分析', 'explain-without-execution': '无需执行的解释',
  'execute-known-result': '执行已知结果', 'unsafe-code-refusal': '危险代码拒绝',
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
  const baseline = experiment.baseline
  return (
    <section className="stability">
      <div className="section-title"><h3>重复运行稳定性</h3><div className="legend"><span className="legend__candidate">{experiment.candidate.version}</span>{baseline ? <span className="legend__baseline">{baseline.version}</span> : null}</div></div>
      <div className="stability__head"><span>任务</span><span>{experiment.candidate.version} 成功率（95% 区间）</span><span>{baseline ? `${baseline.version} 成功率（95% 区间）` : '未配置基线'}</span></div>
      {Object.entries(experiment.metrics.by_task).map(([taskId, metric]) => {
        const baselineMetric = experiment.comparison.baseline_metrics?.by_task?.[taskId]
        return (
          <div className="stability__row" key={taskId}>
            <span>{taskLabels[taskId] ?? taskId}</span>
            <div className="range-cell"><b>{(metric.success_rate * 100).toFixed(0)}%</b><Range rate={metric.success_rate} interval={metric.interval} /></div>
            {baselineMetric ? <div className="range-cell"><b>{(baselineMetric.success_rate * 100).toFixed(0)}%</b><Range baseline rate={baselineMetric.success_rate} interval={baselineMetric.interval} /></div> : <div className="range-cell"><b>—</b></div>}
          </div>
        )
      })}
      <p className="stability__note">{experiment.comparison.message}</p>
    </section>
  )
}
