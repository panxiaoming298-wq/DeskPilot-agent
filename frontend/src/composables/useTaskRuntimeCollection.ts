import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'

import { deriveTaskStatus } from '../taskState'
import type { Task, TaskEvent, TaskStatus, TaskStreamState } from '../types'
import { useTaskApproval } from './useTaskApproval'
import { useTaskControl } from './useTaskControl'
import { useTaskEvents } from './useTaskEvents'
import { useTaskReconciliation } from './useTaskReconciliation'

export const TASK_RUNTIME_CAPACITY = 3

const TERMINAL_STATUSES = new Set<TaskStatus>(['succeeded', 'failed', 'cancelled'])
const INPUT_REQUIRED_EVENTS = new Set([
  'agent.input.required',
  'task.input.required',
  'workbench.input.required',
])
const INPUT_RESOLVED_EVENTS = new Set([
  'agent.input.resolved',
  'task.input.resolved',
  'workbench.input.resolved',
])

type TaskEventsRuntime = ReturnType<typeof useTaskEvents>
type TaskControlRuntime = ReturnType<typeof useTaskControl>
type TaskApprovalRuntime = ReturnType<typeof useTaskApproval>
type TaskReconciliationRuntime = ReturnType<typeof useTaskReconciliation>

export interface TaskRuntimeSlot {
  slotId: number
  task: Ref<Task | null>
  eventsRuntime: TaskEventsRuntime
  controlRuntime: TaskControlRuntime
  approvalRuntime: TaskApprovalRuntime
  reconciliationRuntime: TaskReconciliationRuntime
  lastReadSeq: Ref<number>
  status: ComputedRef<TaskStatus | null>
  terminal: ComputedRef<boolean>
  unreadCount: ComputedRef<number>
  pendingInput: ComputedRef<boolean>
  tokenUsage: ComputedRef<number>
}

export interface TaskRuntimeCard {
  slot_id: number
  task: Task
  status: TaskStatus
  selected: boolean
  connection_state: TaskStreamState
  connected: boolean
  event_cursor: number
  unread_count: number
  pending_approval: boolean
  pending_input: boolean
  pending_reconciliation: boolean
  token_usage: number
}

function latestEventSeq(events: TaskEvent[]): number {
  return events.reduce((latest, event) => Math.max(latest, event.seq), 0)
}

function pendingInputFromEvents(events: TaskEvent[]): boolean {
  let requiredSeq = 0
  let resolvedSeq = 0
  for (const event of events) {
    if (INPUT_REQUIRED_EVENTS.has(event.type)) requiredSeq = Math.max(requiredSeq, event.seq)
    if (INPUT_RESOLVED_EVENTS.has(event.type)) resolvedSeq = Math.max(resolvedSeq, event.seq)
  }
  return requiredSeq > resolvedSeq
}

function numericValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0
}

function tokenUsageFromEvents(events: TaskEvent[]): number {
  let total = 0
  for (const event of events) {
    if (event.type !== 'model.usage') continue
    const usage = event.payload.usage
    if (usage && typeof usage === 'object' && !Array.isArray(usage)) {
      const record = usage as Record<string, unknown>
      const explicitTotal = numericValue(record.total_tokens)
      total += explicitTotal || (
        numericValue(record.input_tokens) + numericValue(record.output_tokens)
      )
      continue
    }
    const explicitTotal = numericValue(event.payload.total_tokens)
    total += explicitTotal || (
      numericValue(event.payload.input_tokens) + numericValue(event.payload.output_tokens)
    )
  }
  return total
}

