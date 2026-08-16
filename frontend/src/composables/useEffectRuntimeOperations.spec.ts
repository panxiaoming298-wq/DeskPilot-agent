import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  EffectRuntimeAuditEvent,
  EffectRuntimeOperationsSnapshot,
} from '../types'

const apiMocks = vi.hoisted(() => ({
  createIdempotencyKey: vi.fn(),
  getEffectRuntimeAlertNotifications: vi.fn(),
  getEffectRuntimeAudit: vi.fn(),
  getEffectRuntimeAuditExport: vi.fn(),
  getEffectRuntimeOperations: vi.fn(),
  requeueOutboxDeadLetter: vi.fn(),
  runEffectRuntimeRetention: vi.fn(),
  sampleEffectRuntimeMetrics: vi.fn(),
}))

vi.mock('../api', () => ({
  ApiProblemError: class ApiProblemError extends Error {},
  ...apiMocks,
}))

import { useEffectRuntimeOperations } from './useEffectRuntimeOperations'

const DIGEST = 'a'.repeat(64)

function auditEvent(sequence = 1, action = 'metrics.sampled'): EffectRuntimeAuditEvent {
  return {
    event_id: `audit-${sequence}`,
    sequence,
    action,
    actor_id: 'local-session',
    request_digest: DIGEST,
    result_digest: DIGEST,
    previous_event_digest: sequence === 1 ? null : DIGEST,
    event_digest: DIGEST,
    details: {},
    occurred_at: '2026-08-15T08:00:00Z',
  }
}

function operationsSnapshot(): EffectRuntimeOperationsSnapshot {
  return {
    schema_version: 'deskpilot.effect-runtime-operations.v1',
    database_time: '2026-08-15T08:00:00Z',
    graph_controls: {
      total: 1,
      pending: 0,
      processing: 0,
      applied: 1,
      superseded: 0,
      actionable: 0,
      claim_expired: 0,
      unrouted: 0,
      oldest_actionable_at: null,
    },
    admissions: {
      total: 1,
      pending: 0,
      granted: 0,
      released: 1,
      cancelled: 0,
      withdrawn: 0,
      expired: 0,
      live_pending: 0,
      live_granted: 0,
      expired_leases: 0,
      scheduler_revision: 1,
      next_grant_sequence: 2,
      configuration_digest: DIGEST,
      global_limit: 8,
      per_graph_limit: 4,
      default_tool_limit: 4,
    },
    ready_projection: {
      projected_graphs: 1,
      projected_nodes: 2,
      ready_nodes: 1,
      missing_live_graphs: 0,
      event_drift_graphs: 0,
      row_count_drift_graphs: 0,
      rebuilds_observed: 1,
      last_rebuilt_at: '2026-08-15T07:59:00Z',
    },
    outbox: {
      total: 2,
      pending_ready: 0,
      retry_scheduled: 0,
      in_flight: 0,
      published: 1,
      dead_lettered: 1,
      inbox_receipts: 1,
      oldest_pending_at: null,
      oldest_dead_lettered_at: '2026-08-15T07:00:00Z',
    },
    alerts: [],
    graph_control_samples: [],
    admission_samples: [],
    ready_projection_samples: [],
    outbox_samples: [{
      message_id: 'message-1',
      task_id: 'task-1',
      event_id: 'event-1',
      event_seq: 1,
      topic: 'task.events',
      state: 'dead_lettered',
      payload_digest: DIGEST,
      attempt_count: 8,
      claim_owner_id: null,
      claim_fencing_token: 3,
      available_at: '2026-08-15T07:00:00Z',
      claim_expires_at: null,
      published_at: null,
      dead_lettered_at: '2026-08-15T07:00:00Z',
      error_digest: DIGEST,
      created_at: '2026-08-15T06:00:00Z',
    }],
    snapshot_digest: DIGEST,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  apiMocks.createIdempotencyKey.mockReturnValue('deskpilot-ui-operation-key')
  apiMocks.getEffectRuntimeOperations.mockResolvedValue(operationsSnapshot())
  apiMocks.getEffectRuntimeAudit.mockResolvedValue({
    events: [auditEvent()],
    next_after_sequence: 1,
    has_more: false,
  })
  apiMocks.getEffectRuntimeAlertNotifications.mockResolvedValue({
    notifications: [],
    next_after_sequence: 0,
    has_more: false,
  })
})

