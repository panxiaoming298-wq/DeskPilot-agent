import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick, ref, type Ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App.vue'
import type {
  Approval,
  ApprovalAction,
  Reconciliation,
  Task,
  TaskControlAction,
  TaskEvent,
  TaskStreamState,
} from './types'

const apiMocks = vi.hoisted(() => ({
  createTask: vi.fn(),
}))

const eventComposableMocks = vi.hoisted(() => ({
  useTaskEvents: vi.fn(),
}))

const controlComposableMocks = vi.hoisted(() => ({
  useTaskControl: vi.fn(),
}))

const approvalComposableMocks = vi.hoisted(() => ({
  useTaskApproval: vi.fn(),
}))

const reconciliationComposableMocks = vi.hoisted(() => ({
  useTaskReconciliation: vi.fn(),
}))

vi.mock('./api', () => ({
  createTask: apiMocks.createTask,
}))

vi.mock('./components/ReconciliationCenter.vue', () => ({
  default: {
    name: 'ReconciliationCenter',
    template: '<section data-testid="reconciliation-center-stub">任务历史与集中对账</section>',
  },
}))

vi.mock('./composables/useTaskEvents', () => ({
  useTaskEvents: eventComposableMocks.useTaskEvents,
}))

vi.mock('./composables/useTaskControl', () => ({
  useTaskControl: controlComposableMocks.useTaskControl,
}))

vi.mock('./composables/useTaskApproval', () => ({
  useTaskApproval: approvalComposableMocks.useTaskApproval,
}))

vi.mock('./composables/useTaskReconciliation', () => ({
  useTaskReconciliation: reconciliationComposableMocks.useTaskReconciliation,
}))

let events: Ref<TaskEvent[]>
let connected: Ref<boolean>
let streamError: Ref<string | null>
let connectionState: Ref<TaskStreamState>
let recoveryMessage: Ref<string | null>
let reconnectAttempt: Ref<number>
let retryDelayMs: Ref<number>
let activeAction: Ref<TaskControlAction | null>
let controlMessage: Ref<string | null>
let controlError: Ref<string | null>
let approval: Ref<Approval | null>
let approvalLoading: Ref<boolean>
let approvalAction: Ref<ApprovalAction | null>
let approvalMessage: Ref<string | null>
let approvalError: Ref<string | null>
let reconciliation: Ref<Reconciliation | null>
let reconciliationLoading: Ref<boolean>
let reconciliationRefreshing: Ref<boolean>
let reconciliationCompensating: Ref<boolean>
let reconciliationMessage: Ref<string | null>
let reconciliationError: Ref<string | null>
let capturedTask: Ref<Task | null>

const connect = vi.fn()
const reconnectNow = vi.fn()
const resetEvents = vi.fn()
const runControl = vi.fn()
const resetControl = vi.fn()
const runApproval = vi.fn()
const resetApproval = vi.fn()
const refreshEvidence = vi.fn()
const createCompensation = vi.fn()
const resetReconciliation = vi.fn()

function makeTask(
  status: Task['status'],
  overrides: Partial<Task> = {},
): Task {
  return {
    task_id: 'task-1',
    conversation_id: null,
    goal: '验证任务控制',
    status,
    mode: 'balanced',
    privacy_mode: 'local_only',
    constraints: ['read_only', 'no_cloud'],
    last_event_seq: 5,
    event_stream: '/api/v1/tasks/task-1/events',
    created_at: '2026-08-09T08:00:00Z',
    updated_at: '2026-08-09T08:00:01Z',
    ...overrides,
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
    timestamp: `2026-08-09T08:00:0${seq}Z`,
    trace_id: 'trace-1',
    payload,
  }
}

