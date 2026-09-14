import { ArrowRight, CheckCircle2, XCircle } from 'lucide-react'
import type { Experiment, Run } from '../types'

const judgeLabels: Record<string, string> = {
  support: '支持规则归因',
  conflict: '与规则归因冲突',
  not_run: '未运行',
}

const confidenceLabels: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}

export function FailureEvidencePanel({
  experiment,
  selectedRun,
  onOpenTrace,
  onReview,
  reviewing,
}: {
  experiment: Experiment
  selectedRun: Run | undefined
  onOpenTrace: () => void
  onReview: (runId: string, failureIndex: number, force: boolean) => void
  reviewing: boolean
}) {
  const failureEntries = Object.entries(experiment.failures).slice(0, 4)
  const evidence = selectedRun?.failures[0]
  const steps = selectedRun?.trajectory.length ? selectedRun.trajectory.map((item) => item.name) : []
  const judgeReview = evidence ? (judgeLabels[evidence.judge_verdict] ?? evidence.judge_verdict) : '无失败证据'
  const confidence = evidence ? (confidenceLabels[evidence.confidence] ?? evidence.confidence) : '—'
  const requestReview = () => {
    if (!selectedRun || !evidence || experiment.status !== 'completed') return
    const force = evidence.judge_verdict !== 'not_run'
    if (force && !window.confirm('重新复核会再次发送事件并产生一次新的模型调用，是否继续？')) return
    onReview(selectedRun.run_id, 0, force)
  }

  return (
    <section className="failure-section">
      <h3>已发现失败</h3>
      <div className="failure-grid">
        <div className="failure-list">
          {failureEntries.map(([label, count], index) => <button type="button" key={label} className={index === 0 ? 'selected' : ''}><span>{label}</span><b>{count}</b></button>)}
        </div>
        <div className="evidence-detail">
          <h4>证据详情{evidence ? `（${evidence.label}）` : ''}</h4>
          {steps.length ? (
            <ol className="trajectory-mini">
              {steps.slice(0, 4).map((step, index) => (
                <li key={`${step}-${index}`} className={index === 1 ? 'problem' : ''}>
                  <span>{index + 1}</span><b>{step}</b><time>事件 #{selectedRun?.trajectory[index]?.seq ?? index + 1}</time>
                </li>
              ))}
            </ol>
          ) : <p>当前没有可展示的失败轨迹。</p>}
          <p>{evidence ? `归因规则：${evidence.rule}；裁判复核：${judgeReview}；置信度：${confidence}` : judgeReview}</p>
          {evidence?.judge_explanation ? <p className="judge-explanation">Judge：{evidence.judge_explanation} · 引用事件 #{evidence.judge_evidence_sequences.join('、#')}</p> : null}
          <div className="evidence-actions">
            <button className="text-button" onClick={onOpenTrace} disabled={!selectedRun}>查看完整轨迹 <ArrowRight size={15} /></button>
            <button className="text-button" onClick={requestReview} disabled={!selectedRun || !evidence || reviewing || experiment.status !== 'completed'}>{reviewing ? '复核中…' : evidence?.judge_verdict === 'not_run' ? '语义复核' : '重新复核（再次调用）'}</button>
          </div>
          {evidence ? <p className="judge-disclosure">仅点击后发送归因范围内的持久化事件；凭据字段会脱敏，其他评测内容仍会发送，并可能产生模型费用。已有结果默认不会重复调用，重新复核需再次确认。</p> : null}
        </div>
      </div>
      <div className="judge-trust">
        {experiment.judge_calibration.passed ? <CheckCircle2 size={19} /> : <XCircle size={19} />}
        <div><b>裁判校准</b><span>fixture {experiment.judge_calibration.correct}/{experiment.judge_calibration.total} 一致 · 模式 {experiment.judge_calibration.mode}</span></div>
      </div>
    </section>
  )
}
