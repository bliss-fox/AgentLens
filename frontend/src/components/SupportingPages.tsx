import { Download, FileCheck2, GitCompareArrows, ShieldCheck } from 'lucide-react'
import type { Experiment } from '../types'
import type { PageKey } from './Sidebar'
import { StabilityChart } from './StabilityChart'

const taskNames: Record<string, string> = {
  'task-1': '查询客户订单', 'task-2': '核验退款资格', 'task-3': '创建售后工单',
  'task-4': '修改收货地址', 'task-5': '解释账单差异', 'task-6': '升级高优先级投诉',
}

export function SupportingPage({ page, experiment }: { page: Exclude<PageKey, 'agent'>; experiment: Experiment }) {
  const titles: Record<Exclude<PageKey, 'agent'>, string> = { compare: '实验对比', tasks: '任务集', runs: '运行记录', calibration: '裁判校准', snapshots: '环境快照' }
  const exportReport = () => {
    const report = JSON.stringify(experiment, null, 2)
    const url = URL.createObjectURL(new Blob([report], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = `${experiment.id}-report.json`; link.click(); URL.revokeObjectURL(url)
  }
  return (
    <main className="support-page">
      <header><div><h1>{titles[page]}</h1><p>{page === 'compare' ? '同任务、同种子、同环境快照下的候选版本对比' : '可复现测评资产与运行证据'}</p></div><button className="button button--primary-outline" onClick={exportReport}><Download size={16} />导出报告</button></header>
      {page === 'compare' ? <ComparePage experiment={experiment} /> : null}
      {page === 'tasks' ? <TasksPage experiment={experiment} /> : null}
      {page === 'runs' ? <RunsPage experiment={experiment} /> : null}
      {page === 'calibration' ? <CalibrationPage experiment={experiment} /> : null}
      {page === 'snapshots' ? <SnapshotsPage experiment={experiment} /> : null}
    </main>
  )
}

function ComparePage({ experiment }: { experiment: Experiment }) {
  return <div className="page-stack"><div className="verdict-banner"><GitCompareArrows /><div><b>{experiment.comparison.message}</b><span>配对差值 {(experiment.comparison.difference * 100).toFixed(1)} 个百分点，95% 区间 {(experiment.comparison.interval[0] * 100).toFixed(1)} 至 {(experiment.comparison.interval[1] * 100).toFixed(1)}</span></div></div><StabilityChart experiment={experiment} /><CandidateFingerprint experiment={experiment} /></div>
}

function CandidateFingerprint({ experiment }: { experiment: Experiment }) {
  return <section className="data-section"><h2>候选版本指纹</h2><div className="fingerprint-grid">{[experiment.candidate, experiment.baseline].map((candidate) => <div key={candidate.id}><h3>{candidate.name} / {candidate.version}</h3><dl><dt>模型</dt><dd>{candidate.model}</dd><dt>Prompt</dt><dd>{candidate.prompt_hash}</dd><dt>脚手架</dt><dd>{candidate.scaffold_version}</dd><dt>工具 Schema</dt><dd>{candidate.tool_schema_hash}</dd></dl></div>)}</div></section>
}

function TasksPage({ experiment }: { experiment: Experiment }) {
  return <section className="data-section"><h2>客服工具任务集 / v2</h2><div className="rows-table"><div className="rows-table__head"><span>任务</span><span>运行次数</span><span>成功率</span><span>95% 区间</span></div>{Object.entries(experiment.metrics.by_task).map(([id, metric]) => <div key={id}><span><FileCheck2 size={16} />{taskNames[id]}</span><span>{metric.runs}</span><b>{(metric.success_rate * 100).toFixed(0)}%</b><span>{(metric.interval[0] * 100).toFixed(0)}%–{(metric.interval[1] * 100).toFixed(0)}%</span></div>)}</div></section>
}

function RunsPage({ experiment }: { experiment: Experiment }) {
  return <section className="data-section"><h2>最近运行 · {experiment.completed_runs} 条</h2><div className="rows-table runs-table"><div className="rows-table__head"><span>运行</span><span>任务</span><span>结果</span><span>轨迹评分</span><span>成本</span><span>工具</span></div>{experiment.runs.slice(0, 18).map((run) => <div key={run.run_id}><code>{run.run_id.replace('support-', '')}</code><span>{taskNames[run.task_id]}</span><span className={run.success ? 'tag tag--pass' : 'tag tag--fail'}>{run.success ? '通过' : '失败'}</span><b>{(run.trajectory_score * 100).toFixed(0)}</b><span>¥{run.cost_cny?.toFixed(2)}</span><span>{run.tool_calls}</span></div>)}</div></section>
}

function CalibrationPage({ experiment }: { experiment: Experiment }) {
  const c = experiment.judge_calibration
  return <div className="page-stack"><div className="verdict-banner verdict-banner--green"><ShieldCheck /><div><b>裁判校准已通过 · {(c.accuracy * 100).toFixed(1)}%</b><span>{c.correct}/{c.total} 个标注样本一致，语义裁判可参与诊断但不作为唯一真值。</span></div></div><section className="data-section"><h2>混淆矩阵</h2><div className="confusion"><div><span>TP</span><b>{c.confusion_matrix.tp}</b></div><div><span>FP</span><b>{c.confusion_matrix.fp}</b></div><div><span>FN</span><b>{c.confusion_matrix.fn}</b></div><div><span>TN</span><b>{c.confusion_matrix.tn}</b></div></div><h3>待人工复核</h3><p>{c.disagreements.join('、')}</p></section></div>
}

function SnapshotsPage({ experiment }: { experiment: Experiment }) {
  const snapshots = [['环境快照', 'customer-tools-v2:cassette-2026-08-12'], ['候选指纹', experiment.candidate.scaffold_version], ['基线指纹', experiment.baseline.scaffold_version], ['价格表', 'pricing-cny-2026-08'], ['评测器', 'agentlens-evaluator@0.1.0']]
  return <section className="data-section"><h2>冻结资产</h2><div className="snapshot-list">{snapshots.map(([label, value]) => <div key={label}><span>{label}</span><code>{value}</code><b>已锁定</b></div>)}</div></section>
}

