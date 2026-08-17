import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getEvaluationReport, listEvaluationRuns, replayEvaluation, runGoldenEvaluation } from '../api'
import type { EvaluationReport, EvaluationRun } from '../types'
import EvaluationLab from './EvaluationLab.vue'

vi.mock('../api', () => ({
  downloadEvaluationReport: vi.fn(), getEvaluationReport: vi.fn(), listEvaluationRuns: vi.fn(),
  replayEvaluation: vi.fn(), runGoldenEvaluation: vi.fn(),
}))

const run: EvaluationRun = {
  run_id: 'evr_original', suite_id: 'deskpilot.resilience-safety', suite_version: 2,
  suite_digest: 'a'.repeat(64), status: 'passed', replay_of_run_id: null, replay_match: null,
  case_count: 20, passed_count: 20, failed_count: 0, safety_case_count: 11,
  safety_passed_count: 11, success_rate: 1, safety_rate: 1, duration_ms: 42,
  result_manifest: {}, manifest_digest: 'b'.repeat(64), started_at: '2026-08-16T00:00:00Z',
  completed_at: '2026-08-16T00:00:01Z', traces: [{ sequence: 1, case_id: 'mcp.text',
    scenario: 'mcp.text_metrics', status: 'passed', input_digest: 'c'.repeat(64),
    output_digest: 'd'.repeat(64), error_code: null, duration_ms: 10,
    previous_event_digest: null, event_digest: 'e'.repeat(64) }],
}

const report: EvaluationReport = {
  schema_version: 'deskpilot.evaluation-report.v1', suite_id: run.suite_id,
  suite_version: 2, suite_digest: run.suite_digest, as_of: run.completed_at,
  run_count: 1, passed_run_count: 1, failed_run_count: 0, run_success_rate: 1,
  run_duration_p50_ms: 42, run_duration_p95_ms: 42,
  case_duration_p50_ms: 10, case_duration_p95_ms: 10, failure_counts: {},
  trend: [{ run_id: run.run_id, status: 'passed', success_rate: 1, safety_rate: 1,
    duration_ms: 42, replay_of_run_id: null, started_at: run.started_at }],
  report_digest: 'f'.repeat(64),
}

describe('EvaluationLab', () => {
  beforeEach(() => {
    vi.mocked(listEvaluationRuns).mockResolvedValue({ runs: [] })
    vi.mocked(getEvaluationReport).mockResolvedValue(report)
    vi.mocked(runGoldenEvaluation).mockResolvedValue(run)
    vi.mocked(replayEvaluation).mockResolvedValue({ ...run, run_id: 'evr_replay', replay_of_run_id: run.run_id, replay_match: true })
  })

  it('runs the fixed suite and displays metrics and trace proof', async () => {
    const wrapper = mount(EvaluationLab)
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(runGoldenEvaluation).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('成功率100%')
    expect(wrapper.text()).toContain('安全通过率100%')
    expect(wrapper.text()).toContain('mcp.text')
    expect(wrapper.text()).toContain('dddddddddddd')
    expect(wrapper.text()).toContain('运行 p50 / p9542 / 42')
    expect(wrapper.text()).toContain('ffffffffffffffff')
  })
})
