import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from './api'
import { AgentWorkspace } from './components/AgentWorkspace'
import { Header } from './components/Header'
import { Sidebar, type PageKey } from './components/Sidebar'
import { SupportingPage } from './components/SupportingPages'
import type { Experiment } from './types'

export function App() {
  const [page, setPage] = useState<PageKey>('agent')
  const [experiment, setExperiment] = useState<Experiment | null>(null)
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState({ completed: 60, total: 60 })
  const closeStream = useRef<(() => void) | null>(null)
  useEffect(() => () => closeStream.current?.(), [])
  const bootstrap = useQuery({ queryKey: ['bootstrap'], queryFn: api.bootstrap })
  const current = experiment ?? bootstrap.data?.experiment ?? null
  const create = useMutation({
    mutationFn: api.createExperiment,
    onSuccess: (result) => {
      setExperiment(result); setProgress({ completed: 0, total: result.total_runs }); setRunning(true)
      closeStream.current?.()
      closeStream.current = api.streamExperiment(result.id, {
        onProgress: (completed, total) => setProgress({ completed, total }),
        onResult: (completedExperiment) => { setExperiment(completedExperiment); setRunning(false) },
        onCancelled: () => setRunning(false),
        onError: () => setRunning(false),
      })
    },
  })
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelExperiment(id),
    onSuccess: (result) => { setExperiment(result); setRunning(false) },
  })

  if (bootstrap.isLoading || !current) return <div className="loading-screen"><span className="loading-logo">A</span><p>正在装载可复现环境…</p></div>
  if (bootstrap.isError) return <div className="error-screen"><h1>无法连接 AgentLens API</h1><p>请确认后端服务运行在 8000 端口。</p></div>

  const run = (prompt: string) => create.mutate({ prompt, candidate_id: current.candidate.id, baseline_candidate_id: current.baseline.id, benchmark_id: 'customer-tools-v2', repetitions: 10 })
  return (
    <div className="app-shell">
      <Sidebar page={page} onPage={setPage} />
      <div className="app-main">
        {page === 'agent' ? <><Header candidate={current.candidate} onNew={() => run(current.prompt)} /><AgentWorkspace experiment={current} running={running} progress={progress} onRun={run} onCancel={() => cancel.mutate(current.id)} /></> : <SupportingPage page={page} experiment={current} />}
      </div>
    </div>
  )
}
