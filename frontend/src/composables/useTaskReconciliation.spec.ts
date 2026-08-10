import { flushPromises } from '@vue/test-utils'
import { nextTick, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    listReconciliations: vi.fn(),
    refreshReconciliationEvidence: vi.fn(),
    createIdempotencyKey: vi.fn(() => 'deskpilot-ui-compensation-key'),
    createReconciliationCompensation: vi.fn(),
  }
})

import {
  createReconciliationCompensation,
  listReconciliations,
  refreshReconciliationEvidence,
} from '../api'
import type { Reconciliation, Task, TaskEvent } from '../types'
import { useTaskReconciliation } from './useTaskReconciliation'

const listMock = vi.mocked(listReconciliations)
const refreshMock = vi.mocked(refreshReconciliationEvidence)
const compensateMock = vi.mocked(createReconciliationCompensation)

function makeTask(taskId = 'task-1'): Task {
  return {
    task_id: taskId,
    conversation_id: null,
    goal: 'unknown file move',
    status: 'failed',
    mode: 'workflow',
    privacy_mode: 'local_only',
    constraints: [],
    last_event_seq: 2,
    event_stream: `/api/v1/ws/tasks/${taskId}`,
    created_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T00:00:01Z',
  }
}

function makeEvent(taskId = 'task-1'): TaskEvent {
  return {
    event_id: 'event-2',
    task_id: taskId,
    seq: 2,
    type: 'tool.unknown',
    timestamp: '2026-08-10T00:00:01Z',
    trace_id: 'trace-1',
    payload: { call_id: 'call-1', requires_reconciliation: true },
  }
}

function makeReconciliation(overrides: Partial<Reconciliation> = {}): Reconciliation {
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
    call_requested_at: '2026-08-10T00:00:00Z',
    call_started_at: '2026-08-10T00:00:00Z',
    call_finished_at: '2026-08-10T00:00:01Z',
    status: 'pending',
    outcome: null,
    evidence_summary: null,
    resolved_by: null,
    unknown_at: '2026-08-10T00:00:01Z',
    resolved_at: null,
    can_create_attempt: false,
    new_attempt_task_id: null,
    new_attempt_created_at: null,
    can_create_compensation: false,
    compensation_task_id: null,
    compensation_receipt_id: null,
    compensation_created_at: null,
    idempotency_receipt: null,
    receipt_evidence: [],
    updated_at: '2026-08-10T00:00:01Z',
    ...overrides,
  }
}

beforeEach(() => {
  listMock.mockReset()
  refreshMock.mockReset()
  compensateMock.mockReset()
})

