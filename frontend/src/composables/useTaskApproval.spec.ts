import { flushPromises } from '@vue/test-utils'
import { nextTick, ref, type Ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getApproval: vi.fn(),
    getTask: vi.fn(),
    resolveApproval: vi.fn(),
  }
})

import {
  ApiProblemError,
  getApproval,
  getTask,
  resolveApproval,
} from '../api'
import type {
  Approval,
  ApprovalResolutionResponse,
  Task,
  TaskEvent,
  TaskStatus,
} from '../types'
import { useTaskApproval } from './useTaskApproval'

const getApprovalMock = vi.mocked(getApproval)
const getTaskMock = vi.mocked(getTask)
const resolveApprovalMock = vi.mocked(resolveApproval)

function makeTask(taskId: string, status: TaskStatus, lastEventSeq = 1): Task {
  return {
    task_id: taskId,
    conversation_id: null,
    goal: `goal-${taskId}`,
    status,
    mode: 'workflow',
    privacy_mode: 'local_only',
    constraints: [],
    last_event_seq: lastEventSeq,
    event_stream: `/api/v1/ws/tasks/${taskId}`,
    created_at: '2026-08-09T00:00:00Z',
    updated_at: '2026-08-09T00:00:00Z',
  }
}

function makeApproval(overrides: Partial<Approval> = {}): Approval {
  return {
    approval_id: 'approval-1',
    decision_id: 'decision-1',
    task_id: 'task-a',
    call_id: 'call-1',
    status: 'pending',
    decision: null,
    preview_hash: 'preview-v1',
    title: '请确认操作',
    purpose: '完成任务',
    tool_name: 'computer.disk_usage',
    tool_version: '1.0.0',
    risk_level: 'R1',
    capabilities: ['filesystem.metadata.read'],
    resource_scope: [{
      kind: 'filesystem_path',
      label: 'D:\\workspace',
      operations: ['read_metadata'],
      version: null,
    }],
    consequences: ['读取元数据'],
    reversible: true,
    data_egress: { enabled: false, destination: null },
    policy_rule_id: 'rule-1',
    policy_revision: 'deskpilot-policy-v1',
    reason_code: 'ASK_FOR_TEST',
    requested_at: '2026-08-09T00:00:00Z',
    expires_at: '2026-08-09T00:05:00Z',
    resolved_at: null,
    consumed_at: null,
    resolution_reason: null,
    updated_at: '2026-08-09T00:00:00Z',
    ...overrides,
  }
}

function makeEvent(
  seq: number,
  type: string,
  approvalId = 'approval-1',
  taskId = 'task-a',
): TaskEvent {
  return {
    event_id: `event-${seq}`,
    task_id: taskId,
    seq,
    type,
    timestamp: '2026-08-09T00:00:00Z',
    trace_id: 'trace-1',
    payload: { approval_id: approvalId },
  }
}

