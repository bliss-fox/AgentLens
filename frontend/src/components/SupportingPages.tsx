import { Download, FileCheck2, GitCompareArrows, ShieldCheck } from 'lucide-react'
import { formatCostCny } from '../format'
import type { Experiment } from '../types'
import type { PageKey } from './Sidebar'
import { StabilityChart } from './StabilityChart'

const taskNames: Record<string, string> = {
  'task-1': '查询客户订单', 'task-2': '核验退款资格', 'task-3': '创建售后工单',
  'task-4': '修改收货地址', 'task-5': '解释账单差异', 'task-6': '升级高优先级投诉',
  'generate-and-verify': '代码生成并验证', 'repair-and-verify': '缺陷修复并验证',
  'static-analysis': '静态分析', 'explain-without-execution': '无需执行的解释',
  'execute-known-result': '执行已知结果', 'unsafe-code-refusal': '危险代码拒绝',
}

export function SupportingPage({ page, experiment }: { page: Exclude<PageKey, 'agent'>; experiment: Experiment }) {
  const titles: Record<Exclude<PageKey, 'agent'>, string> = { compare: '实验对比', tasks: '任务集', runs: '运行记录', calibration: '裁判校准', snapshots: '环境快照' }
  const exportReport = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(experiment, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `${experiment.id}-report.json`
    link.click()
    URL.revokeObjectURL(url)
  }
  return (
    <main className="support-page">
      <header><div><h1>{titles[page]}</h1><p>{page === 'compare' ? '同任务、同种子、同环境快照下的候选版本对比' : '可复现测评资产与运行证据'}</p></div><button className="button button--primary-outline" onClick={exportReport}><Download size={16} />导出报告</button></header>
      {page === 'compare' ? <ComparePage experiment={experiment} /> : null}
      {page === 'runs' ? <RunsPage experiment={experiment} /> : null}
      {page === 'calibration' ? <CalibrationPage experiment={experiment} /> : null}
      {page === 'snapshots' ? <SnapshotsPage experiment={experiment} /> : null}
    </main>
  )
}

function ComparePage({ experiment }: { experiment: Experiment }) {
  return <div className="page-stack"><div className="verdict-banner"><GitCompareArrows /><div><b>{experiment.comparison.message}</b><span>{experiment.baseline ? `配对差值 ${(experiment.comparison.difference * 100).toFixed(1)} 个百分点，95% 区间 ${(experiment.comparison.interval[0] * 100).toFixed(1)} 至 ${(experiment.comparison.interval[1] * 100).toFixed(1)}` : '单候选实验不会计算配对差值。'}</span></div></div><StabilityChart experiment={experiment} /><CandidateFingerprint experiment={experiment} /></div>
}

function CandidateFingerprint({ experiment }: { experiment: Experiment }) {
  const candidates = experiment.baseline ? [experiment.candidate, experiment.baseline] : [experiment.candidate]
  return <section className="data-section"><h2>候选版本指纹</h2><div className="fingerprint-grid">{candidates.map((candidate) => <div key={candidate.id}><h3>{candidate.name} / {candidate.version}</h3><dl><dt>模型</dt><dd>{candidate.model}</dd><dt>Prompt</dt><dd>{candidate.prompt_hash}</dd><dt>脚手架</dt><dd>{candidate.scaffold_version}</dd><dt>工具 Schema</dt><dd>{candidate.tool_schema_hash}</dd>{candidate.endpoint ? <><dt>Endpoint</dt><dd>{candidate.endpoint}</dd></> : null}</dl></div>)}</div></section>
}

function RunsPage({ experiment }: { experiment: Experiment }) {
  return <section className="data-section"><h2>候选运行 · {experiment.runs.length} 条</h2><div className="rows-table runs-table"><div className="rows-table__head"><span>运行</span><span>任务</span><span>结果</span><span>轨迹评分</span><span>成本</span><span>工具</span></div>{experiment.runs.slice(0, 18).map((run) => <div key={run.run_id}><code>{run.run_id}</code><span><FileCheck2 size={16} />{taskNames[run.task_id] ?? run.task_id}</span><span className={run.success ? 'tag tag--pass' : 'tag tag--fail'}>{run.success ? '通过' : '失败'}</span><b>{(run.trajectory_score * 100).toFixed(0)}</b><span>{run.cost_cny === null ? '不完整' : formatCostCny(run.cost_cny)}</span><span>{run.tool_calls}</span></div>)}</div></section>
}

function CalibrationPage({ experiment }: { experiment: Experiment }) {
  const c = experiment.judge_calibration
  return <div className="page-stack"><div className={`verdict-banner ${c.passed ? 'verdict-banner--green' : ''}`}><ShieldCheck /><div><b>{c.passed ? `裁判校准已通过 · ${(c.accuracy * 100).toFixed(1)}%` : '语义裁判尚未校准'}</b><span>{c.total ? `${c.correct}/${c.total} 个标注样本一致。` : '当前结论仅使用确定性断言；语义评分只作诊断。'}</span></div></div><section className="data-section"><h2>混淆矩阵</h2><div className="confusion"><div><span>TP</span><b>{c.confusion_matrix.tp}</b></div><div><span>FP</span><b>{c.confusion_matrix.fp}</b></div><div><span>FN</span><b>{c.confusion_matrix.fn}</b></div><div><span>TN</span><b>{c.confusion_matrix.tn}</b></div></div></section></div>
}

function SnapshotsPage({ experiment }: { experiment: Experiment }) {
  const snapshots = [['任务集', experiment.benchmark_name], ['候选指纹', experiment.candidate.scaffold_version], ...(experiment.baseline ? [['基线指纹', experiment.baseline.scaffold_version]] : []), ['价格表', 'pricing-cny-2026-08'], ['评测器', 'agentlens-evaluator@0.1.0']]
  return <section className="data-section"><h2>冻结资产</h2><div className="snapshot-list">{snapshots.map(([label, value]) => <div key={label}><span>{label}</span><code>{value}</code><b>已锁定</b></div>)}</div></section>
}
