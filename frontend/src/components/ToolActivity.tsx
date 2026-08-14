import { CheckCircle2, CircleDashed } from 'lucide-react'

const tools = [
  ['snapshot_environment', '已完成', '1.82s'],
  ['check_judge_calibration', '已完成', '9.34s'],
  ['run_trials · 任务 1–6', '运行中', '03:24'],
  ['analyze_trajectories', '排队中', '—'],
] as const

export function ToolActivity({ running }: { running: boolean }) {
  return (
    <section className="tool-activity">
      <h3>工具调用</h3>
      <div className="tool-table tool-table--head"><span>工具</span><span>状态</span><span>开始时间</span><span>耗时</span></div>
      {tools.map(([name, originalStatus, duration], index) => {
        const status = index === 2 && !running ? '已完成' : originalStatus
        const active = status === '运行中'
        return (
          <div className={`tool-table ${active ? 'tool-table--active' : ''}`} key={name}>
            <code>{name}</code>
            <span className={`status-text status-text--${active ? 'active' : status === '已完成' ? 'ok' : 'muted'}`}>
              {active ? <CircleDashed size={13} /> : status === '已完成' ? <CheckCircle2 size={13} /> : null}{status}
            </span>
            <time>{index < 3 ? `10:${21 + index}:0${index + 6}` : '—'}</time><span>{duration}</span>
          </div>
        )
      })}
    </section>
  )
}

