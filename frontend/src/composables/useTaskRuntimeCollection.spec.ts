import { mount } from '@vue/test-utils'
import { defineComponent, h, nextTick, ref, type Ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Task, TaskEvent } from '../types'
import { TASK_RUNTIME_CAPACITY, useTaskRuntimeCollection } from './useTaskRuntimeCollection'

const mocks = vi.hoisted(() => ({
  eventRuntimes: [] as Array<Record<string, unknown>>,
  useTaskEvents: vi.fn(),
  useTaskControl: vi.fn(),
  useTaskApproval: vi.fn(),
  useTaskReconciliation: vi.fn(),
}))

vi.mock('./useTaskEvents', () => ({ useTaskEvents: mocks.useTaskEvents }))
vi.mock('./useTaskControl', () => ({ useTaskControl: mocks.useTaskControl }))
vi.mock('./useTaskApproval', () => ({ useTaskApproval: mocks.useTaskApproval }))
vi.mock('./useTaskReconciliation', () => ({
  useTaskReconciliation: mocks.useTaskReconciliation,
}))

type Collection = ReturnType<typeof useTaskRuntimeCollection>

function makeTask(taskId: string, status: Task['status'] = 'running'): Task {
  return {
    task_id: taskId,
    conversation_id: null,
    goal: `目标 ${taskId}`,
    status,
    mode: 'execute',
    privacy_mode: 'local_only',
    constraints: ['no_cloud'],
    last_event_seq: 5,
    event_stream: `/api/v1/tasks/${taskId}/events`,
    created_at: '2026-08-25T00:00:00Z',
    updated_at: '2026-08-25T00:00:01Z',
  }
}

function makeEvent(taskId: string, seq: number, type = 'agent.started'): TaskEvent {
  return {
    event_id: `${taskId}-event-${seq}`,
    task_id: taskId,
    seq,
    type,
    timestamp: '2026-08-25T00:00:02Z',
    trace_id: `${taskId}-trace`,
    payload: {},
  }
}

function mountCollection(): { collection: Collection, unmount: () => void } {
  let collection: Collection | null = null
  const wrapper = mount(defineComponent({
    setup() {
      collection = useTaskRuntimeCollection()
      return () => h('div')
    },
  }))
  if (!collection) throw new Error('collection was not initialized')
  return { collection: collection as Collection, unmount: () => wrapper.unmount() }
}

beforeEach(() => {
  mocks.eventRuntimes = []
  mocks.useTaskEvents.mockReset()
  mocks.useTaskControl.mockReset()
  mocks.useTaskApproval.mockReset()
  mocks.useTaskReconciliation.mockReset()

  mocks.useTaskEvents.mockImplementation(() => {
    const runtime = {
      events: ref<TaskEvent[]>([]),
      connected: ref(false),
      streamError: ref<string | null>(null),
      connectionState: ref<'idle' | 'connected'>('idle'),
      recoveryMessage: ref<string | null>(null),
      reconnectAttempt: ref(0),
      retryDelayMs: ref(0),
      connect: vi.fn(),
      reconnectNow: vi.fn(),
      reset: vi.fn(),
    }
    mocks.eventRuntimes.push(runtime)
    return runtime
  })
  mocks.useTaskControl.mockImplementation(() => ({
    activeAction: ref(null),
    controlMessage: ref(null),
    controlError: ref(null),
    runControl: vi.fn(),
    reset: vi.fn(),
  }))
  mocks.useTaskApproval.mockImplementation(() => ({
    approval: ref(null),
    loading: ref(false),
    activeAction: ref(null),
    approvalMessage: ref(null),
    approvalError: ref(null),
    runApproval: vi.fn(),
    reset: vi.fn(),
  }))
  mocks.useTaskReconciliation.mockImplementation(() => ({
    reconciliation: ref(null),
    loading: ref(false),
    refreshing: ref(false),
    compensating: ref(false),
    message: ref(null),
    error: ref(null),
    refreshEvidence: vi.fn(),
    createCompensation: vi.fn(),
    reset: vi.fn(),
  }))
})

describe('useTaskRuntimeCollection', () => {
  it('为三个活动任务保留独立连接，并拒绝第四个活动任务', () => {
    const { collection, unmount } = mountCollection()

    expect(collection.trackTask(makeTask('task-1'))).toBe(true)
    expect(collection.trackTask(makeTask('task-2'))).toBe(true)
    expect(collection.trackTask(makeTask('task-3'))).toBe(true)
    expect(collection.trackTask(makeTask('task-4'))).toBe(false)

    expect(collection.activeTaskCount.value).toBe(TASK_RUNTIME_CAPACITY)
    expect(collection.hasTaskCapacity.value).toBe(false)
    expect(mocks.eventRuntimes.map((runtime) => (
      runtime.connect as ReturnType<typeof vi.fn>
    ).mock.calls[0]?.[0])).toEqual(['task-1', 'task-2', 'task-3'])
    expect(mocks.eventRuntimes.every((runtime) => (
      runtime.reset as ReturnType<typeof vi.fn>
    ).mock.calls.length === 1)).toBe(true)
    unmount()
  })

  it('切换焦点不重置后台连接，并按任务维护 cursor 与未读', async () => {
    const { collection, unmount } = mountCollection()
    collection.trackTask(makeTask('task-1'))
    collection.trackTask(makeTask('task-2'))

    const first = mocks.eventRuntimes[0] as {
      events: Ref<TaskEvent[]>
      connect: ReturnType<typeof vi.fn>
      reset: ReturnType<typeof vi.fn>
    }
    const second = mocks.eventRuntimes[1] as {
      events: Ref<TaskEvent[]>
      connect: ReturnType<typeof vi.fn>
      reset: ReturnType<typeof vi.fn>
    }
    first.events.value = [makeEvent('task-1', 6)]
    await nextTick()

    expect(collection.taskCards.value.find((card) => card.task.task_id === 'task-1')).toMatchObject({
      event_cursor: 6,
      unread_count: 1,
      selected: false,
    })
    expect(collection.slots[0].task.value?.last_event_seq).toBe(6)
    expect(collection.selectTask('task-1')).toBe(true)
    expect(collection.taskCards.value.find((card) => card.task.task_id === 'task-1')).toMatchObject({
      unread_count: 0,
      selected: true,
    })
    expect(first.connect).toHaveBeenCalledOnce()
    expect(second.connect).toHaveBeenCalledOnce()
    expect(first.reset).toHaveBeenCalledOnce()
    expect(second.reset).toHaveBeenCalledOnce()
    unmount()
  })

  it('只回收终态槽位，不中断其它活动任务', async () => {
    const { collection, unmount } = mountCollection()
    collection.trackTask(makeTask('task-1'))
    collection.trackTask(makeTask('task-2'))
    collection.trackTask(makeTask('task-3'))

    const first = mocks.eventRuntimes[0] as {
      events: Ref<TaskEvent[]>
      connect: ReturnType<typeof vi.fn>
      reset: ReturnType<typeof vi.fn>
    }
    first.events.value = [makeEvent('task-1', 6, 'task.completed')]
    await nextTick()

    expect(collection.trackTask(makeTask('task-4'))).toBe(true)
    expect(first.reset).toHaveBeenCalledTimes(2)
    expect(first.connect).toHaveBeenLastCalledWith('task-4')
    for (const runtime of mocks.eventRuntimes.slice(1)) {
      expect((runtime.reset as ReturnType<typeof vi.fn>)).toHaveBeenCalledOnce()
      expect((runtime.connect as ReturnType<typeof vi.fn>)).toHaveBeenCalledOnce()
    }
    unmount()
  })
})
