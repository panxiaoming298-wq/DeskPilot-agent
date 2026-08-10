import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    controlTask: vi.fn(),
    getTask: vi.fn(),
  }
})

import { ApiProblemError, controlTask, getTask } from '../api'
import type { Task, TaskStatus } from '../types'
import { useTaskControl } from './useTaskControl'

const controlTaskMock = vi.mocked(controlTask)
const getTaskMock = vi.mocked(getTask)

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

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('useTaskControl', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('成功后使用服务端完整快照更新任务', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'running'))
    const paused = makeTask('task-a', 'paused', 8)
    controlTaskMock.mockResolvedValueOnce(paused)
    const control = useTaskControl(task)

    await control.runControl('pause', '  用户暂时离开  ')

    expect(controlTaskMock).toHaveBeenCalledWith('task-a', 'pause', {
      reason: '用户暂时离开',
    })
    expect(task.value).toStrictEqual(paused)
    expect(control.controlMessage.value).toBe('任务已暂停。')
    expect(control.controlError.value).toBeNull()
    expect(control.activeAction.value).toBeNull()
    expect(getTaskMock).not.toHaveBeenCalled()
  })

  it('命令未完成时阻止重复控制请求', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'running'))
    const pending = deferred<Task>()
    controlTaskMock.mockReturnValueOnce(pending.promise)
    const control = useTaskControl(task)

    const first = control.runControl('pause')
    await control.runControl('cancel')

    expect(controlTaskMock).toHaveBeenCalledTimes(1)
    expect(control.activeAction.value).toBe('pause')

    pending.resolve(makeTask('task-a', 'paused', 2))
    await first
    expect(control.activeAction.value).toBeNull()
  })

  it('旧任务的迟到响应不会污染新任务', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'running'))
    const pending = deferred<Task>()
    controlTaskMock.mockReturnValueOnce(pending.promise)
    const control = useTaskControl(task)

    const request = control.runControl('pause')
    const newTask = makeTask('task-b', 'created')
    task.value = newTask
    pending.resolve(makeTask('task-a', 'paused', 2))
    await request

    expect(task.value).toStrictEqual(newTask)
    expect(control.controlMessage.value).toBeNull()
    expect(control.controlError.value).toBeNull()
  })

  it.each<TaskStatus>(['running', 'succeeded', 'failed', 'cancelled'])(
    'resume 响应丢失后可由 %s 快照确认结果',
    async (status) => {
      const task = ref<Task | null>(makeTask('task-a', 'paused'))
      const reconciled = makeTask('task-a', status, 7)
      controlTaskMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
      getTaskMock.mockResolvedValueOnce(reconciled)
      const control = useTaskControl(task)

      await control.runControl('resume')

      expect(controlTaskMock).toHaveBeenCalledTimes(1)
      expect(getTaskMock).toHaveBeenCalledWith('task-a')
      expect(task.value).toStrictEqual(reconciled)
      expect(control.controlMessage.value).toBe('恢复响应中断，已通过任务快照确认。')
      expect(control.controlError.value).toBeNull()
    },
  )

  it('resume 响应丢失且仍为 paused 时只报告未确认，不会重试命令', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'paused'))
    const reconciled = makeTask('task-a', 'paused', 4)
    controlTaskMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    getTaskMock.mockResolvedValueOnce(reconciled)
    const control = useTaskControl(task)

    await control.runControl('resume')

    expect(controlTaskMock).toHaveBeenCalledTimes(1)
    expect(task.value).toStrictEqual(reconciled)
    expect(control.controlMessage.value).toBeNull()
    expect(control.controlError.value).toBe('恢复结果尚未确认，当前状态为已暂停。')
  })

  it('运行上下文不可用时对账并保留 paused 状态和清晰提示', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'paused'))
    const reconciled = makeTask('task-a', 'paused', 5)
    controlTaskMock.mockRejectedValueOnce(
      new ApiProblemError(
        'Task runtime is unavailable.',
        409,
        'TASK_RUNTIME_UNAVAILABLE',
        null,
      ),
    )
    getTaskMock.mockResolvedValueOnce(reconciled)
    const control = useTaskControl(task)

    await control.runControl('resume')

    expect(getTaskMock).toHaveBeenCalledWith('task-a')
    expect(task.value).toStrictEqual(reconciled)
    expect(control.controlMessage.value).toBeNull()
    expect(control.controlError.value).toContain('运行上下文已失效')
    expect(control.controlError.value).toContain('仍为暂停状态')
  })

  it('对账失败时保留原始命令错误', async () => {
    const task = ref<Task | null>(makeTask('task-a', 'running'))
    controlTaskMock.mockRejectedValueOnce(new TypeError('connection lost'))
    getTaskMock.mockRejectedValueOnce(new Error('snapshot unavailable'))
    const control = useTaskControl(task)

    await control.runControl('cancel')

    expect(control.controlError.value).toBe('取消失败：connection lost')
    expect(control.controlError.value).not.toContain('snapshot unavailable')
  })

  it('reset 清空状态并使未完成响应失效', async () => {
    const original = makeTask('task-a', 'running')
    const task = ref<Task | null>(original)
    const pending = deferred<Task>()
    controlTaskMock.mockReturnValueOnce(pending.promise)
    const control = useTaskControl(task)

    const request = control.runControl('pause')
    expect(control.activeAction.value).toBe('pause')
    control.reset()
    expect(control.activeAction.value).toBeNull()
    expect(control.controlMessage.value).toBeNull()
    expect(control.controlError.value).toBeNull()

    pending.resolve(makeTask('task-a', 'paused', 3))
    await request
    expect(task.value).toStrictEqual(original)
    expect(control.activeAction.value).toBeNull()
  })
})