export function useTaskRuntimeCollection(capacity = TASK_RUNTIME_CAPACITY) {
  if (!Number.isInteger(capacity) || capacity < TASK_RUNTIME_CAPACITY) {
    throw new Error(`Task runtime capacity must be at least ${TASK_RUNTIME_CAPACITY}`)
  }

  const selectedTaskId = ref<string | null>(null)
  const slots: TaskRuntimeSlot[] = Array.from({ length: capacity }, (_, index) => {
    const task = ref<Task | null>(null)
    const eventsRuntime = useTaskEvents()
    const controlRuntime = useTaskControl(task)
    const approvalRuntime = useTaskApproval(task, eventsRuntime.events)
    const reconciliationRuntime = useTaskReconciliation(task, eventsRuntime.events)
    const lastReadSeq = ref(0)
    const status = computed(() => deriveTaskStatus(task.value, eventsRuntime.events.value))
    const terminal = computed(() => status.value !== null && TERMINAL_STATUSES.has(status.value))
    const unreadCount = computed(() => eventsRuntime.events.value.filter(
      (event) => event.seq > lastReadSeq.value,
    ).length)
    const pendingInput = computed(() => pendingInputFromEvents(eventsRuntime.events.value))
    const tokenUsage = computed(() => tokenUsageFromEvents(eventsRuntime.events.value))

    watch(eventsRuntime.events, (events) => {
      const latest = events.at(-1)
      if (task.value && latest && latest.seq > task.value.last_event_seq) {
        const eventStatus = deriveTaskStatus(task.value, events)
        task.value = {
          ...task.value,
          ...(eventStatus ? { status: eventStatus } : {}),
          last_event_seq: latest.seq,
          updated_at: latest.timestamp,
        }
      }
      if (task.value?.task_id === selectedTaskId.value) {
        lastReadSeq.value = Math.max(lastReadSeq.value, latestEventSeq(events))
      }
    })

    return {
      slotId: index + 1,
      task,
      eventsRuntime,
      controlRuntime,
      approvalRuntime,
      reconciliationRuntime,
      lastReadSeq,
      status,
      terminal,
      unreadCount,
      pendingInput,
      tokenUsage,
    }
  })

  const selectedRuntime = computed(() => {
    const selected = slots.find((slot) => slot.task.value?.task_id === selectedTaskId.value)
    return selected ?? slots.find((slot) => slot.task.value !== null) ?? null
  })

  const activeTaskCount = computed(() => slots.filter(
    (slot) => slot.task.value !== null && !slot.terminal.value,
  ).length)
  const hasTaskCapacity = computed(() => activeTaskCount.value < capacity)

  const taskCards = computed<TaskRuntimeCard[]>(() => slots.flatMap((slot) => {
    const task = slot.task.value
    const status = slot.status.value
    if (!task || !status) return []
    const eventCursor = Math.max(task.last_event_seq, latestEventSeq(slot.eventsRuntime.events.value))
    return [{
      slot_id: slot.slotId,
      task,
      status,
      selected: task.task_id === selectedTaskId.value,
      connection_state: slot.eventsRuntime.connectionState.value,
      connected: slot.eventsRuntime.connected.value,
      event_cursor: eventCursor,
      unread_count: slot.unreadCount.value,
      pending_approval: slot.approvalRuntime.approval.value?.status === 'pending'
        || status === 'waiting_approval',
      pending_input: slot.pendingInput.value,
      pending_reconciliation: status === 'waiting_reconciliation',
      token_usage: slot.tokenUsage.value,
    }]
  }))

  function selectTask(taskId: string): boolean {
    const slot = slots.find((candidate) => candidate.task.value?.task_id === taskId)
    if (!slot) return false
    selectedTaskId.value = taskId
    slot.lastReadSeq.value = Math.max(
      slot.lastReadSeq.value,
      slot.task.value?.last_event_seq ?? 0,
      latestEventSeq(slot.eventsRuntime.events.value),
    )
    return true
  }

  function resetSlot(slot: TaskRuntimeSlot): void {
    slot.eventsRuntime.reset()
    slot.controlRuntime.reset()
    slot.approvalRuntime.reset()
    slot.reconciliationRuntime.reset()
    slot.task.value = null
    slot.lastReadSeq.value = 0
  }

  function trackTask(snapshot: Task): boolean {
    const existing = slots.find((slot) => slot.task.value?.task_id === snapshot.task_id)
    if (existing) {
      existing.task.value = snapshot
      return selectTask(snapshot.task_id)
    }

    const available = slots.find((slot) => slot.task.value === null)
      ?? slots.find((slot) => slot.terminal.value)
    if (!available) return false

    resetSlot(available)
    available.task.value = snapshot
    available.lastReadSeq.value = snapshot.last_event_seq
    selectedTaskId.value = snapshot.task_id
    available.eventsRuntime.connect(snapshot.task_id)
    return true
  }

  return {
    capacity,
    slots,
    selectedTaskId,
    selectedRuntime,
    taskCards,
    activeTaskCount,
    hasTaskCapacity,
    selectTask,
    trackTask,
  }
}
