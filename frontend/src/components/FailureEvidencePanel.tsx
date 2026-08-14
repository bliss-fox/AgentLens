import { ArrowRight, CheckCircle2, Search, XCircle } from 'lucide-react'
import type { Experiment, Run } from '../types'

export function FailureEvidencePanel({ experiment, selectedRun, onOpenTrace }: { experiment: Experiment; selectedRun: Run | undefined; onOpenTrace: () => void }) {
  const failureEntries = Object.entries(experiment.failures).slice(0, 4)
  const evidence = selectedRun?.failures[0]
  const steps = selectedRun?.trajectory.length ? selectedRun.trajectory.map((item) => item.name) : ['search_customer', 'search_customer ×4', '未验证结果', '宣布完成']
  return (
    <section className="failure-section">
      <h3>已发现失败</h3>
      <div className="failure-grid">
        <div className="failure-list">
          {failureEntries.map(([label, count], index) => <button key={label} className={index === 0 ? 'selected' : ''}><span>{label}</span><b>{count}</b></button>)}
        </div>
        <div className="evidence-detail">
          <h4>证据详情{evidence ? `（${evidence.label}）` : ''}</h4>
          <ol className="trajectory-mini">
            {steps.slice(0, 4).map((step, index) => (
              <li key={`${step}-${index}`} className={index === 1 ? 'problem' : ''}>
                <span>{index + 1}</span><b>{step}</b><time>10:23:{11 + index * 3}</time>
              </li>
            ))}
          </ol>
          <p>归因：规则命中；裁判复核：支持；证据充分</p>
          <button className="text-button" onClick={onOpenTrace}>查看完整轨迹 <ArrowRight size={15} /></button>
        </div>
      </div>
      <div className="judge-trust">
        {experiment.judge_calibration.passed ? <CheckCircle2 size={19} /> : <XCircle size={19} />}
        <div><b>裁判可信度</b><span>校准集 {experiment.judge_calibration.correct}/{experiment.judge_calibration.total} 一致 · {experiment.judge_calibration.disagreements.length} 个分歧样本待人工复核</span></div>
      </div>
    </section>
  )
}

