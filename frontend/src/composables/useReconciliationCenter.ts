import { computed, ref, watch } from 'vue'

import {
  ApiProblemError,
  createIdempotencyKey,
  createReconciliationAttempt,
  createReconciliationCompensation,
  getTask,
  listReconciliations,
  listTasks,
  refreshReconciliationEvidence,
  resolveReconciliation,
} from '../api'
import type {
  Reconciliation,
  ReconciliationOutcome,
  ReconciliationStatus,
  Task,
  TaskStatus,
} from '../types'

type ReconciliationFilter = ReconciliationStatus | 'all'
type TaskFilter = TaskStatus | 'all'
type CenterAction = 'attempt' | 'compensation' | 'resolve'

interface ResolveRetry {
  reconciliationId: string
  outcome: ReconciliationOutcome
  evidenceSummary: string
  key: string
}

const PAGE_SIZE = 25

function originalErrorMessage(error: unknown): string {
  if (error instanceof ApiProblemError) return error.message
  if (error instanceof Error && error.message) return error.message
  return '请求未完成'
}

export function useReconciliationCenter() {
  const tasks = ref<Task[]>([])
  const taskTotal = ref(0)
  const taskOffset = ref(0)
  const taskFilter = ref<TaskFilter>('all')
  const reconciliations = ref<Reconciliation[]>([])
  const reconciliationFilter = ref<ReconciliationFilter>('all')
  const selectedId = ref<string | null>(null)
  const loading = ref(false)
  const refreshing = ref(false)
  const activeAction = ref<CenterAction | null>(null)
  const message = ref<string | null>(null)
  const error = ref<string | null>(null)

  const selected = computed(
    () => reconciliations.value.find(
      (item) => item.reconciliation_id === selectedId.value,
    ) ?? null,
  )
  const canPreviousTasks = computed(() => taskOffset.value > 0)
  const canNextTasks = computed(
    () => taskOffset.value + tasks.value.length < taskTotal.value,
  )

  let loadGeneration = 0
  let resolveRetry: ResolveRetry | null = null
  const actionKeys = new Map<string, string>()

  function actionKey(action: Exclude<CenterAction, 'resolve'>, id: string): string {
    const scope = `${action}:${id}`
    const existing = actionKeys.get(scope)
    if (existing) return existing
    const key = createIdempotencyKey()
    actionKeys.set(scope, key)
    return key
  }

  function replaceReconciliation(snapshot: Reconciliation): void {
    const index = reconciliations.value.findIndex(
      (item) => item.reconciliation_id === snapshot.reconciliation_id,
    )
    const matchesFilter = reconciliationFilter.value === 'all'
      || snapshot.status === reconciliationFilter.value
    if (index < 0) {
      if (matchesFilter) reconciliations.value = [snapshot, ...reconciliations.value]
      return
    }
    const next = [...reconciliations.value]
    if (matchesFilter) next[index] = snapshot
    else next.splice(index, 1)
    reconciliations.value = next
    if (
      !matchesFilter
      && selectedId.value === snapshot.reconciliation_id
    ) {
      selectedId.value = next[0]?.reconciliation_id ?? null
    }
  }

  function prependTask(snapshot: Task): void {
    const matchesFilter = taskFilter.value === 'all'
      || snapshot.status === taskFilter.value
    if (!matchesFilter) return
    taskTotal.value += 1
    if (taskOffset.value !== 0) return
    tasks.value = [
      snapshot,
      ...tasks.value.filter((item) => item.task_id !== snapshot.task_id),
    ].slice(0, PAGE_SIZE)
  }

  async function reload(): Promise<void> {
    const generation = ++loadGeneration
    loading.value = true
    message.value = null
    error.value = null
    try {
      const [taskPage, records] = await Promise.all([
        listTasks(
          taskFilter.value === 'all' ? undefined : taskFilter.value,
          PAGE_SIZE,
          taskOffset.value,
        ),
        listReconciliations(
          undefined,
          reconciliationFilter.value === 'all'
            ? undefined
            : reconciliationFilter.value,
        ),
      ])
      if (generation !== loadGeneration) return
      tasks.value = taskPage.items
      taskTotal.value = taskPage.total
      reconciliations.value = records
      if (!records.some((item) => item.reconciliation_id === selectedId.value)) {
        selectedId.value = records[0]?.reconciliation_id ?? null
      }
    } catch (caught) {
      if (generation !== loadGeneration) return
      error.value = `加载历史与对账记录失败：${originalErrorMessage(caught)}`
    } finally {
      if (generation === loadGeneration) loading.value = false
    }
  }

  function selectReconciliation(reconciliationId: string): void {
    if (!reconciliations.value.some(
      (item) => item.reconciliation_id === reconciliationId,
    )) return
    selectedId.value = reconciliationId
    message.value = null
    error.value = null
  }

  async function refreshSelectedEvidence(): Promise<void> {
    const current = selected.value
    if (!current || refreshing.value || activeAction.value !== null) return
    refreshing.value = true
    message.value = null
    error.value = null
    try {
      const result = await refreshReconciliationEvidence(current.reconciliation_id)
      if (selectedId.value !== current.reconciliation_id) return
      if (result.reconciliation.reconciliation_id !== current.reconciliation_id) {
        error.value = '证据响应与当前对账记录不匹配，已忽略。'
        return
      }
      replaceReconciliation(result.reconciliation)
      message.value = result.replayed
        ? '当前 Runner 证据未变化。'
        : '已追加一条 Runner 查询证据。'
    } catch (caught) {
      if (selectedId.value !== current.reconciliation_id) return
      error.value = `刷新 Runner 证据失败：${originalErrorMessage(caught)}`
    } finally {
      refreshing.value = false
    }
  }

  async function resolveSelected(
    outcome: ReconciliationOutcome,
    evidenceSummary: string,
  ): Promise<boolean> {
    const current = selected.value
    const summary = evidenceSummary.trim()
    if (
      !current ||
      current.status !== 'pending' ||
      !summary ||
      activeAction.value !== null
    ) return false
    if (
      !resolveRetry ||
      resolveRetry.reconciliationId !== current.reconciliation_id ||
      resolveRetry.outcome !== outcome ||
      resolveRetry.evidenceSummary !== summary
    ) {
      resolveRetry = {
        reconciliationId: current.reconciliation_id,
        outcome,
        evidenceSummary: summary,
        key: createIdempotencyKey(),
      }
    }
    activeAction.value = 'resolve'
    message.value = null
    error.value = null
    try {
      const result = await resolveReconciliation(
        current.reconciliation_id,
        outcome,
        summary,
        resolveRetry.key,
      )
      if (selectedId.value !== current.reconciliation_id) return false
      replaceReconciliation(result.reconciliation)
      resolveRetry = null
      message.value = result.replayed
        ? '已恢复先前提交的人工裁决。'
        : '人工裁决已持久化，原 unknown Tool 账本保持不变。'
      return true
    } catch (caught) {
      if (selectedId.value !== current.reconciliation_id) return false
      error.value = `提交人工裁决失败：${originalErrorMessage(caught)}`
      return false
    } finally {
      activeAction.value = null
    }
  }

  async function createAttempt(): Promise<Task | null> {
    const current = selected.value
    if (!current?.can_create_attempt || activeAction.value !== null) return null
    const scope = `attempt:${current.reconciliation_id}`
    activeAction.value = 'attempt'
    message.value = null
    error.value = null
    try {
      const result = await createReconciliationAttempt(
        current.reconciliation_id,
        actionKey('attempt', current.reconciliation_id),
      )
      if (selectedId.value !== current.reconciliation_id) return null
      replaceReconciliation(result.reconciliation)
      actionKeys.delete(scope)
      prependTask(result.task)
      message.value = result.replayed
        ? '已恢复先前创建的新 attempt 任务。'
        : '已创建新 attempt 任务。'
      return result.task
    } catch (caught) {
      if (selectedId.value !== current.reconciliation_id) return null
      error.value = `创建新 attempt 失败：${originalErrorMessage(caught)}`
      return null
    } finally {
      activeAction.value = null
    }
  }

  async function createCompensation(): Promise<Task | null> {
    const current = selected.value
    if (!current?.can_create_compensation || activeAction.value !== null) return null
    const scope = `compensation:${current.reconciliation_id}`
    activeAction.value = 'compensation'
    message.value = null
    error.value = null
    try {
      const result = await createReconciliationCompensation(
        current.reconciliation_id,
        actionKey('compensation', current.reconciliation_id),
      )
      if (selectedId.value !== current.reconciliation_id) return null
      replaceReconciliation(result.reconciliation)
      actionKeys.delete(scope)
      prependTask(result.task)
      message.value = result.replayed
        ? '已恢复先前创建的反向任务。'
        : '已创建回执绑定的反向任务。'
      return result.task
    } catch (caught) {
      if (selectedId.value !== current.reconciliation_id) return null
      error.value = `创建反向任务失败：${originalErrorMessage(caught)}`
      return null
    } finally {
      activeAction.value = null
    }
  }

  async function taskForNavigation(taskId: string): Promise<Task | null> {
    const cached = tasks.value.find((item) => item.task_id === taskId)
    if (cached) return cached
    try {
      return await getTask(taskId)
    } catch (caught) {
      error.value = `读取关联任务失败：${originalErrorMessage(caught)}`
      return null
    }
  }

  function previousTasks(): void {
    if (!canPreviousTasks.value || loading.value) return
    taskOffset.value = Math.max(0, taskOffset.value - PAGE_SIZE)
  }

  function nextTasks(): void {
    if (!canNextTasks.value || loading.value) return
    taskOffset.value += PAGE_SIZE
  }

  watch(taskFilter, () => {
    taskOffset.value = 0
  })
  watch(
    [taskFilter, taskOffset, reconciliationFilter],
    () => {
      void reload()
    },
    { immediate: true },
  )

  return {
    tasks,
    taskTotal,
    taskOffset,
    taskFilter,
    reconciliations,
    reconciliationFilter,
    selected,
    selectedId,
    loading,
    refreshing,
    activeAction,
    message,
    error,
    canPreviousTasks,
    canNextTasks,
    reload,
    selectReconciliation,
    refreshSelectedEvidence,
    resolveSelected,
    createAttempt,
    createCompensation,
    taskForNavigation,
    previousTasks,
    nextTasks,
  }
}
