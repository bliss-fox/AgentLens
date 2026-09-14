import { BarChart3, Bot, ClipboardList, Clock3, FlaskConical, Gauge, Settings, ShieldCheck } from 'lucide-react'
import { BrandMark } from './BrandMark'

export type PageKey = 'agent' | 'compare' | 'tasks' | 'runs' | 'calibration' | 'snapshots'

const items = [
  ['agent', '测评 Agent', Bot],
  ['compare', '实验对比', BarChart3],
  ['tasks', '任务集', ClipboardList],
  ['runs', '运行记录', Clock3],
  ['calibration', '裁判校准', ShieldCheck],
  ['snapshots', '环境快照', FlaskConical],
] as const

export function Sidebar({ page, onPage }: { page: PageKey; onPage: (page: PageKey) => void }) {
  return (
    <aside className="sidebar">
      <div className="brand"><BrandMark /><span>AgentLens</span></div>
      <nav aria-label="主导航">
        {items.map(([key, label, Icon]) => (
          <button key={key} className={`nav-item ${page === key ? 'nav-item--active' : ''}`} onClick={() => onPage(key)}>
            <Icon size={19} strokeWidth={1.8} /><span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar__spacer" />
      <button className="nav-item"><Settings size={19} /><span>设置</span></button>
      <div className="sidebar__status"><Gauge size={14} /><span>本地真实服务</span></div>
    </aside>
  )
}
