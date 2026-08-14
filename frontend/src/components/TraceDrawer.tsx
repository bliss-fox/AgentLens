import { X } from 'lucide-react'
import type { Run } from '../types'

export function TraceDrawer({ run, open, onClose }: { run?: Run; open: boolean; onClose: () => void }) {
  if (!open || !run) return null
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="trace-drawer" onClick={(event) => event.stopPropagation()} aria-label="完整轨迹">
        <header><div><span>完整轨迹</span><b>{run.run_id}</b></div><button onClick={onClose} aria-label="关闭"><X /></button></header>
        <div className="trace-summary"><span className={run.success ? 'pass' : 'fail'}>{run.success ? '通过' : '失败'}</span><span>轨迹评分 {(run.trajectory_score * 100).toFixed(0)}</span><span>¥{run.cost_cny?.toFixed(2)}</span><span>{run.duration_seconds}s</span></div>
        <ol className="full-trace">
          {run.events.map((event) => (
            <li key={event.seq}><span>{event.seq}</span><div><b>{event.type}</b><time>{new Date(event.timestamp).toLocaleTimeString('zh-CN')}</time><pre>{JSON.stringify(event.payload, null, 2)}</pre></div></li>
          ))}
        </ol>
      </aside>
    </div>
  )
}

