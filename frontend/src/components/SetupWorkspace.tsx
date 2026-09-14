import { useEffect, useState } from 'react'
import { CheckCircle2, FlaskConical, PlugZap, Save, Send } from 'lucide-react'
import type { Benchmark, Candidate } from '../types'

export function SetupWorkspace({
  candidates,
  benchmarks,
  candidateId,
  benchmarkId,
  running,
  onRun,
  onSaveCandidate,
  onTestCandidate,
  onSaveBenchmark,
}: {
  candidates: Candidate[]
  benchmarks: Benchmark[]
  candidateId: string
  benchmarkId: string
  running: boolean
  onRun: (prompt: string, baselineId: string | null, repetitions: number) => void
  onSaveCandidate: (candidate: Candidate) => Promise<void>
  onTestCandidate: (candidateId: string) => Promise<void>
  onSaveBenchmark: (benchmark: Benchmark) => Promise<void>
}) {
  const candidate = candidates.find((item) => item.id === candidateId)
  const benchmark = benchmarks.find((item) => item.id === benchmarkId)
  const [prompt, setPrompt] = useState('评估 Coding Agent 的工具选择、执行验证、稳定性、成本和失败原因。')
  const [baselineId, setBaselineId] = useState('')
  const [repetitions, setRepetitions] = useState(3)
  const [candidateDraft, setCandidateDraft] = useState<Candidate | null>(candidate ?? null)
  const [benchmarkJson, setBenchmarkJson] = useState(benchmark ? JSON.stringify(benchmark, null, 2) : '')
  const [status, setStatus] = useState<string | null>(null)

  useEffect(() => setCandidateDraft(candidate ?? null), [candidate])
  useEffect(() => setBenchmarkJson(benchmark ? JSON.stringify(benchmark, null, 2) : ''), [benchmark])

  const saveBenchmark = async () => {
    try {
      const parsed = JSON.parse(benchmarkJson) as Benchmark
      await onSaveBenchmark(parsed)
      setStatus('任务集 JSON 已校验并保存。')
    } catch (error) {
      setStatus(error instanceof Error ? `任务集保存失败：${error.message}` : '任务集保存失败。')
    }
  }

  return (
    <main className="setup-workspace">
      <section className="setup-hero">
        <div><span className="eyebrow">REAL HTTP EVALUATION</span><h2>运行一次真实测评</h2><p>每次运行都会调用候选 Agent，工具请求经过授权网关，轨迹与用量事件写入数据库。默认 smoke 为每题 3 次；正式评测为每题 10 次。</p></div>
        <div className="readiness-card"><CheckCircle2 size={20} /><div><b>配置资产已就绪</b><span>{candidates.length} 个候选 · {benchmark?.tasks.length ?? 0} 个任务</span></div></div>
      </section>

      <section className="setup-grid">
        <div className="setup-card setup-card--run">
          <h3><FlaskConical size={18} />测评设置</h3>
          <label>测评目标<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} /></label>
          <div className="field-row">
            <label>重复次数<div className="segment">{[3, 10].map((value) => <button key={value} className={repetitions === value ? 'active' : ''} onClick={() => setRepetitions(value)}>{value === 3 ? 'Smoke · 3' : '正式 · 10'}</button>)}</div></label>
            <label>可选基线<select value={baselineId} onChange={(event) => setBaselineId(event.target.value)}><option value="">不对比（单候选）</option>{candidates.filter((item) => item.id !== candidateId).map((item) => <option key={item.id} value={item.id}>{item.name} / {item.version}</option>)}</select></label>
          </div>
          <button className="button button--primary run-real" disabled={running || !candidate || !benchmark || !prompt.trim()} onClick={() => onRun(prompt.trim(), baselineId || null, repetitions)}><Send size={17} />{running ? '正在运行…' : `开始真实测评 · ${(benchmark?.tasks.length ?? 0) * repetitions * (baselineId ? 2 : 1)} 次`}</button>
        </div>

        <div className="setup-card">
          <h3><PlugZap size={18} />候选 Agent 连接</h3>
          {candidateDraft ? <div className="form-stack">
            <label>Endpoint<input value={candidateDraft.endpoint ?? ''} onChange={(event) => setCandidateDraft({ ...candidateDraft, endpoint: event.target.value })} /></label>
            <div className="field-row"><label>版本<input value={candidateDraft.version} onChange={(event) => setCandidateDraft({ ...candidateDraft, version: event.target.value })} /></label><label>模型标签<input value={candidateDraft.model} onChange={(event) => setCandidateDraft({ ...candidateDraft, model: event.target.value })} /></label></div>
            <div className="button-row"><button className="button" onClick={() => onSaveCandidate(candidateDraft)}><Save size={15} />保存</button><button className="button button--primary-outline" onClick={() => onTestCandidate(candidateDraft.id)}>测试连接</button></div>
          </div> : <p>请先选择候选 Agent。</p>}
        </div>
      </section>

      <section className="setup-card task-import">
        <div><h3>任务集 JSON 编辑 / 导入</h3><p>编辑完整 schema 后保存。后续实验会冻结当前版本，不受再次编辑影响。</p></div>
        <textarea aria-label="任务集 JSON" value={benchmarkJson} onChange={(event) => setBenchmarkJson(event.target.value)} spellCheck={false} />
        <div className="button-row"><button className="button" disabled={!benchmarkJson.trim()} onClick={saveBenchmark}><Save size={15} />校验并保存任务集</button>{status ? <span className="save-status" role="status">{status}</span> : null}</div>
      </section>
    </main>
  )
}
