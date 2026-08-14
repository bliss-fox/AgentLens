import { ChevronDown, GitCompareArrows, ListTree, Plus } from 'lucide-react'
import type { Candidate } from '../types'

export function Header({ candidate, onNew }: { candidate: Candidate; onNew: () => void }) {
  return (
    <header className="header">
      <h1>测评 Agent</h1>
      <div className="header__selectors">
        <label><span>候选版本</span><button><GitCompareArrows size={16} />{candidate.name} / {candidate.version}<ChevronDown size={15} /></button></label>
        <label><span>基准任务集</span><button><ListTree size={16} />客服工具任务集 / v2<ChevronDown size={15} /></button></label>
        <button className="button button--primary-outline" onClick={onNew}><Plus size={17} />新建会话</button>
      </div>
    </header>
  )
}