function makeApproval(overrides: Partial<Approval> = {}): Approval {
  return {
    approval_id: 'approval-1',
    decision_id: 'decision-1',
    task_id: 'task-1',
    call_id: 'call-1',
    status: 'pending',
    decision: null,
    preview_hash: 'preview-1',
    title: '请确认磁盘信息读取',
    purpose: '完成当前任务',
    tool_name: 'computer.disk_usage',
    tool_version: '1.0.0',
    risk_level: 'R1',
    capabilities: ['filesystem.metadata.read'],
    resource_scope: [],
    consequences: [],
    reversible: true,
    data_egress: { enabled: false, destination: null },
    policy_rule_id: 'rule-1',
    policy_revision: 'deskpilot-policy-v1',
    reason_code: 'ASK_FOR_TEST',
    requested_at: '2026-08-09T08:00:00Z',
    expires_at: '2026-08-09T08:05:00Z',
    resolved_at: null,
    consumed_at: null,
    resolution_reason: null,
    updated_at: '2026-08-09T08:00:00Z',
    ...overrides,
  }
}

function mountApp(): VueWrapper {
  return mount(App, {
    global: {
      stubs: {
        ProviderSettings: { template: '<div data-testid="provider-settings" />' },
        TaskEventItem: {
          props: ['event'],
          template: '<li class="event-stub">{{ event.type }}</li>',
        },
      },
    },
  })
}

async function createVisibleTask(wrapper: VueWrapper, task: Task): Promise<void> {
  apiMocks.createTask.mockResolvedValueOnce(task)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
}

beforeEach(() => {
  events = ref([])
  connected = ref(false)
  streamError = ref(null)
  connectionState = ref('idle')
  recoveryMessage = ref(null)
  reconnectAttempt = ref(0)
  retryDelayMs = ref(0)
  activeAction = ref(null)
  controlMessage = ref(null)
  controlError = ref(null)
  approval = ref(null)
  approvalLoading = ref(false)
  approvalAction = ref(null)
  approvalMessage = ref(null)
  approvalError = ref(null)
  reconciliation = ref(null)
  reconciliationLoading = ref(false)
  reconciliationRefreshing = ref(false)
  reconciliationCompensating = ref(false)
  reconciliationMessage = ref(null)
  reconciliationError = ref(null)
  capturedTask = ref(null)

  connect.mockReset()
  reconnectNow.mockReset()
  resetEvents.mockReset()
  runControl.mockReset()
  resetControl.mockReset()
  runApproval.mockReset()
  resetApproval.mockReset()
  refreshEvidence.mockReset()
  createCompensation.mockReset()
  resetReconciliation.mockReset()
  apiMocks.createTask.mockReset()

  eventComposableMocks.useTaskEvents.mockReturnValue({
    events,
    connected,
    streamError,
    connectionState,
    recoveryMessage,
    reconnectAttempt,
    retryDelayMs,
    connect,
    reconnectNow,
    reset: resetEvents,
  })

  controlComposableMocks.useTaskControl.mockImplementation((task: Ref<Task | null>) => {
    capturedTask = task
    return {
      activeAction,
      controlMessage,
      controlError,
      runControl,
      reset: resetControl,
    }
  })

  approvalComposableMocks.useTaskApproval.mockReturnValue({
    approval,
    loading: approvalLoading,
    activeAction: approvalAction,
    approvalMessage,
    approvalError,
    runApproval,
    reset: resetApproval,
  })

  reconciliationComposableMocks.useTaskReconciliation.mockReturnValue({
    reconciliation,
    loading: reconciliationLoading,
    refreshing: reconciliationRefreshing,
    compensating: reconciliationCompensating,
    message: reconciliationMessage,
    error: reconciliationError,
    refreshEvidence,
    createCompensation,
    reset: resetReconciliation,
  })
})

