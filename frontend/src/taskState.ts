import type { Task, TaskEvent, TaskStatus } from './types'

const TASK_STATUSES: ReadonlySet<TaskStatus> = new Set([
  'created',
  'classifying',
  'running',
  'waiting_approval',
  'waiting_reconciliation',
  'succeeded',
  'failed',
  'cancelled',
  'paused',
])

function eventStatus(event: TaskEvent): TaskStatus | null {
  if (event.type === 'task.status_changed') {
    const status = event.payload.to
    return typeof status === 'string' && TASK_STATUSES.has(status as TaskStatus)
      ? (status as TaskStatus)
      : null
  }

  if (event.type === 'task.completed') {
    return 'succeeded'
  }
  if (event.type === 'task.failed') {
    return 'failed'
  }
  if (event.type === 'task.cancelled') {
    return 'cancelled'
  }
  if (event.type === 'task.waiting_reconciliation') {
    return 'waiting_reconciliation'
  }

  return null
}

export function deriveTaskStatus(task: Task | null, events: TaskEvent[]): TaskStatus | null {
  let latestEventStatus: TaskStatus | null = null
  let latestEventSeq = Number.NEGATIVE_INFINITY

  for (const event of events) {
    const status = eventStatus(event)
    if (status !== null && event.seq >= latestEventSeq) {
      latestEventStatus = status
      latestEventSeq = event.seq
    }
  }

  if (task !== null && (latestEventStatus === null || task.last_event_seq >= latestEventSeq)) {
    return task.status
  }

  return latestEventStatus
}
