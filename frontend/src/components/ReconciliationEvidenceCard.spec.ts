import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type {
  Reconciliation,
  ReconciliationReceiptEvidence,
  ToolCommitReceipt,
} from '../types'
import ReconciliationEvidenceCard from './ReconciliationEvidenceCard.vue'

const commitReceipt: ToolCommitReceipt = {
  receipt_id: `cmt_${'a'.repeat(64)}`,
  call_id: 'call-1',
  tool_name: 'file.move',
  tool_version: '1.0.0',
  status: 'committed',
  authorization_id: `auth_${'b'.repeat(64)}`,
  approval_id: 'approval-1',
  preview_hash: 'c'.repeat(64),
  prepare_digest: 'd'.repeat(64),
  idempotency_key_digest: 'e'.repeat(64),
  resource_versions_before: { source: 'f'.repeat(64), destination: 'absent' },
  resource_versions_after: { source: 'absent', destination: 'f'.repeat(64) },
  commit_started_at: '2026-08-10T01:00:00Z',
  receipt_recorded_at: '2026-08-10T01:00:01Z',
}

function makeEvidence(
  overrides: Partial<ReconciliationReceiptEvidence> = {},
): ReconciliationReceiptEvidence {
  return {
    evidence_id: 'evidence-1',
    kind: 'commit_receipt',
    queried_runner_id: 'runner-2',
    commit_receipt: commitReceipt,
    error_code: null,
    observed_at: '2026-08-10T01:00:02Z',
    ...overrides,
  }
}

function makeReconciliation(
  evidence: ReconciliationReceiptEvidence[] = [makeEvidence()],
): Reconciliation {
  return {
    reconciliation_id: 'reconciliation-1',
    task_id: 'task-1',
    call_id: 'call-1',
    step_id: 'step-1',
    attempt: 1,
    tool_name: 'file.move',
    tool_version: '1.0.0',
    contract_digest: '1'.repeat(64),
    arguments_digest: '2'.repeat(64),
    idempotency: 'key_required',
    runner_id: 'runner-1',
    call_error_code: 'RUNNER_CALL_OUTCOME_UNKNOWN',
    call_resolution_source: 'control_plane',
    call_requested_at: '2026-08-10T00:59:58Z',
    call_started_at: '2026-08-10T00:59:59Z',
    call_finished_at: '2026-08-10T01:00:00Z',
    status: 'pending',
    outcome: null,
    evidence_summary: null,
    resolved_by: null,
    unknown_at: '2026-08-10T01:00:00Z',
    resolved_at: null,
    graph_recovery_status: 'not_applicable',
    graph_recovery_action: null,
    graph_recovery_event_id: null,
    graph_recovered_at: null,
    can_create_attempt: false,
    new_attempt_task_id: null,
    new_attempt_created_at: null,
    can_create_compensation: true,
    compensation_task_id: null,
    compensation_receipt_id: null,
    compensation_created_at: null,
    idempotency_receipt: null,
    receipt_evidence: evidence,
    updated_at: '2026-08-10T01:00:00Z',
  }
}

function mountCard(reconciliation = makeReconciliation()) {
  return mount(ReconciliationEvidenceCard, {
    props: {
      reconciliation,
      loading: false,
      refreshing: false,
      compensating: false,
      message: null,
      error: null,
    },
  })
}

describe('ReconciliationEvidenceCard', () => {
  it('把 committed receipt 展示为正向证据但保留 unknown 人工裁决边界', async () => {
    const wrapper = mountCard()

    expect(wrapper.text()).toContain('已发现提交回执')
    expect(wrapper.text()).toContain('原始调用保持 unknown')
    expect(wrapper.text()).toContain('confirmed_succeeded')
    expect(wrapper.text()).toContain('源文件')
    expect(wrapper.text()).toContain('目标文件')
    expect(wrapper.text()).toContain('等待人工裁决')

    await wrapper.get('[data-testid="refresh-reconciliation-evidence"]').trigger('click')
    expect(wrapper.emitted('refresh')).toEqual([[]])
  })

  it('需要二次确认才发出创建反向任务事件', async () => {
    const wrapper = mountCard()

    await wrapper.get('[data-testid="create-reconciliation-compensation"]').trigger('click')
    expect(wrapper.emitted('compensate')).toBeUndefined()
    expect(wrapper.text()).toContain('精确路径将在下一张审批卡展示')

    await wrapper.get('[data-testid="confirm-reconciliation-compensation"]').trigger('click')
    expect(wrapper.emitted('compensate')).toEqual([[]])
  })

  it('明确说明 no receipt 不能证明无副作用或允许重试', () => {
    const noReceipt = makeEvidence({
      kind: 'no_receipt',
      commit_receipt: null,
      error_code: null,
    })
    const wrapper = mountCard(makeReconciliation([noReceipt]))

    expect(wrapper.text()).toContain('当前未发现回执')
    expect(wrapper.text()).toContain('没有回执不等于“未生效”')
    expect(wrapper.text()).toContain('不能据此安全重试')
  })

  it('只展示查询失败稳定错误码，并在刷新时禁用按钮', () => {
    const queryFailed = makeEvidence({
      kind: 'query_failed',
      commit_receipt: null,
      error_code: 'RUNNER_RECEIPT_QUERY_UNAVAILABLE',
    })
    const wrapper = mount(ReconciliationEvidenceCard, {
      props: {
        reconciliation: makeReconciliation([queryFailed]),
        loading: false,
        refreshing: true,
        compensating: false,
        message: null,
        error: null,
      },
    })

    expect(wrapper.text()).toContain('查询失败')
    expect(wrapper.text()).toContain('RUNNER_RECEIPT_QUERY_UNAVAILABLE')
    expect(wrapper.get('[data-testid="refresh-reconciliation-evidence"]').attributes('disabled')).toBeDefined()
  })
})