describe('App task workspace', () => {
  it('从侧栏进入独立的任务历史与集中对账工作台', async () => {
    const wrapper = mountApp()
    const centerNav = wrapper.findAll('nav button').find(
      (button) => button.text().includes('历史与对账'),
    )

    expect(centerNav).toBeDefined()
    await centerNav?.trigger('click')

    expect(wrapper.find('[data-testid="reconciliation-center-stub"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('集中核对结果不确定的工具调用')
    expect(wrapper.text()).toContain('DURABLE EXECUTION LEDGER')
  })

  it('创建补偿后切换到新任务并重建事件与审批上下文', async () => {
    reconciliation.value = {} as Reconciliation
    const wrapper = mount(App, {
      global: {
        stubs: {
          ProviderSettings: { template: '<div />' },
          TaskEventItem: { template: '<li />' },
          ReconciliationEvidenceCard: {
            emits: ['compensate'],
            template: '<button data-testid="emit-compensation" @click="$emit(\'compensate\')">compensate</button>',
          },
        },
      },
    })
    const original = makeTask('failed')
    const compensation = makeTask('created', { task_id: 'task-compensation' })
    await createVisibleTask(wrapper, original)
    createCompensation.mockResolvedValueOnce(compensation)

    await wrapper.get('[data-testid="emit-compensation"]').trigger('click')
    await flushPromises()

    expect(createCompensation).toHaveBeenCalledTimes(1)
    expect(capturedTask.value).toEqual(compensation)
    expect(resetEvents).toHaveBeenCalledTimes(2)
    expect(resetControl).toHaveBeenCalledTimes(2)
    expect(resetApproval).toHaveBeenCalledTimes(2)
    expect(resetReconciliation).toHaveBeenCalledTimes(2)
    expect(connect).toHaveBeenLastCalledWith('task-compensation')
  })

  it('shows pause and cancel for a running task and forwards control actions', async () => {
    const wrapper = mountApp()
    await createVisibleTask(wrapper, makeTask('running'))

    expect(wrapper.get('[data-testid="pause-task"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="cancel-task"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="resume-task"]').exists()).toBe(false)

    await wrapper.get('[data-testid="pause-task"]').trigger('click')
    expect(runControl).toHaveBeenCalledWith('pause')

    await wrapper.get('[data-testid="cancel-task"]').trigger('click')
    await wrapper.get('[data-testid="confirm-cancel"]').trigger('click')
    expect(runControl).toHaveBeenCalledWith('cancel')
  })

  it('shows resume and cancel for a paused task', async () => {
    const wrapper = mountApp()
    await createVisibleTask(wrapper, makeTask('paused'))

    expect(wrapper.get('[data-testid="resume-task"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="cancel-task"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="pause-task"]').exists()).toBe(false)

    await wrapper.get('[data-testid="resume-task"]').trigger('click')
    expect(runControl).toHaveBeenCalledWith('resume')
  })

  it('等待审批时展示精确审批卡和取消，并转发用户决定', async () => {
    approval.value = makeApproval()
    const wrapper = mountApp()
    await createVisibleTask(wrapper, makeTask('waiting_approval'))

    expect(wrapper.get('.status-pill').text()).toBe('等待审批')
    expect(wrapper.text()).toContain('请确认磁盘信息读取')
    expect(wrapper.find('[data-testid="pause-task"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="resume-task"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="cancel-task"]').exists()).toBe(true)

    await wrapper.get('[data-testid="approve-approval"]').trigger('click')
    expect(runApproval).toHaveBeenCalledWith('approve', undefined)
  })

  it('审批和任务命令共享交互锁，避免同页并发', async () => {
    approval.value = makeApproval()
    const wrapper = mountApp()
    await createVisibleTask(wrapper, makeTask('waiting_approval'))

    approvalAction.value = 'approve'
    await nextTick()
    expect(wrapper.get('[data-testid="cancel-task"]').attributes('disabled')).toBeDefined()

    approvalAction.value = null
    activeAction.value = 'cancel'
    await nextTick()
    expect(wrapper.get('[data-testid="approve-approval"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="reject-approval"]').attributes('disabled')).toBeDefined()
  })

  it('removes task commands for terminal states', async () => {
    const wrapper = mountApp()
    await createVisibleTask(wrapper, makeTask('succeeded'))

    expect(wrapper.find('[data-testid="pause-task"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="resume-task"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="cancel-task"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('任务已结束，无需继续操作。')
  })

  it('disables every available command while a control request is pending', async () => {
    const wrapper = mountApp()
    await createVisibleTask(wrapper, makeTask('running'))

    activeAction.value = 'pause'
    await nextTick()

    expect(wrapper.get('[data-testid="pause-task"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="cancel-task"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('正在暂停…')
  })

  it('prefers a newer task snapshot over stale websocket status, then accepts a newer cancelled event', async () => {
    const wrapper = mountApp()
    await createVisibleTask(wrapper, makeTask('running', { last_event_seq: 5 }))

    events.value = [makeEvent(4, 'task.cancelled')]
    await nextTick()

    expect(wrapper.get('.status-pill').text()).toBe('执行中')
    expect(wrapper.find('[data-testid="pause-task"]').exists()).toBe(true)

    events.value = [
      makeEvent(4, 'task.status_changed', { to: 'running' }),
      makeEvent(6, 'task.cancelled'),
    ]
    await nextTick()

    expect(wrapper.get('.status-pill').text()).toBe('已取消')
    expect(wrapper.find('[data-testid="pause-task"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="cancel-task"]').exists()).toBe(false)
  })

  it('resets and connects only after creation succeeds, and blocks a second active task', async () => {
    let resolveCreation: ((task: Task) => void) | undefined
    apiMocks.createTask.mockImplementationOnce(
      () => new Promise<Task>((resolve) => {
        resolveCreation = resolve
      }),
    )
    const wrapper = mountApp()

    await wrapper.get('form').trigger('submit')
    await nextTick()

    expect(apiMocks.createTask).toHaveBeenCalledTimes(1)
    expect(resetEvents).not.toHaveBeenCalled()
    expect(resetControl).not.toHaveBeenCalled()
    expect(resetApproval).not.toHaveBeenCalled()
    expect(resetReconciliation).not.toHaveBeenCalled()
    expect(connect).not.toHaveBeenCalled()
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()

    resolveCreation?.(makeTask('running'))
    await flushPromises()

    expect(resetEvents).toHaveBeenCalledTimes(1)
    expect(resetControl).toHaveBeenCalledTimes(1)
    expect(resetApproval).toHaveBeenCalledTimes(1)
    expect(resetReconciliation).toHaveBeenCalledTimes(1)
    expect(connect).toHaveBeenCalledWith('task-1')
    expect(wrapper.get('button[type="submit"]').text()).toBe('当前任务进行中')

    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(apiMocks.createTask).toHaveBeenCalledTimes(1)
  })

  it('submits an explicit single-file move only after both paths are provided', async () => {
    const wrapper = mountApp()
    await wrapper.get('[data-testid="task-kind"]').setValue('file_move')

    expect(wrapper.find('[data-testid="source-path"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="destination-path"]').exists()).toBe(true)
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="source-path"]').setValue('D:\\input\\draft.txt')
    await wrapper
      .get('[data-testid="destination-path"]')
      .setValue('D:\\archive\\draft.txt')
    apiMocks.createTask.mockResolvedValueOnce(
      makeTask('created', { constraints: ['single_file', 'no_overwrite', 'no_cloud'] }),
    )
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(apiMocks.createTask).toHaveBeenCalledWith({
      goal: '验证 DeskPilot 前后端任务事件闭环',
      privacy_mode: 'local_only',
      constraints: ['single_file', 'no_overwrite', 'no_cloud'],
      tool_request: {
        kind: 'file_move',
        source: 'D:\\input\\draft.txt',
        destination: 'D:\\archive\\draft.txt',
      },
    })
  })

  it('shows reconnect details, retries immediately, and then reports recovery', async () => {
    const wrapper = mountApp()

    streamError.value = '任务事件连接暂时不可用，正在尝试恢复。'
    connectionState.value = 'reconnecting'
    reconnectAttempt.value = 2
    retryDelayMs.value = 4_000
    await nextTick()

    expect(wrapper.get('.stream-notice').attributes('data-tone')).toBe('warning')
    expect(wrapper.text()).toContain('第 2 次恢复 · 最长 4 秒后重试')
    await wrapper.get('.stream-notice button').trigger('click')
    expect(reconnectNow).toHaveBeenCalledTimes(1)

    streamError.value = null
    connectionState.value = 'connected'
    recoveryMessage.value = '事件流已恢复，正在从 #5 后继续接收。'
    await nextTick()

    expect(wrapper.get('.stream-notice').attributes('data-tone')).toBe('success')
    expect(wrapper.text()).toContain('连接已恢复')
    expect(wrapper.text()).toContain('事件流已恢复，正在从 #5 后继续接收。')
    expect(wrapper.find('.stream-notice button').exists()).toBe(false)
  })
})
