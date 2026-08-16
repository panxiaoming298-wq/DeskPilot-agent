import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { EffectRuntimeOperationsSnapshot } from '../types'
import { useEffectRuntimeOperations } from '../composables/useEffectRuntimeOperations'
import EffectRuntimeOperations from './EffectRuntimeOperations.vue'

vi.mock('../composables/useEffectRuntimeOperations', () => ({
  useEffectRuntimeOperations: vi.fn(),
}))

const DIGEST = 'a'.repeat(64)

function snapshot(): EffectRuntimeOperationsSnapshot {
  return {
    schema_version: 'deskpilot.effect-runtime-operations.v1',
    database_time: '2026-08-15T08:00:00Z',
    graph_controls: {
      total: 2, pending: 1, processing: 0, applied: 1, superseded: 0,
      actionable: 1, claim_expired: 0, unrouted: 1,
      oldest_actionable_at: '2026-08-15T07:00:00Z',
    },
    admissions: {
      total: 2, pending: 1, granted: 1, released: 0, cancelled: 0,
      withdrawn: 0, expired: 0, live_pending: 1, live_granted: 1,
      expired_leases: 0, scheduler_revision: 4, next_grant_sequence: 8,
      configuration_digest: DIGEST, global_limit: 8, per_graph_limit: 4,
      default_tool_limit: 4,
    },
    ready_projection: {
      projected_graphs: 1, projected_nodes: 4, ready_nodes: 2,
      missing_live_graphs: 0, event_drift_graphs: 1, row_count_drift_graphs: 0,
      rebuilds_observed: 1, last_rebuilt_at: '2026-08-15T07:30:00Z',
    },
    outbox: {
      total: 3, pending_ready: 1, retry_scheduled: 0, in_flight: 0,
      published: 1, dead_lettered: 1, inbox_receipts: 2,
      oldest_pending_at: '2026-08-15T07:00:00Z',
      oldest_dead_lettered_at: '2026-08-15T06:00:00Z',
    },
    alerts: [{ code: 'READY_EVENT_DRIFT', severity: 'critical', domain: 'ready', count: 1 }],
    graph_control_samples: [{
      control_id: 'control-1', task_id: 'task-1', graph_id: 'graph-1', command: 'cancel',
      request_digest: DIGEST, status: 'pending', revision: 2, attempt_count: 1,
      target_owner_id: null, target_fencing_token: null, claim_owner_id: null,
      claim_fencing_token: 0, claim_expires_at: null, last_error_code: null,
      updated_at: '2026-08-15T08:00:00Z',
    }],
    admission_samples: [],
    ready_projection_samples: [],
    outbox_samples: [{
      message_id: 'message-1', task_id: 'task-1', event_id: 'event-1', event_seq: 1,
      topic: 'task.events', state: 'dead_lettered', payload_digest: DIGEST,
      attempt_count: 8, claim_owner_id: null, claim_fencing_token: 3,
      available_at: '2026-08-15T06:00:00Z', claim_expires_at: null,
      published_at: null, dead_lettered_at: '2026-08-15T06:00:00Z',
      error_digest: DIGEST, created_at: '2026-08-15T05:00:00Z',
    }],
    snapshot_digest: DIGEST,
  }
}

function management() {
  return {
    snapshot: ref<EffectRuntimeOperationsSnapshot | null>(snapshot()),
    auditEvents: ref([{
      event_id: 'audit-1', sequence: 1, action: 'metrics.sampled',
      actor_id: 'local-session', request_digest: DIGEST, result_digest: DIGEST,
      previous_event_digest: null, event_digest: DIGEST, details: {},
      occurred_at: '2026-08-15T08:00:00Z',
    }]),
    auditHasMore: ref(false),
    alertNotifications: ref([{
      notification_id: 'notification-1', sequence: 1,
      alert_code: 'READY_EVENT_DRIFT', transition: 'opened',
      severity: 'critical', domain: 'ready', count: 1, alert_revision: 1,
      snapshot_digest: DIGEST, audit_event_id: 'audit-1', audit_sequence: 1,
      previous_event_digest: null, event_digest: DIGEST,
      occurred_at: '2026-08-15T08:00:00Z',
    }]),
    alertNotificationsHaveMore: ref(false),
    deadLetters: ref([]),
    loading: ref(false),
    activeAction: ref(null),
    message: ref(null),
    error: ref(null),
    reload: vi.fn().mockResolvedValue(undefined),
    sampleMetrics: vi.fn().mockResolvedValue(true),
    runRetention: vi.fn().mockResolvedValue(true),
    requeueDeadLetter: vi.fn().mockResolvedValue(true),
    downloadAuditExport: vi.fn().mockResolvedValue(true),
    dismissFeedback: vi.fn(),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('EffectRuntimeOperations', () => {
  it('展示四域脱敏真值、稳定告警与审计链', async () => {
    const runtime = management()
    vi.mocked(useEffectRuntimeOperations).mockReturnValue(
      runtime as unknown as ReturnType<typeof useEffectRuntimeOperations>,
    )

    const wrapper = mount(EffectRuntimeOperations)
    await flushPromises()

    expect(runtime.reload).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('运行时运维与审计')
    expect(wrapper.text()).toContain('READY_EVENT_DRIFT')
    expect(wrapper.text()).toContain('control-1')
    expect(wrapper.text()).toContain('metrics.sampled')
    expect(wrapper.text()).toContain('opened')
    expect(wrapper.text()).toContain('payload aaaaaaaaaa…aaaaaaaa')
    expect(wrapper.text()).not.toContain('error body')

    const sampleButton = wrapper.findAll('button').find(
      (button) => button.text() === '采样并审计',
    )
    await sampleButton?.trigger('click')
    expect(runtime.sampleMetrics).toHaveBeenCalledOnce()
    const exportButton = wrapper.findAll('button').find(
      (button) => button.text() === '导出冻结审计',
    )
    await exportButton?.trigger('click')
    expect(runtime.downloadAuditExport).toHaveBeenCalledOnce()
  })

  it('retention 与 DLQ requeue 都必须经过显式二次确认', async () => {
    const runtime = management()
    vi.mocked(useEffectRuntimeOperations).mockReturnValue(
      runtime as unknown as ReturnType<typeof useEffectRuntimeOperations>,
    )
    const wrapper = mount(EffectRuntimeOperations)
    await flushPromises()

    const requeue = wrapper.findAll('button').find(
      (button) => button.text() === '重新入队',
    )
    await requeue?.trigger('click')
    expect(runtime.requeueDeadLetter).not.toHaveBeenCalled()
    const requeueDialog = wrapper.get('[aria-label="确认重新入队"]')
    expect(requeueDialog.text()).toContain('不会展示或修改 payload')
    await requeueDialog.findAll('button')[0].trigger('click')
    await flushPromises()
    expect(runtime.requeueDeadLetter).toHaveBeenCalledWith('message-1')

    await wrapper.get('input[type="number"]').setValue(45)
    const retention = wrapper.findAll('button').find(
      (button) => button.text() === '运行 retention',
    )
    await retention?.trigger('click')
    expect(runtime.runRetention).not.toHaveBeenCalled()
    const retentionDialog = wrapper.get('[aria-label="确认运行 retention"]')
    expect(retentionDialog.text()).toContain('不可逆清理')
    await retentionDialog.findAll('button')[0].trigger('click')
    await flushPromises()
    expect(runtime.runRetention).toHaveBeenCalledWith(45)
  })
})
