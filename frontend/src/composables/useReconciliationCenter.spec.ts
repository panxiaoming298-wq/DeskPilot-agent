import { flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    createIdempotencyKey: vi.fn(),
    createReconciliationAttempt: vi.fn(),
    createReconciliationCompensation: vi.fn(),
    getTask: vi.fn(),
    listReconciliations: vi.fn(),
    listTasks: vi.fn(),
    refreshReconciliationEvidence: vi.fn(),
    recoverReconciliationGraph: vi.fn(),
    resolveReconciliation: vi.fn(),
  }
})

import {
  createIdempotencyKey,
  createReconciliationAttempt,
  createReconciliationCompensation,
  getTask,
  listReconciliations,
  listTasks,
  refreshReconciliationEvidence,
  recoverReconciliationGraph,
  resolveReconciliation,
} from '../api'
import type { Reconciliation, Task } from '../types'
import { useReconciliationCenter } from './useReconciliationCenter'

const keyMock = vi.mocked(createIdempotencyKey)
const attemptMock = vi.mocked(createReconciliationAttempt)
const compensationMock = vi.mocked(createReconciliationCompensation)
const getTaskMock = vi.mocked(getTask)
const listReconciliationsMock = vi.mocked(listReconciliations)
const listTasksMock = vi.mocked(listTasks)
const refreshMock = vi.mocked(refreshReconciliationEvidence)
const recoverGraphMock = vi.mocked(recoverReconciliationGraph)
const resolveMock = vi.mocked(resolveReconciliation)

function makeTask(taskId = 'task-1', overrides: Partial<Task> = {}): Task {
  return {
    task_id: taskId,
    conversation_id: null,
    goal: `任务 ${taskId}`,
    status: 'failed',
    mode: 'workflow',
    privacy_mode: 'local_only',
    constraints: [],
    last_event_seq: 3,
    event_stream: `/api/v1/ws/tasks/${taskId}`,
    created_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T00:00:01Z',
    ...overrides,
  }
}

function makeReconciliation(
  overrides: Partial<Reconciliation> = {},
): Reconciliation {
  return {
    reconciliation_id: 'reconciliation-1',
    task_id: 'task-1',
    call_id: 'call-1',
    step_id: 'step-1',
    attempt: 1,
    tool_name: 'computer.disk_usage',
    tool_version: '1.0.0',
    contract_digest: '1'.repeat(64),
    arguments_digest: '2'.repeat(64),
    idempotency: 'key_optional',
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
    graph_recovery_status: 'not_applicable',
    graph_recovery_action: null,
    graph_recovery_event_id: null,
    graph_recovered_at: null,
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
  keyMock.mockReset()
  attemptMock.mockReset()
  compensationMock.mockReset()
  getTaskMock.mockReset()
  listReconciliationsMock.mockReset()
  listTasksMock.mockReset()
  refreshMock.mockReset()
  recoverGraphMock.mockReset()
  resolveMock.mockReset()
  keyMock.mockReturnValue('center-key')
  listTasksMock.mockResolvedValue({
    items: [makeTask()],
    total: 1,
    limit: 25,
    offset: 0,
  })
  listReconciliationsMock.mockResolvedValue([makeReconciliation()])
})

