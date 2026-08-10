import { ref, watch, type Ref } from 'vue'

import {
  ApiProblemError,
  createIdempotencyKey,
  createReconciliationCompensation,
  listReconciliations,
  refreshReconciliationEvidence,
} from '../api'
import type { Reconciliation, Task, TaskEvent } from '../types'

interface UnknownSignal {
  callId: string
  key: string
}

function latestUnknownSignal(events: TaskEvent[], taskId: string): UnknownSignal | null {
  let latest: TaskEvent | null = null
  for (const event of events) {
    if (
      event.task_id === taskId &&
      event.type === 'tool.unknown' &&
      typeof event.payload.call_id === 'string' &&
      (latest === null || event.seq >= latest.seq)
    ) {
      latest = event
    }
  }
  if (!latest) return null
  return {
    callId: latest.payload.call_id as string,
    key: `${latest.seq}:${latest.payload.call_id as string}`,
  }
}

function originalErrorMessage(error: unknown): string {
  if (error instanceof ApiProblemError) return error.message
  if (error instanceof Error && error.message) return error.message
  return '请求未完成'
}

export function useTaskReconciliation(
  task: Ref<Task | null>,
  events: Ref<TaskEvent[]>,
) {
  const reconciliation = ref<Reconciliation | null>(null)
  const loading = ref(false)
  const refreshing = ref(false)
  const compensating = ref(false)
  const message = ref<string | null>(null)
  const error = ref<string | null>(null)

  let trackedTaskId = task.value?.task_id ?? null
  let lastSignalKey: string | null = null
  let generation = 0
  let compensationKey: string | null = null

  function clearState(): void {
    generation += 1
    lastSignalKey = null
    reconciliation.value = null
    loading.value = false
    refreshing.value = false
    compensating.value = false
    compensationKey = null
    message.value = null
    error.value = null
  }

  async function collectForSignal(signal: UnknownSignal): Promise<void> {
    const taskId = task.value?.task_id
    if (!taskId) return
    const currentGeneration = ++generation
    loading.value = true
    refreshing.value = false
    message.value = null
    error.value = null

    try {
      const records = await listReconciliations(taskId)
      if (currentGeneration !== generation || task.value?.task_id !== taskId) return
      const record = records.find((item) => item.call_id === signal.callId)
      if (!record) {
        error.value = '任务已进入 unknown，但尚未找到对应的持久化对账记录。'
        return
      }
      reconciliation.value = record
      loading.value = false
      refreshing.value = true

      const result = await refreshReconciliationEvidence(record.reconciliation_id)
      if (currentGeneration !== generation || task.value?.task_id !== taskId) return
      if (
        result.reconciliation.task_id !== taskId ||
        result.reconciliation.call_id !== signal.callId ||
        result.reconciliation.reconciliation_id !== record.reconciliation_id
      ) {
        error.value = 'Runner 证据响应与当前 unknown 调用不匹配，已忽略该响应。'
        return
      }
      reconciliation.value = result.reconciliation
      message.value = result.replayed
        ? 'Runner 证据快照未变化，已复用之前的内容寻址记录。'
        : '已保存一条新的 Runner 查询证据。'
    } catch (caught) {
      if (currentGeneration !== generation || task.value?.task_id !== taskId) return
      error.value = `无法采集 Runner 对账证据：${originalErrorMessage(caught)}`
    } finally {
      if (currentGeneration === generation) {
        loading.value = false
        refreshing.value = false
      }
    }
  }

  function synchronizeEvents(): void {
    const taskId = task.value?.task_id
    if (!taskId) return
    const signal = latestUnknownSignal(events.value, taskId)
    if (!signal || signal.key === lastSignalKey) return
    lastSignalKey = signal.key
    void collectForSignal(signal)
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

  async function refreshEvidence(): Promise<void> {
    const taskId = task.value?.task_id
    const current = reconciliation.value
    if (!taskId || !current || current.task_id !== taskId || refreshing.value) return
    const currentGeneration = ++generation
    refreshing.value = true
    message.value = null
    error.value = null
    try {
      const result = await refreshReconciliationEvidence(current.reconciliation_id)
      if (currentGeneration !== generation || task.value?.task_id !== taskId) return
      if (
        result.reconciliation.task_id !== taskId ||
        result.reconciliation.reconciliation_id !== current.reconciliation_id
      ) {
        error.value = 'Runner 证据响应与当前对账记录不匹配，已忽略该响应。'
        return
      }
      reconciliation.value = result.reconciliation
      message.value = result.replayed
        ? '当前 Runner 证据未变化。'
        : '已保存一条新的 Runner 查询证据。'
    } catch (caught) {
      if (currentGeneration !== generation || task.value?.task_id !== taskId) return
      error.value = `刷新 Runner 对账证据失败：${originalErrorMessage(caught)}`
    } finally {
      if (currentGeneration === generation) refreshing.value = false
    }
  }

  async function createCompensation(): Promise<Task | null> {
    const taskId = task.value?.task_id
    const current = reconciliation.value
    if (
      !taskId ||
      !current ||
      current.task_id !== taskId ||
      !current.can_create_compensation ||
      compensating.value
    ) {
      return null
    }
    const currentGeneration = generation
    compensationKey ??= createIdempotencyKey()
    compensating.value = true
    message.value = null
    error.value = null
    try {
      const result = await createReconciliationCompensation(
        current.reconciliation_id,
        compensationKey,
      )
      if (currentGeneration !== generation || task.value?.task_id !== taskId) return null
      if (
        result.reconciliation.reconciliation_id !== current.reconciliation_id ||
        result.reconciliation.task_id !== taskId ||
        result.task.task_id !== result.reconciliation.compensation_task_id
      ) {
        error.value = '反向任务响应与当前对账记录不匹配，已忽略该响应。'
        return null
      }
      reconciliation.value = result.reconciliation
      compensationKey = null
      message.value = result.replayed
        ? '已恢复先前创建的反向任务。'
        : '已创建反向任务；请在新审批卡核对精确路径与版本。'
      return result.task
    } catch (caught) {
      if (currentGeneration !== generation || task.value?.task_id !== taskId) return null
      error.value = `创建反向任务失败：${originalErrorMessage(caught)}`
      return null
    } finally {
      if (currentGeneration === generation) compensating.value = false
    }
  }

  function reset(): void {
    trackedTaskId = null
    clearState()
  }

  return {
    reconciliation,
    loading,
    refreshing,
    compensating,
    message,
    error,
    refreshEvidence,
    createCompensation,
    reset,
  }
}
