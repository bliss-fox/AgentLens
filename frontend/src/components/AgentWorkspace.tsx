import { useMemo, useState } from 'react'
import { Bot, CircleStop, RefreshCw, Send, User } from 'lucide-react'
import type { Experiment } from '../types'
import { BrandMark } from './BrandMark'
import { ExecutionPlan } from './ExecutionPlan'
import { FailureEvidencePanel } from './FailureEvidencePanel'
import { MetricRail } from './MetricRail'
import { StabilityChart } from './StabilityChart'
import { ToolActivity } from './ToolActivity'
import { TraceDrawer } from './TraceDrawer'

export function AgentWorkspace({
  experiment, running, progress, onRun, onCancel,
}: {
  experiment: Experiment
  running: boolean
  progress: { completed: number; total: number }
  onRun: (prompt: string) => void
  onCancel: () => void
}) {
  const [prompt, setPrompt] = useState(experiment.prompt)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const selectedRun = useMemo(() => experiment.runs.find((run) => !run.success && run.failures.some((item) => item.category === 'loop')) ?? experiment.runs.find((run) => !run.success), [experiment.runs])
  const run = () => prompt.trim() && onRun(prompt.trim())

  return (
    <div className="workspace">
      <main className="conversation-pane">
        <section className="request-section">
          <h2>评估请求</h2>
          <div className="message-row message-row--user"><span className="avatar avatar--user"><User size={17} /></span><div><b>你</b><p>{experiment.prompt}</p></div><time>10:21:03</time></div>
          <div className="message-row message-row--agent">
            <span className="avatar avatar--agent"><BrandMark small /></span>
            <div className="agent-message"><b>测评 Agent</b><ExecutionPlan steps={experiment.plan} progress={running ? progress : undefined} /></div>
            <time>10:21:05</time>
          </div>
        </section>
        <ToolActivity running={running} />
        <div className="composer">
          <textarea aria-label="测评条件" value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="继续追问，或调整测评条件…" />
          <button className="send-button" onClick={run} disabled={running} aria-label="运行测评">{running ? <RefreshCw className="spin" /> : <Send />}</button>
          <button className="button button--danger" onClick={onCancel} disabled={!running}><CircleStop size={17} />停止运行</button>
        </div>
      </main>
      <aside className="evidence-pane">
        <h2>实时证据</h2>
        <MetricRail experiment={experiment} />
        <StabilityChart experiment={experiment} />
        <FailureEvidencePanel experiment={experiment} selectedRun={selectedRun} onOpenTrace={() => setDrawerOpen(true)} />
      </aside>
      <TraceDrawer run={selectedRun} open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </div>
  )
}