describe('useEffectRuntimeOperations', () => {
  it('从服务端加载四域真值，并用采样响应推进快照与审计链', async () => {
    const operations = useEffectRuntimeOperations()
    await operations.reload()

    expect(operations.snapshot.value).toStrictEqual(operationsSnapshot())
    expect(operations.deadLetters.value.map((item) => item.message_id)).toEqual([
      'message-1',
    ])

    const sampled = operationsSnapshot()
    sampled.outbox.pending_ready = 1
    apiMocks.sampleEffectRuntimeMetrics.mockResolvedValue({
      snapshot: sampled,
      audit_event: auditEvent(2),
      alert_notifications: [],
    })

    expect(await operations.sampleMetrics()).toBe(true)
    expect(operations.snapshot.value?.outbox.pending_ready).toBe(1)
    expect(operations.auditEvents.value.map((event) => event.sequence)).toEqual([1, 2])
    expect(operations.message.value).toContain('hash-chain')
  })

  it('沿冻结 cursor 完整导出脱敏审计并校验终点', async () => {
    const operations = useEffectRuntimeOperations()
    const createObjectURL = vi.fn().mockReturnValue('blob:audit-export')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    apiMocks.getEffectRuntimeAuditExport
      .mockResolvedValueOnce({
        schema_version: 'deskpilot.effect-runtime-audit-export.v1',
        export_id: `opx_${DIGEST}`,
        database_time: '2026-08-15T08:00:00Z',
        through_sequence: 2,
        through_event_digest: DIGEST,
        events: [auditEvent(1)],
        page_digest: DIGEST,
        next_cursor: 'cursor-2',
        has_more: true,
      })
      .mockResolvedValueOnce({
        schema_version: 'deskpilot.effect-runtime-audit-export.v1',
        export_id: `opx_${DIGEST}`,
        database_time: '2026-08-15T08:00:00Z',
        through_sequence: 2,
        through_event_digest: DIGEST,
        events: [auditEvent(2)],
        page_digest: DIGEST,
        next_cursor: null,
        has_more: false,
      })

    expect(await operations.downloadAuditExport()).toBe(true)
    expect(apiMocks.getEffectRuntimeAuditExport.mock.calls).toEqual([[null], ['cursor-2']])
    expect(createObjectURL).toHaveBeenCalledOnce()
    expect(click).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:audit-export')
    expect(operations.message.value).toContain('冻结至 #2')
    click.mockRestore()
    vi.unstubAllGlobals()
  })

  it('retention 传输失败后只在人工重试时复用同一幂等键', async () => {
    const operations = useEffectRuntimeOperations()
    apiMocks.runEffectRuntimeRetention
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({
        cutoff: '2026-07-16T08:00:00Z',
        counts: {
          graph_controls: 1,
          admissions: 2,
          ready_checkpoints: 3,
          ready_nodes: 4,
          ready_states: 5,
          published_outbox: 6,
          inbox_receipts: 7,
        },
        manifest_digest: DIGEST,
        audit_event: auditEvent(2, 'retention.completed'),
      })

    expect(await operations.runRetention(30)).toBe(false)
    expect(await operations.runRetention(30)).toBe(true)

    expect(apiMocks.createIdempotencyKey).toHaveBeenCalledOnce()
    expect(apiMocks.runEffectRuntimeRetention.mock.calls).toEqual([
      [30, 'deskpilot-ui-operation-key'],
      [30, 'deskpilot-ui-operation-key'],
    ])
    expect(operations.message.value).toContain('28 条')
  })

  it('只允许当前 DLQ 样本重新入队，并拒绝错配响应且保留重试键', async () => {
    const operations = useEffectRuntimeOperations()
    await operations.reload()
    apiMocks.requeueOutboxDeadLetter
      .mockResolvedValueOnce({
        message_id: 'another-message',
        attempt_count: 0,
        claim_fencing_token: 4,
        available_at: '2026-08-15T08:00:00Z',
        audit_event: auditEvent(2, 'outbox.dead_letter.requeued'),
      })
      .mockResolvedValueOnce({
        message_id: 'message-1',
        attempt_count: 0,
        claim_fencing_token: 4,
        available_at: '2026-08-15T08:00:00Z',
        audit_event: auditEvent(2, 'outbox.dead_letter.requeued'),
      })

    expect(await operations.requeueDeadLetter('not-in-snapshot')).toBe(false)
    expect(await operations.requeueDeadLetter('message-1')).toBe(false)
    expect(operations.error.value).toContain('响应与当前消息不匹配')
    expect(await operations.requeueDeadLetter('message-1')).toBe(true)

    expect(apiMocks.createIdempotencyKey).toHaveBeenCalledOnce()
    expect(apiMocks.requeueOutboxDeadLetter.mock.calls).toEqual([
      ['message-1', 'deskpilot-ui-operation-key'],
      ['message-1', 'deskpilot-ui-operation-key'],
    ])
  })
})
