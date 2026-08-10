import { ref, watch, type Ref } from 'vue'

import {
  ApiProblemError,
  getApproval,
  getTask,
  resolveApproval,
} from '../api'
import type {
  Approval,
  ApprovalAction,
  ApprovalStatus,
  Task,
  TaskEvent,
} from '../types'

interface ApprovalSignal {
  approvalId: string
  key: string
}

const approvalEventTypes = new Set([
  'approval.required',
  'approval.resolved',
  'approval.expired',
  'approval.invalidated',
  'tool.started',
])

const approvalStatusLabels: Record<ApprovalStatus, string> = {
  pending: '等待审批',
  approved: '已同意',
  rejected: '已拒绝',
  expired: '已过期',
  cancelled: '已取消',
}

const actionLabels: Record<ApprovalAction, string> = {
  approve: '同意',
  reject: '拒绝',
}

function latestApprovalSignal(events: TaskEvent[], taskId: string): ApprovalSignal | null {
  let latest: TaskEvent | null = null
  for (const event of events) {
    if (
      event.task_id === taskId &&
      approvalEventTypes.has(event.type) &&
      typeof event.payload.approval_id === 'string' &&
      (latest === null || event.seq >= latest.seq)
    ) {
      latest = event
    }
  }
  if (!latest) return null
  return {
    approvalId: latest.payload.approval_id as string,
    key: `${latest.seq}:${latest.type}:${latest.payload.approval_id as string}`,
  }
}

function originalErrorMessage(error: unknown): string {
  if (error instanceof ApiProblemError) return error.message
  if (error instanceof Error && error.message) return error.message
  return '请求未完成'
}

