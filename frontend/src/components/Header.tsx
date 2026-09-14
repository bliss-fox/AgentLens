import { GitCompareArrows, ListTree, Plus } from 'lucide-react'
import type { Benchmark, Candidate } from '../types'

export function Header({
  candidates,
  benchmarks,
  candidateId,
  benchmarkId,
  onCandidate,
  onBenchmark,
  onNew,
}: {
  candidates: Candidate[]
  benchmarks: Benchmark[]
  candidateId: string
  benchmarkId: string
  onCandidate: (id: string) => void
  onBenchmark: (id: string) => void
  onNew: () => void
}) {
  return (
    <header className="header">
      <h1>测评 Agent</h1>
      <div className="header__selectors">
        <label>
          <span>候选版本</span>
          <div className="select-shell"><GitCompareArrows size={16} /><select aria-label="候选版本" value={candidateId} onChange={(event) => onCandidate(event.target.value)}>{candidates.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.name} / {candidate.version}</option>)}</select></div>
        </label>
        <label>
          <span>基准任务集</span>
          <div className="select-shell"><ListTree size={16} /><select aria-label="基准任务集" value={benchmarkId} onChange={(event) => onBenchmark(event.target.value)}>{benchmarks.map((benchmark) => <option key={benchmark.id} value={benchmark.id}>{benchmark.name}</option>)}</select></div>
        </label>
        <button className="button button--primary-outline" onClick={onNew}><Plus size={17} />新建测评</button>
      </div>
    </header>
  )
}
