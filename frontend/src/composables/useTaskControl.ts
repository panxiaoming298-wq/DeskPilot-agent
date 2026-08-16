import { ref, type Ref } from 'vue'

import { ApiProblemError, controlTask, getTask } from '../api'
import type { Task, TaskControlAction, TaskStatus } from '../types'

const terminalStatuses = new Set<TaskStatus>(['succeeded', 'failed', 'cancelled'])

const successStatus: Record<TaskControlAction, TaskStatus> = {
  pause: 'paused',
  resume: 'running',
  cancel: 'cancelled',
}

const actionLabels: Record<TaskControlAction, string> = {
  pause: '暂停',
  resume: '恢复',
  cancel: '取消',
}

const successMessages: Record<TaskControlAction, string> = {
  pause: '任务已暂停。',
  resume: '任务已恢复运行。',
  cancel: '任务已取消。',
}

const statusLabels: Record<TaskStatus, string> = {
  created: '已创建',
  classifying: '正在分类',
  running: '运行中',
  waiting_approval: '等待审批',
  waiting_reconciliation: '等待对账恢复',
  succeeded: '已完成',
  failed: '已失败',
  cancelled: '已取消',
  paused: '已暂停',
}

function reconciledAsSuccess(action: TaskControlAction, status: TaskStatus): boolean {
  if (action === 'resume') {
    return status === 'running' || terminalStatuses.has(status)
  }
  return status === successStatus[action]
}

function originalErrorMessage(error: unknown): string {
  if (error instanceof ApiProblemError) return error.message
  if (error instanceof Error && error.message) return error.message
  return '请求未完成'
}

export function useTaskControl(task: Ref<Task | null>) {
  const activeAction = ref<TaskControlAction | null>(null)
  const controlMessage = ref<string | null>(null)
  const controlError = ref<string | null>(null)
  let requestGeneration = 0

  function isCurrentRequest(generation: number, taskId: string): boolean {
    return requestGeneration === generation && task.value?.task_id === taskId
  }

  async function runControl(action: TaskControlAction, reason?: string): Promise<void> {
    if (activeAction.value || !task.value) return

    const taskId = task.value.task_id
    const generation = ++requestGeneration
    const normalizedReason = reason?.trim()

    activeAction.value = action
    controlMessage.value = null
    controlError.value = null

    try {
      const snapshot = await controlTask(
        taskId,
        action,
        normalizedReason ? { reason: normalizedReason } : undefined,
      )
      if (!isCurrentRequest(generation, taskId)) return

      task.value = snapshot
      controlMessage.value = successMessages[action]
    } catch (error) {
      const shouldReconcile = !(error instanceof ApiProblemError) || error.status === 409
      let snapshot: Task | null = null

      if (shouldReconcile) {
        try {
          snapshot = await getTask(taskId)
        } catch {
          // The command error remains the useful root cause when reconciliation also fails.
        }
      }

      if (!isCurrentRequest(generation, taskId)) return
      if (snapshot) task.value = snapshot

      if (error instanceof ApiProblemError && error.code === 'TASK_RUNTIME_UNAVAILABLE') {
        controlError.value =
          snapshot?.status === 'paused'
            ? '任务运行上下文已失效，当前仍为暂停状态；请取消后重新创建任务。'
            : '任务运行上下文已失效；请刷新状态，必要时取消后重新创建任务。'
        return
      }

      if (snapshot && reconciledAsSuccess(action, snapshot.status)) {
        controlMessage.value = `${actionLabels[action]}响应中断，已通过任务快照确认。`
        return
      }

      if (snapshot) {
        controlError.value = `${actionLabels[action]}结果尚未确认，当前状态为${statusLabels[snapshot.status]}。`
        return
      }

      controlError.value = `${actionLabels[action]}失败：${originalErrorMessage(error)}`
    } finally {
      if (requestGeneration === generation) activeAction.value = null
    }
  }

  function reset(): void {
    requestGeneration += 1
    activeAction.value = null
    controlMessage.value = null
    controlError.value = null
  }

  return {
    activeAction,
    controlMessage,
    controlError,
    runControl,
    reset,
  }
}