export function useTaskApproval(
  task: Ref<Task | null>,
  events: Ref<TaskEvent[]>,
) {
  const approval = ref<Approval | null>(null)
  const loading = ref(false)
  const activeAction = ref<ApprovalAction | null>(null)
  const approvalMessage = ref<string | null>(null)
  const approvalError = ref<string | null>(null)

  let trackedTaskId = task.value?.task_id ?? null
  let lastSignalKey: string | null = null
  let loadGeneration = 0
  let actionGeneration = 0

  function clearState(): void {
    loadGeneration += 1
    actionGeneration += 1
    lastSignalKey = null
    approval.value = null
    loading.value = false
    activeAction.value = null
    approvalMessage.value = null
    approvalError.value = null
  }

  function synchronizeEvents(): void {
    const taskId = task.value?.task_id
    if (!taskId) return
    const signal = latestApprovalSignal(events.value, taskId)
    if (!signal || signal.key === lastSignalKey) return
    lastSignalKey = signal.key
    void refreshApproval(signal.approvalId)
  }

  watch(
    () => task.value?.task_id ?? null,
    (taskId) => {
      if (taskId === trackedTaskId) return
      trackedTaskId = taskId
      clearState()
      synchronizeEvents()
    },
  )

  watch(events, synchronizeEvents, { immediate: true })

  async function refreshApproval(approvalId?: string): Promise<void> {
    const taskId = task.value?.task_id
    const targetApprovalId = approvalId ?? approval.value?.approval_id
    if (!taskId || !targetApprovalId) return

    const generation = ++loadGeneration
    if (approval.value?.approval_id !== targetApprovalId) approval.value = null
    loading.value = true
    approvalError.value = null

    try {
      const snapshot = await getApproval(targetApprovalId)
      if (
        generation !== loadGeneration ||
        task.value?.task_id !== taskId ||
        snapshot.task_id !== taskId ||
        snapshot.approval_id !== targetApprovalId
      ) {
        return
      }
      approval.value = snapshot
    } catch (error) {
      if (generation !== loadGeneration || task.value?.task_id !== taskId) return
      approvalError.value = `无法读取审批预览：${originalErrorMessage(error)}`
    } finally {
      if (generation === loadGeneration) loading.value = false
    }
  }

  function isCurrentAction(
    generation: number,
    taskId: string,
    approvalId: string,
  ): boolean {
    return (
      generation === actionGeneration &&
      task.value?.task_id === taskId &&
      approval.value?.approval_id === approvalId
    )
  }

  async function runApproval(action: ApprovalAction, reason?: string): Promise<void> {
    const currentApproval = approval.value
    const taskId = task.value?.task_id
    if (
      !currentApproval ||
      !taskId ||
      currentApproval.task_id !== taskId ||
      currentApproval.status !== 'pending' ||
      activeAction.value !== null ||
      loading.value
    ) {
      return
    }

    loadGeneration += 1
    loading.value = false
    const generation = ++actionGeneration
    const approvalId = currentApproval.approval_id
    const previewHash = currentApproval.preview_hash
    const normalizedReason = reason?.trim()
    activeAction.value = action
    approvalMessage.value = null
    approvalError.value = null

    try {
      const response = await resolveApproval(approvalId, action, {
        preview_hash: previewHash,
        scope: 'once',
        reason: normalizedReason || undefined,
      })
      if (!isCurrentAction(generation, taskId, approvalId)) return
      if (
        response.approval.task_id !== taskId ||
        response.approval.approval_id !== approvalId ||
        response.task.task_id !== taskId
      ) {
        approvalError.value = '审批响应与当前任务不匹配，已忽略该响应。'
        return
      }

      approval.value = response.approval
      task.value = response.task
      const replayed = response.replayed ? '服务端已确认之前的相同决定。' : ''
      approvalMessage.value = action === 'approve'
        ? `已仅为本次操作授权。${replayed}`
        : `已拒绝本次操作。${replayed}`
    } catch (error) {
      const shouldReconcile = !(error instanceof ApiProblemError) || error.status === 409
      if (!shouldReconcile) {
        if (isCurrentAction(generation, taskId, approvalId)) {
          approvalError.value = `${actionLabels[action]}审批失败：${originalErrorMessage(error)}`
        }
        return
      }

      const [approvalResult, taskResult] = await Promise.allSettled([
        getApproval(approvalId),
        getTask(taskId),
      ])
      if (!isCurrentAction(generation, taskId, approvalId)) return

      if (taskResult.status === 'fulfilled' && taskResult.value.task_id === taskId) {
        task.value = taskResult.value
      }

      if (
        approvalResult.status !== 'fulfilled' ||
        approvalResult.value.task_id !== taskId ||
        approvalResult.value.approval_id !== approvalId
      ) {
        approvalError.value = `${actionLabels[action]}审批失败：${originalErrorMessage(error)}`
        return
      }

      const reconciled = approvalResult.value
      approval.value = reconciled
      const expectedStatus: ApprovalStatus = action === 'approve' ? 'approved' : 'rejected'
      if (reconciled.status === expectedStatus) {
        if (
          action === 'approve' &&
          reconciled.consumed_at === null &&
          task.value?.status === 'running' &&
          error instanceof ApiProblemError &&
          error.code === 'APPROVAL_RUNTIME_UNAVAILABLE'
        ) {
          try {
            const replay = await resolveApproval(approvalId, action, {
              preview_hash: previewHash,
              scope: 'once',
              reason: normalizedReason || undefined,
            })
            if (!isCurrentAction(generation, taskId, approvalId)) return
            if (
              replay.approval.task_id !== taskId ||
              replay.approval.approval_id !== approvalId ||
              replay.task.task_id !== taskId
            ) {
              approvalError.value = '审批重试响应与当前任务不匹配，已忽略该响应。'
              return
            }
            approval.value = replay.approval
            task.value = replay.task
            approvalMessage.value = '审批已记录，并已安全重试运行时恢复。'
          } catch (retryError) {
            if (!isCurrentAction(generation, taskId, approvalId)) return
            approvalError.value = `审批已记录，但运行时仍不可恢复：${originalErrorMessage(retryError)}`
          }
          return
        }
        approvalMessage.value = `${actionLabels[action]}响应中断，已通过审批快照确认。`
        return
      }

      if (reconciled.status !== 'pending') {
        approvalError.value = `${actionLabels[action]}未生效，服务端审批状态为${approvalStatusLabels[reconciled.status]}。`
        return
      }

      if (reconciled.preview_hash !== previewHash) {
        approvalError.value = '审批内容已变化，已载入最新预览。请重新核对后再决定。'
        return
      }

      approvalError.value = `${actionLabels[action]}结果尚未确认，服务端仍显示等待审批。请核对后重试。`
    } finally {
      if (generation === actionGeneration) activeAction.value = null
    }
  }

  function reset(): void {
    trackedTaskId = null
    clearState()
  }

  return {
    approval,
    loading,
    activeAction,
    approvalMessage,
    approvalError,
    refreshApproval,
    runApproval,
    reset,
  }
}