describe('useTaskReconciliation', () => {
  it('收到 tool.unknown 后只触发一次自动定位与证据采集', async () => {
    const initial = makeReconciliation()
    const collected = makeReconciliation({
      receipt_evidence: [{
        evidence_id: 'evidence-1',
        kind: 'no_receipt',
        queried_runner_id: 'runner-2',
        commit_receipt: null,
        error_code: null,
        observed_at: '2026-08-10T00:00:02Z',
      }],
    })
    listMock.mockResolvedValue([initial])
    refreshMock.mockResolvedValue({
      reconciliation: collected,
      evidence: collected.receipt_evidence[0],
      replayed: false,
    })
    const task = ref<Task | null>(makeTask())
    const events = ref<TaskEvent[]>([])
    const control = useTaskReconciliation(task, events)

    events.value = [makeEvent()]
    await nextTick()
    await flushPromises()

    expect(listMock).toHaveBeenCalledWith('task-1')
    expect(refreshMock).toHaveBeenCalledWith('reconciliation-1')
    expect(control.reconciliation.value).toEqual(collected)
    expect(control.message.value).toContain('新的 Runner 查询证据')

    events.value = [...events.value]
    await nextTick()
    await flushPromises()
    expect(refreshMock).toHaveBeenCalledTimes(1)
  })

  it('手动刷新内容未变化时保留快照并显示去重结果', async () => {
    const snapshot = makeReconciliation()
    listMock.mockResolvedValue([snapshot])
    refreshMock
      .mockResolvedValueOnce({
        reconciliation: snapshot,
        evidence: {
          evidence_id: 'evidence-1',
          kind: 'no_receipt',
          queried_runner_id: 'runner-2',
          commit_receipt: null,
          error_code: null,
          observed_at: '2026-08-10T00:00:02Z',
        },
        replayed: false,
      })
      .mockResolvedValueOnce({
        reconciliation: snapshot,
        evidence: {
          evidence_id: 'evidence-1',
          kind: 'no_receipt',
          queried_runner_id: 'runner-2',
          commit_receipt: null,
          error_code: null,
          observed_at: '2026-08-10T00:00:02Z',
        },
        replayed: true,
      })
    const task = ref<Task | null>(makeTask())
    const events = ref<TaskEvent[]>([makeEvent()])
    const control = useTaskReconciliation(task, events)
    await flushPromises()

    await control.refreshEvidence()

    expect(refreshMock).toHaveBeenCalledTimes(2)
    expect(control.message.value).toBe('当前 Runner 证据未变化。')
  })

  it('切换任务后忽略旧任务的延迟响应', async () => {
    let resolveList!: (value: Reconciliation[]) => void
    listMock.mockReturnValue(new Promise((resolve) => {
      resolveList = resolve
    }))
    const task = ref<Task | null>(makeTask())
    const events = ref<TaskEvent[]>([makeEvent()])
    const control = useTaskReconciliation(task, events)
    await nextTick()

    task.value = makeTask('task-2')
    events.value = []
    resolveList([makeReconciliation()])
    await flushPromises()

    expect(control.reconciliation.value).toBeNull()
    expect(refreshMock).not.toHaveBeenCalled()
  })

  it('使用稳定幂等键创建回执绑定补偿，失败后手动重试仍复用原键', async () => {
    const receipt = {
      receipt_id: `cmt_${'a'.repeat(64)}`,
      call_id: 'call-1',
      tool_name: 'file.move',
      tool_version: '1.0.0',
      status: 'committed' as const,
      authorization_id: `auth_${'b'.repeat(64)}`,
      approval_id: 'approval-1',
      preview_hash: 'c'.repeat(64),
      prepare_digest: 'd'.repeat(64),
      idempotency_key_digest: 'e'.repeat(64),
      resource_versions_before: { source: 'f'.repeat(64), destination: 'absent' },
      resource_versions_after: { source: 'absent', destination: 'f'.repeat(64) },
      commit_started_at: '2026-08-10T00:00:00Z',
      receipt_recorded_at: '2026-08-10T00:00:01Z',
    }
    const eligible = makeReconciliation({
      can_create_compensation: true,
      receipt_evidence: [{
        evidence_id: 'evidence-1',
        kind: 'commit_receipt',
        queried_runner_id: 'runner-1',
        commit_receipt: receipt,
        error_code: null,
        observed_at: '2026-08-10T00:00:02Z',
      }],
    })
    const completed = makeReconciliation({
      ...eligible,
      can_create_compensation: false,
      compensation_task_id: 'task-compensation',
      compensation_receipt_id: receipt.receipt_id,
      compensation_created_at: '2026-08-10T00:00:03Z',
    })
    const compensationTask = makeTask('task-compensation')
    listMock.mockResolvedValue([eligible])
    refreshMock.mockResolvedValue({
      reconciliation: eligible,
      evidence: eligible.receipt_evidence[0],
      replayed: false,
    })
    compensateMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({
        reconciliation: completed,
        task: compensationTask,
        replayed: true,
      })
    const task = ref<Task | null>(makeTask())
    const events = ref<TaskEvent[]>([makeEvent()])
    const control = useTaskReconciliation(task, events)
    await flushPromises()

    expect(await control.createCompensation()).toBeNull()
    expect(control.error.value).toContain('创建反向任务失败')
    expect(await control.createCompensation()).toEqual(compensationTask)
    expect(compensateMock).toHaveBeenNthCalledWith(
      1,
      'reconciliation-1',
      'deskpilot-ui-compensation-key',
    )
    expect(compensateMock).toHaveBeenNthCalledWith(
      2,
      'reconciliation-1',
      'deskpilot-ui-compensation-key',
    )
    expect(control.reconciliation.value).toEqual(completed)
    expect(control.message.value).toContain('恢复先前创建')
  })
})
