import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Experiment, FailureEvidence, Run } from '../types'
import { FailureEvidencePanel } from './FailureEvidencePanel'

function fixture(judgeVerdict: FailureEvidence['judge_verdict']) {
  const failure = {
    category: 'tool_misuse',
    label: '工具误用',
    severity: 'high',
    event_range: [2, 3],
    rule: 'tool.allowlist_or_schema',
    explanation: 'deterministic explanation',
    judge_verdict: judgeVerdict,
    confidence: 'high',
    judge_explanation: judgeVerdict === 'not_run' ? null : 'reviewed',
    judge_evidence_sequences: judgeVerdict === 'not_run' ? [] : [2],
  } as FailureEvidence
  const run = {
    run_id: 'run-failed',
    failures: [failure],
    trajectory: [],
  } as unknown as Run
  const experiment = {
    id: 'exp-completed',
    status: 'completed',
    failures: { 工具误用: 1 },
    judge_calibration: {
      total: 24,
      correct: 22,
      passed: true,
      mode: 'fixture',
    },
  } as unknown as Experiment
  return { experiment, run }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('on-demand Judge confirmation', () => {
  it('runs a first review without force or confirmation', () => {
    const { experiment, run } = fixture('not_run')
    const onReview = vi.fn()
    const confirm = vi.spyOn(window, 'confirm')

    render(
      <FailureEvidencePanel
        experiment={experiment}
        selectedRun={run}
        onOpenTrace={() => undefined}
        onReview={onReview}
        reviewing={false}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '语义复核' }))

    expect(confirm).not.toHaveBeenCalled()
    expect(onReview).toHaveBeenCalledWith('run-failed', 0, false)
  })

  it('requires confirmation before a forced repeat review', () => {
    const { experiment, run } = fixture('support')
    const onReview = vi.fn()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)

    render(
      <FailureEvidencePanel
        experiment={experiment}
        selectedRun={run}
        onOpenTrace={() => undefined}
        onReview={onReview}
        reviewing={false}
      />,
    )
    const button = screen.getByRole('button', { name: '重新复核（再次调用）' })
    fireEvent.click(button)
    expect(onReview).not.toHaveBeenCalled()

    confirm.mockReturnValue(true)
    fireEvent.click(button)
    expect(onReview).toHaveBeenCalledWith('run-failed', 0, true)
  })
})
