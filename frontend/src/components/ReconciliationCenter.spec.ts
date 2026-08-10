import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const composableMock = vi.hoisted(() => ({
  useReconciliationCenter: vi.fn(),
}))

vi.mock('../composables/useReconciliationCenter', () => ({
  useReconciliationCenter: composableMock.useReconciliationCenter,
}))

import type { Reconciliation, Task } from '../types'
import ReconciliationCenter from './ReconciliationCenter.vue'

function makeTask(taskId: string, status: Task['status'] = 'failed'): Task {
  return {
    task_id: taskId,
    conversation_id: null,
    goal: `任务 ${taskId}`,
    status,
    mode: 'workflow',
    privacy_mode: 'local_only',
    constraints: [],
    last_event_seq: 3,
    event_stream: `/api/v1/ws/tasks/${taskId}`,
    created_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T00:00:01Z',
  }
}

function makeReconciliation(): Reconciliation {
  return {
    reconciliation_id: 'reconciliation-1',
    task_id: 'task-1',
    call_id: 'call-1',
    step_id: 'step-1',
    attempt: 1,
    tool_name: 'file.move',
    tool_version: '1.0.0',
    contract_digest: '1'.repeat(64),
    arguments_digest: '2'.repeat(64),
    idempotency: 'key_required',
    runner_id: 'runner-1',
    call_error_code: 'RUNNER_CALL_OUTCOME_UNKNOWN',
    call_resolution_source: 'control_plane',
    call_requested_at: '2026-08-10T00:00:00Z',
    call_started_at: '2026-08-10T00:00:00Z',
    call_finished_at: '2026-08-10T00:00:01Z',
    status: 'pending',
    outcome: null,
    evidence_summary: null,
    resolved_by: null,
    unknown_at: '2026-08-10T00:00:01Z',
    resolved_at: null,
    can_create_attempt: false,
    new_attempt_task_id: null,
    new_attempt_created_at: null,
    can_create_compensation: true,
    compensation_task_id: null,
    compensation_receipt_id: null,
    compensation_created_at: null,
    idempotency_receipt: null,
    receipt_evidence: [{
      evidence_id: 'evidence-1',
      kind: 'no_receipt',
      queried_runner_id: 'runner-1',
      commit_receipt: null,
      error_code: null,
      observed_at: '2026-08-10T00:00:02Z',
    }],
    updated_at: '2026-08-10T00:00:02Z',
  }
}

function makeCenter() {
  const tasks = [makeTask('task-1'), makeTask('task-2')]
  const selected = makeReconciliation()
  return {
    tasks: ref(tasks),
    taskTotal: ref(2),
    taskOffset: ref(0),
    taskFilter: ref('all'),
    reconciliations: ref([selected]),
    reconciliationFilter: ref('all'),
    selected: ref(selected),
    loading: ref(false),
    refreshing: ref(false),
    activeAction: ref(null),
    message: ref(null),
    error: ref(null),
    canPreviousTasks: ref(false),
    canNextTasks: ref(false),
    reload: vi.fn(),
    selectReconciliation: vi.fn(),
    refreshSelectedEvidence: vi.fn(),
    resolveSelected: vi.fn().mockResolvedValue(true),
    createAttempt: vi.fn().mockResolvedValue(null),
    createCompensation: vi.fn().mockResolvedValue(makeTask('task-compensation', 'created')),
    taskForNavigation: vi.fn(async (taskId: string) => (
      tasks.find((task) => task.task_id === taskId) ?? null
    )),
    previousTasks: vi.fn(),
    nextTasks: vi.fn(),
  }
}

let center: ReturnType<typeof makeCenter>

beforeEach(() => {
  center = makeCenter()
  composableMock.useReconciliationCenter.mockReset()
  composableMock.useReconciliationCenter.mockReturnValue(center)
})

describe('ReconciliationCenter', () => {
  it('展示 unknown 不变量，并要求第二次点击才提交不可改写裁决', async () => {
    const wrapper = mount(ReconciliationCenter)

    expect(wrapper.text()).toContain('原 Tool 账本始终保持 unknown')
    expect(wrapper.text()).toContain('不证明无副作用')
    await wrapper.get('[data-testid="reconciliation-summary"]').setValue('已核对目标目录')
    await wrapper.get('[data-testid="resolve-reconciliation"]').trigger('click')

    expect(center.resolveSelected).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请再次点击确认')

    await wrapper.get('[data-testid="resolve-reconciliation"]').trigger('click')
    await flushPromises()

    expect(center.resolveSelected).toHaveBeenCalledWith(
      'accepted_unknown',
      '已核对目标目录',
    )
  })

  it('补偿也需要二次确认，并仅发送服务端返回的新任务快照', async () => {
    const wrapper = mount(ReconciliationCenter)

    await wrapper.get('[data-testid="create-center-compensation"]').trigger('click')
    expect(center.createCompensation).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('committed receipt')

    await wrapper.get('[data-testid="create-center-compensation"]').trigger('click')
    await flushPromises()

    expect(center.createCompensation).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('openTask')).toEqual([[makeTask('task-compensation', 'created')]])
  })

  it('活动任务未结束时禁用其他历史任务切换', async () => {
    const wrapper = mount(ReconciliationCenter, {
      props: { activeTaskId: 'task-1', taskSwitchLocked: true },
    })
    const historyItems = wrapper.findAll('.history-item')

    expect(historyItems[0].attributes('disabled')).toBeUndefined()
    expect(historyItems[1].attributes('disabled')).toBeDefined()
    await historyItems[0].trigger('click')
    await flushPromises()

    expect(center.taskForNavigation).toHaveBeenCalledWith('task-1')
    expect(wrapper.emitted('openTask')).toEqual([[makeTask('task-1')]])
  })
})