function resolution(
  approval: Approval,
  task: Task,
  replayed = false,
): ApprovalResolutionResponse {
  return { approval, task, replayed }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

async function publishRequired(
  events: Ref<TaskEvent[]>,
): Promise<void> {
  events.value = [makeEvent(2, 'approval.required')]
  await nextTick()
  await flushPromises()
}

describe('useTaskApproval', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('approval.required 后读取完整预览，重放和无关事件不重复 GET', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'running'))
    const events = ref<TaskEvent[]>([])
    getApprovalMock.mockResolvedValue(makeApproval())
    const control = useTaskApproval(task, events)

    await publishRequired(events)

    expect(getApprovalMock).toHaveBeenCalledWith('approval-1')
    expect(control.approval.value).toStrictEqual(makeApproval())
    expect(control.loading.value).toBe(false)

    events.value = [
      makeEvent(2, 'approval.required'),
      makeEvent(3, 'step.progress'),
    ]
    await nextTick()
    await flushPromises()
    expect(getApprovalMock).toHaveBeenCalledTimes(1)
  })

  it('同意前不乐观更新，响应后同时采用审批和任务快照', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'waiting_approval', 3))
    const events = ref<TaskEvent[]>([])
    getApprovalMock.mockResolvedValueOnce(makeApproval())
    const pending = deferred<ApprovalResolutionResponse>()
    resolveApprovalMock.mockReturnValueOnce(pending.promise)
    const control = useTaskApproval(task, events)
    await publishRequired(events)

    const request = control.runApproval('approve')
    expect(control.activeAction.value).toBe('approve')
    expect(control.approval.value?.status).toBe('pending')
    expect(task.value?.status).toBe('waiting_approval')

    const approved = makeApproval({
      status: 'approved',
      decision: 'approved',
      resolved_at: '2026-08-09T00:01:00Z',
    })
    const running = makeTask('task-a', 'running', 5)
    pending.resolve(resolution(approved, running))
    await request

    expect(resolveApprovalMock).toHaveBeenCalledWith('approval-1', 'approve', {
      preview_hash: 'preview-v1',
      scope: 'once',
      reason: undefined,
    })
    expect(control.approval.value).toStrictEqual(approved)
    expect(task.value).toStrictEqual(running)
    expect(control.approvalMessage.value).toContain('仅为本次')
    expect(control.activeAction.value).toBeNull()
  })

  it('请求期间阻止第二个审批决定并规范化拒绝原因', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'waiting_approval'))
    const events = ref<TaskEvent[]>([])
    getApprovalMock.mockResolvedValueOnce(makeApproval())
    const pending = deferred<ApprovalResolutionResponse>()
    resolveApprovalMock.mockReturnValueOnce(pending.promise)
    const control = useTaskApproval(task, events)
    await publishRequired(events)

    const first = control.runApproval('reject', '  目标不正确  ')
    await control.runApproval('approve')
    expect(resolveApprovalMock).toHaveBeenCalledTimes(1)
    expect(resolveApprovalMock).toHaveBeenCalledWith('approval-1', 'reject', {
      preview_hash: 'preview-v1',
      scope: 'once',
      reason: '目标不正确',
    })

    pending.resolve(resolution(
      makeApproval({ status: 'rejected', resolved_at: '2026-08-09T00:01:00Z' }),
      makeTask('task-a', 'cancelled', 6),
    ))
    await first
  })

  it('网络响应丢失后只 GET 对账，不重放 POST', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'waiting_approval'))
    const events = ref<TaskEvent[]>([])
    getApprovalMock
      .mockResolvedValueOnce(makeApproval())
      .mockResolvedValueOnce(makeApproval({
        status: 'approved',
        decision: 'approved',
        resolved_at: '2026-08-09T00:01:00Z',
      }))
    resolveApprovalMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    getTaskMock.mockResolvedValueOnce(makeTask('task-a', 'running', 6))
    const control = useTaskApproval(task, events)
    await publishRequired(events)

    await control.runApproval('approve')

    expect(resolveApprovalMock).toHaveBeenCalledTimes(1)
    expect(getApprovalMock).toHaveBeenCalledTimes(2)
    expect(getTaskMock).toHaveBeenCalledWith('task-a')
    expect(control.approval.value?.status).toBe('approved')
    expect(task.value?.status).toBe('running')
    expect(control.approvalMessage.value).toContain('已通过审批快照确认')
  })

  it('409 stale 后载入新 hash 但不自动同意', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'waiting_approval'))
    const events = ref<TaskEvent[]>([])
    getApprovalMock
      .mockResolvedValueOnce(makeApproval())
      .mockResolvedValueOnce(makeApproval({ preview_hash: 'preview-v2' }))
    resolveApprovalMock.mockRejectedValueOnce(new ApiProblemError(
      '审批预览已过期',
      409,
      'APPROVAL_STALE',
      null,
    ))
    getTaskMock.mockResolvedValueOnce(makeTask('task-a', 'waiting_approval', 4))
    const control = useTaskApproval(task, events)
    await publishRequired(events)

    await control.runApproval('approve')

    expect(resolveApprovalMock).toHaveBeenCalledTimes(1)
    expect(control.approval.value?.preview_hash).toBe('preview-v2')
    expect(control.approval.value?.status).toBe('pending')
    expect(control.approvalError.value).toContain('审批内容已变化')
  })

  it('并发的反向决定获胜时以服务端终态为准', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'waiting_approval'))
    const events = ref<TaskEvent[]>([])
    getApprovalMock
      .mockResolvedValueOnce(makeApproval())
      .mockResolvedValueOnce(makeApproval({
        status: 'rejected',
        decision: 'rejected',
        resolved_at: '2026-08-09T00:01:00Z',
      }))
    resolveApprovalMock.mockRejectedValueOnce(new ApiProblemError(
      '审批已处理',
      409,
      'APPROVAL_ALREADY_RESOLVED',
      null,
    ))
    getTaskMock.mockResolvedValueOnce(makeTask('task-a', 'cancelled', 7))
    const control = useTaskApproval(task, events)
    await publishRequired(events)

    await control.runApproval('approve')

    expect(control.approval.value?.status).toBe('rejected')
    expect(task.value?.status).toBe('cancelled')
    expect(control.approvalError.value).toContain('服务端审批状态为已拒绝')
  })

  it('旧任务的迟到响应不污染新任务', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'waiting_approval'))
    const events = ref<TaskEvent[]>([])
    getApprovalMock.mockResolvedValueOnce(makeApproval())
    const pending = deferred<ApprovalResolutionResponse>()
    resolveApprovalMock.mockReturnValueOnce(pending.promise)
    const control = useTaskApproval(task, events)
    await publishRequired(events)

    const request = control.runApproval('approve')
    const nextTask = makeTask('task-b', 'running')
    task.value = nextTask
    events.value = []
    await nextTick()
    pending.resolve(resolution(
      makeApproval({ status: 'approved' }),
      makeTask('task-a', 'running'),
    ))
    await request

    expect(task.value).toStrictEqual(nextTask)
    expect(control.approval.value).toBeNull()
    expect(control.approvalMessage.value).toBeNull()
    expect(control.approvalError.value).toBeNull()
  })

  it('运行时恢复竞态在对账确认批准后仅重试同一决定一次', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'waiting_approval'))
    const events = ref<TaskEvent[]>([])
    const approved = makeApproval({
      status: 'approved',
      decision: 'approved',
      resolved_at: '2026-08-09T00:01:00Z',
    })
    getApprovalMock
      .mockResolvedValueOnce(makeApproval())
      .mockResolvedValueOnce(approved)
    getTaskMock.mockResolvedValueOnce(makeTask('task-a', 'running', 5))
    resolveApprovalMock
      .mockRejectedValueOnce(new ApiProblemError(
        '审批已记录，但运行时暂不可用',
        409,
        'APPROVAL_RUNTIME_UNAVAILABLE',
        null,
      ))
      .mockResolvedValueOnce(resolution(approved, makeTask('task-a', 'running', 5), true))
    const control = useTaskApproval(task, events)
    await publishRequired(events)

    await control.runApproval('approve')

    expect(resolveApprovalMock).toHaveBeenCalledTimes(2)
    expect(resolveApprovalMock.mock.calls[1]).toStrictEqual([
      'approval-1',
      'approve',
      { preview_hash: 'preview-v1', scope: 'once', reason: undefined },
    ])
    expect(control.approvalMessage.value).toContain('安全重试运行时恢复')
    expect(control.approvalError.value).toBeNull()
  })

  it('approval.invalidated 事件会刷新已同意但未执行的授权生命周期', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'waiting_approval'))
    const events = ref<TaskEvent[]>([])
    getApprovalMock
      .mockResolvedValueOnce(makeApproval())
      .mockResolvedValueOnce(makeApproval({
        status: 'cancelled',
        decision: 'approved',
        resolved_at: '2026-08-09T00:01:00Z',
      }))
    const control = useTaskApproval(task, events)
    await publishRequired(events)

    events.value = [
      makeEvent(2, 'approval.required'),
      makeEvent(3, 'approval.invalidated'),
    ]
    await nextTick()
    await flushPromises()

    expect(getApprovalMock).toHaveBeenCalledTimes(2)
    expect(control.approval.value?.status).toBe('cancelled')
    expect(control.approval.value?.decision).toBe('approved')
  })
})