describe('useReconciliationCenter', () => {
  it('并行加载有界任务历史和集中对账列表，并选中第一条记录', async () => {
    const center = useReconciliationCenter()
    await flushPromises()

    expect(listTasksMock).toHaveBeenCalledWith(undefined, 25, 0)
    expect(listReconciliationsMock).toHaveBeenCalledWith(undefined, undefined)
    expect(center.tasks.value).toEqual([makeTask()])
    expect(center.taskTotal.value).toBe(1)
    expect(center.selected.value?.reconciliation_id).toBe('reconciliation-1')
  })

  it('裁决网络失败后以相同请求手动重试时复用幂等键', async () => {
    const resolved = makeReconciliation({
      status: 'resolved',
      outcome: 'confirmed_no_effect',
      evidence_summary: '已核对外部状态',
      resolved_by: 'local-user',
      resolved_at: '2026-08-10T00:00:02Z',
    })
    keyMock.mockReturnValue('resolve-key-stable')
    resolveMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({ reconciliation: resolved, replayed: true })
    const center = useReconciliationCenter()
    await flushPromises()

    expect(await center.resolveSelected(
      'confirmed_no_effect',
      '  已核对外部状态  ',
    )).toBe(false)
    expect(await center.resolveSelected(
      'confirmed_no_effect',
      '已核对外部状态',
    )).toBe(true)

    expect(resolveMock).toHaveBeenNthCalledWith(
      1,
      'reconciliation-1',
      'confirmed_no_effect',
      '已核对外部状态',
      'resolve-key-stable',
    )
    expect(resolveMock).toHaveBeenNthCalledWith(
      2,
      'reconciliation-1',
      'confirmed_no_effect',
      '已核对外部状态',
      'resolve-key-stable',
    )
    expect(keyMock).toHaveBeenCalledTimes(1)
    expect(center.selected.value).toEqual(resolved)
    expect(center.message.value).toContain('恢复先前提交')
  })

  it('失败后修改裁决正文会分配新的幂等键', async () => {
    keyMock
      .mockReturnValueOnce('resolve-key-a')
      .mockReturnValueOnce('resolve-key-b')
    resolveMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const center = useReconciliationCenter()
    await flushPromises()

    await center.resolveSelected('accepted_unknown', '第一次核对')
    await center.resolveSelected('accepted_unknown', '第二次核对')

    expect(resolveMock.mock.calls.map((call) => call[3])).toEqual([
      'resolve-key-a',
      'resolve-key-b',
    ])
  })

  it('pending 筛选下裁决成功后移除已 resolved 记录', async () => {
    const resolved = makeReconciliation({
      status: 'resolved',
      outcome: 'accepted_unknown',
      evidence_summary: '无法继续查明',
      resolved_by: 'local-user',
      resolved_at: '2026-08-10T00:00:02Z',
    })
    resolveMock.mockResolvedValue({ reconciliation: resolved, replayed: false })
    const center = useReconciliationCenter()
    await flushPromises()
    center.reconciliationFilter.value = 'pending'
    await flushPromises()

    expect(await center.resolveSelected(
      'accepted_unknown',
      '无法继续查明',
    )).toBe(true)
    expect(center.reconciliations.value).toEqual([])
    expect(center.selected.value).toBeNull()
  })

  it('新 attempt 失败后复用幂等键，成功后加入当前第一页并更新血缘', async () => {
    const eligible = makeReconciliation({
      status: 'resolved',
      outcome: 'confirmed_no_effect',
      evidence_summary: '没有产生效果',
      resolved_by: 'local-user',
      resolved_at: '2026-08-10T00:00:02Z',
      can_create_attempt: true,
    })
    const successor = makeTask('task-attempt', {
      status: 'created',
      created_at: '2026-08-10T00:00:03Z',
    })
    const completed = makeReconciliation({
      ...eligible,
      can_create_attempt: false,
      new_attempt_task_id: successor.task_id,
      new_attempt_created_at: successor.created_at,
    })
    listReconciliationsMock.mockResolvedValue([eligible])
    keyMock.mockReturnValue('attempt-key-stable')
    attemptMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({
        reconciliation: completed,
        task: successor,
        replayed: true,
      })
    const center = useReconciliationCenter()
    await flushPromises()

    expect(await center.createAttempt()).toBeNull()
    expect(await center.createAttempt()).toEqual(successor)

    expect(attemptMock).toHaveBeenNthCalledWith(
      1,
      'reconciliation-1',
      'attempt-key-stable',
    )
    expect(attemptMock).toHaveBeenNthCalledWith(
      2,
      'reconciliation-1',
      'attempt-key-stable',
    )
    expect(center.tasks.value[0]).toEqual(successor)
    expect(center.taskTotal.value).toBe(2)
    expect(center.selected.value).toEqual(completed)
  })

  it('血缘导航优先使用历史缓存，缺失时才读取任务快照', async () => {
    const remote = makeTask('task-remote')
    getTaskMock.mockResolvedValue(remote)
    const center = useReconciliationCenter()
    await flushPromises()

    expect(await center.taskForNavigation('task-1')).toEqual(makeTask())
    expect(getTaskMock).not.toHaveBeenCalled()
    expect(await center.taskForNavigation('task-remote')).toEqual(remote)
    expect(getTaskMock).toHaveBeenCalledWith('task-remote')
  })

  it('图恢复失败时复用幂等键，成功后更新 reconciliation 和原任务', async () => {
    const pending = makeReconciliation({
      status: 'resolved',
      outcome: 'confirmed_succeeded',
      graph_recovery_status: 'pending',
    })
    const resumedTask = makeTask('task-1', { status: 'running' })
    const applied = makeReconciliation({
      ...pending,
      graph_recovery_status: 'applied',
      graph_recovery_action: 'continue',
    })
    listReconciliationsMock.mockResolvedValue([pending])
    keyMock.mockReturnValue('recover-graph-key')
    recoverGraphMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({
        reconciliation: applied,
        task: resumedTask,
        graph: {
          graph_id: 'graph-1',
          task_id: 'task-1',
          status: 'active',
          fencing_token: 2,
          revision: 8,
        },
        replayed: true,
        resumed: true,
      })
    const center = useReconciliationCenter()
    await flushPromises()

    expect(await center.recoverGraph('continue')).toBeNull()
    expect(await center.recoverGraph('continue')).toEqual(resumedTask)
    expect(recoverGraphMock).toHaveBeenNthCalledWith(
      2,
      'reconciliation-1',
      'continue',
      'recover-graph-key',
    )
    expect(center.tasks.value[0]).toEqual(resumedTask)
    expect(center.selected.value).toEqual(applied)
  })
})
