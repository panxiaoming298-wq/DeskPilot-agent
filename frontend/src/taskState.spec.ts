import { describe, expect, it } from 'vitest'

import { deriveTaskStatus } from './taskState'
import type { Task, TaskEvent, TaskStatus } from './types'

function makeTask(status: TaskStatus, lastEventSeq: number): Task {
  return {
    task_id: 'task-1',
    conversation_id: null,
    goal: 'test goal',
    status,
    mode: 'execute',
    privacy_mode: 'local_only',
    constraints: [],
    last_event_seq: lastEventSeq,
    event_stream: '/api/v1/tasks/task-1/events',
    created_at: '2026-08-09T00:00:00Z',
    updated_at: '2026-08-09T00:00:00Z',
  }
}

function makeEvent(
  seq: number,
  type: string,
  payload: Record<string, unknown> = {},
): TaskEvent {
  return {
    event_id: `event-${seq}`,
    task_id: 'task-1',
    seq,
    type,
    timestamp: '2026-08-09T00:00:00Z',
    trace_id: 'trace-1',
    payload,
  }
}

describe('deriveTaskStatus', () => {
  it('uses a control response snapshot that is ahead of stale websocket events', () => {
    const task = makeTask('paused', 8)
    const events = [makeEvent(7, 'task.status_changed', { to: 'running' })]

    expect(deriveTaskStatus(task, events)).toBe('paused')
  })

  it('uses the newest websocket status when it is ahead of the snapshot', () => {
    const task = makeTask('running', 4)
    const events = [
      makeEvent(6, 'task.status_changed', { to: 'paused' }),
      makeEvent(5, 'task.status_changed', { to: 'running' }),
    ]

    expect(deriveTaskStatus(task, events)).toBe('paused')
  })

  it('prefers the snapshot when its sequence equals the latest status event', () => {
    const task = makeTask('running', 6)
    const events = [makeEvent(6, 'task.status_changed', { to: 'paused' })]

    expect(deriveTaskStatus(task, events)).toBe('running')
  })

  it('maps task.cancelled to the cancelled status', () => {
    expect(deriveTaskStatus(null, [makeEvent(9, 'task.cancelled')])).toBe('cancelled')
  })

  it('ignores invalid status_changed payload values', () => {
    const events = [
      makeEvent(10, 'task.status_changed', { to: 'not-a-status' }),
      makeEvent(8, 'task.status_changed', { to: 'running' }),
    ]

    expect(deriveTaskStatus(null, events)).toBe('running')
  })

  it('returns the snapshot when no event expresses a status', () => {
    const task = makeTask('classifying', 3)
    const events = [makeEvent(5, 'agent.started', { agent: 'planner' })]

    expect(deriveTaskStatus(task, events)).toBe('classifying')
  })

  it('returns null for empty input', () => {
    expect(deriveTaskStatus(null, [])).toBeNull()
  })

  it.each([
    'created',
    'classifying',
    'running',
    'waiting_approval',
    'succeeded',
    'failed',
    'cancelled',
    'paused',
  ] satisfies TaskStatus[])('accepts the legal status_changed value %s', (status) => {
    expect(
      deriveTaskStatus(null, [makeEvent(1, 'task.status_changed', { to: status })]),
    ).toBe(status)
  })

  it.each([
    ['task.completed', 'succeeded'],
    ['task.failed', 'failed'],
  ] as const)('maps %s to %s', (type, expected) => {
    expect(deriveTaskStatus(null, [makeEvent(1, type)])).toBe(expected)
  })
})
