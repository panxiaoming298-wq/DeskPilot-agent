<script setup lang="ts">
import { computed, ref } from 'vue'
import { createTask } from './api'
import ApprovalCard from './components/ApprovalCard.vue'
import ProviderSettings from './components/ProviderSettings.vue'
import ReconciliationEvidenceCard from './components/ReconciliationEvidenceCard.vue'
import ReconciliationCenter from './components/ReconciliationCenter.vue'
import TaskControls from './components/TaskControls.vue'
import TaskEventItem from './components/TaskEventItem.vue'
import { useTaskControl } from './composables/useTaskControl'
import { useTaskApproval } from './composables/useTaskApproval'
import { useTaskEvents } from './composables/useTaskEvents'
import { useTaskReconciliation } from './composables/useTaskReconciliation'
import { deriveTaskStatus } from './taskState'
import type {
  ApprovalAction,
  PlanStep,
  Task,
  TaskControlAction,
  TaskCreate,
  TaskStatus,
} from './types'

const goal = ref('验证 DeskPilot 前后端任务事件闭环')
const taskKind = ref<'disk_usage' | 'file_move'>('disk_usage')
const sourcePath = ref('')
const destinationPath = ref('')
const activeView = ref<'tasks' | 'reconciliations' | 'providers'>('tasks')
const privacyMode = ref<TaskCreate['privacy_mode']>('local_only')
const submitting = ref(false)
const submitError = ref<string | null>(null)
const task = ref<Task | null>(null)

const {
  events,
  connected,
  streamError,
  connectionState,
  recoveryMessage,
  reconnectAttempt,
  retryDelayMs,
  connect,
  reconnectNow,
  reset,
} = useTaskEvents()
const {
  activeAction,
  controlMessage,
  controlError,
  runControl,
  reset: resetControl,
} = useTaskControl(task)
const {
  approval,
  loading: approvalLoading,
  activeAction: approvalAction,
  approvalMessage,
  approvalError,
  runApproval,
  reset: resetApproval,
} = useTaskApproval(task, events)
const {
  reconciliation,
  loading: reconciliationLoading,
  refreshing: reconciliationRefreshing,
  compensating: reconciliationCompensating,
  message: reconciliationMessage,
  error: reconciliationError,
  refreshEvidence,
  createCompensation,
  reset: resetReconciliation,
} = useTaskReconciliation(task, events)

const status = computed<TaskStatus | null>(() => deriveTaskStatus(task.value, events.value))

const planSteps = computed<PlanStep[]>(() => {
  const planEvent = events.value.find((event) => event.type === 'plan.proposed')
  const steps = planEvent?.payload.steps
  if (!Array.isArray(steps)) return []

  return steps.filter((step): step is PlanStep => {
    if (!step || typeof step !== 'object') return false
    const candidate = step as Record<string, unknown>
    return (
      typeof candidate.step_id === 'string' &&
      typeof candidate.agent === 'string' &&
      typeof candidate.title === 'string'
    )
  })
})

const statusLabel = computed(() => {
  const labels: Partial<Record<TaskStatus, string>> = {
    created: '已创建',
    classifying: '正在分类',
    running: '执行中',
    waiting_approval: '等待审批',
    succeeded: '已完成',
    failed: '失败',
    cancelled: '已取消',
    paused: '已暂停',
  }
  return status.value ? labels[status.value] : '等待任务'
})

const terminal = computed(() =>
  status.value === 'succeeded' || status.value === 'failed' || status.value === 'cancelled',
)
const taskInProgress = computed(() => task.value !== null && !terminal.value)
const fileMoveInputsValid = computed(() => {
  const source = sourcePath.value.trim()
  const destination = destinationPath.value.trim()
  return source.length > 0 && destination.length > 0 && source !== destination
})
const canSubmit = computed(() =>
  goal.value.trim().length > 0 &&
  !submitting.value &&
  !taskInProgress.value &&
  (taskKind.value === 'disk_usage' || fileMoveInputsValid.value),
)

const connectionLabel = computed(() => {
  if (terminal.value) return '事件流已归档'
  const labels = {
    idle: '事件流未连接',
    connecting: '事件流连接中',
    connected: '事件流已连接',
    reconnecting: '事件流恢复中',
    reauthenticating: '本地会话重认证中',
    forbidden: '事件流未获授权',
    not_found: '任务事件流不存在',
    archived: '事件流已归档',
  }
  return labels[connectionState.value]
})

const canReconnect = computed(() =>
  connectionState.value === 'reconnecting' || connectionState.value === 'reauthenticating',
)

const reconnectDetail = computed(() => {
  if (!canReconnect.value) return null
  const seconds = Math.max(1, Math.ceil(retryDelayMs.value / 1_000))
  return `第 ${reconnectAttempt.value} 次恢复 · 最长 ${seconds} 秒后重试`
})

const pageHeading = computed(() =>
  activeView.value === 'tasks'
    ? '让任务过程成为可验证的数据'
    : activeView.value === 'reconciliations'
      ? '集中核对结果不确定的工具调用'
      : '在本地安全切换云端与本地模型',
)

async function submitTask() {
  const normalizedGoal = goal.value.trim()
  if (!canSubmit.value) return

  submitting.value = true
  submitError.value = null

  try {
    const createdTask = await createTask({
      goal: normalizedGoal,
      privacy_mode: privacyMode.value,
      constraints: taskKind.value === 'file_move'
        ? [
            'single_file',
            'no_overwrite',
            ...(privacyMode.value === 'local_only' ? ['no_cloud'] : []),
          ]
        : privacyMode.value === 'local_only'
          ? ['read_only', 'no_cloud']
          : ['read_only'],
      tool_request: taskKind.value === 'file_move'
        ? {
            kind: 'file_move',
            source: sourcePath.value.trim(),
            destination: destinationPath.value.trim(),
          }
        : undefined,
    })
    reset()
    resetControl()
    resetApproval()
    resetReconciliation()
    task.value = createdTask
    connect(createdTask.task_id)
  } catch (error) {
    submitError.value = error instanceof Error ? error.message : '创建任务失败'
  } finally {
    submitting.value = false
  }
}

function handleTaskControl(action: TaskControlAction): void {
  void runControl(action)
}

function handleApproval(action: ApprovalAction, reason?: string): void {
  void runApproval(action, reason)
}

function handleEvidenceRefresh(): void {
  void refreshEvidence()
}

async function handleCompensation(): Promise<void> {
  const compensationTask = await createCompensation()
  if (!compensationTask) return
  reset()
  resetControl()
  resetApproval()
  resetReconciliation()
  task.value = compensationTask
  connect(compensationTask.task_id)
}

function handleOpenHistoricalTask(snapshot: Task): void {
  if (
    taskInProgress.value &&
    task.value?.task_id !== snapshot.task_id
  ) return
  reset()
  resetControl()
  resetApproval()
  resetReconciliation()
  task.value = snapshot
  activeView.value = 'tasks'
  connect(snapshot.task_id)
}
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark">DP</span>
        <div>
          <strong>DeskPilot</strong>
          <small>Local agent workspace</small>
        </div>
      </div>

      <nav aria-label="工作区导航">
        <button class="nav-item" :class="{ active: activeView === 'tasks' }" type="button" @click="activeView = 'tasks'">
          <span>任务工作台</span>
          <span class="nav-count">01</span>
        </button>
        <button class="nav-item" type="button" disabled>知识库 <small>待开发</small></button>
        <button class="nav-item" type="button" disabled>Agent 与工具 <small>待开发</small></button>
        <button class="nav-item" :class="{ active: activeView === 'reconciliations' }" type="button" @click="activeView = 'reconciliations'">
          <span>历史与对账</span>
          <span class="nav-count">02</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'providers' }" type="button" @click="activeView = 'providers'">
          <span>模型与设置</span>
          <span class="nav-count">03</span>
        </button>
      </nav>

      <div class="stage-card">
        <span class="eyebrow">CURRENT STAGE</span>
        <strong>{{ activeView === 'tasks' ? '阶段 2 · 可控任务' : activeView === 'reconciliations' ? '阶段 2 · 持久化对账' : '阶段 2 · 模型控制面' }}</strong>
        <p>{{ activeView === 'tasks' ? '通过检查点、实时事件和控制命令验证执行闭环。' : activeView === 'reconciliations' ? '查看任务历史、Runner 证据、裁决与后继血缘。' : '管理可切换的模型连接、健康状态与配置审计。' }}</p>
      </div>
    </aside>

    <section class="main-panel">
      <header class="topbar">
        <div>
          <span class="eyebrow">{{ activeView === 'tasks' ? 'WINDOWS MULTI-AGENT SYSTEM' : activeView === 'reconciliations' ? 'DURABLE EXECUTION LEDGER' : 'OPENAI-COMPATIBLE GATEWAY' }}</span>
          <h1>{{ pageHeading }}</h1>
        </div>
        <div
          v-if="activeView === 'tasks'"
          class="connection"
          :class="{ online: connected || terminal }"
          :data-state="terminal ? 'archived' : connectionState"
        >
          <span />
          {{ connectionLabel }}
        </div>
        <div v-else class="connection online"><span />安全本地会话</div>
      </header>

      <template v-if="activeView === 'tasks'">
      <section class="composer" aria-labelledby="task-heading">
        <div class="composer-copy">
          <span class="eyebrow">NEW TASK</span>
          <h2 id="task-heading">创建可暂停的任务闭环</h2>
          <p v-if="taskKind === 'disk_usage'">默认使用离线 Fake 模型与只读工具，不会联网或修改电脑。</p>
          <p v-else>文件移动使用固定应用计划；只有审批卡中的单个源和目标会进入受控提交。</p>
        </div>
        <form @submit.prevent="submitTask">
          <label for="goal">任务目标</label>
          <textarea
            id="goal"
            v-model="goal"
            rows="3"
            maxlength="4000"
            placeholder="描述希望 DeskPilot 完成的目标"
          />
          <label class="select-field task-kind-field">
            <span>任务类型</span>
            <select v-model="taskKind" data-testid="task-kind">
              <option value="disk_usage">读取磁盘容量（只读）</option>
              <option value="file_move">移动单个文件（需要审批）</option>
            </select>
          </label>
          <div v-if="taskKind === 'file_move'" class="file-move-fields">
            <label class="path-field" for="source-path">
              <span>源文件</span>
              <input
                id="source-path"
                v-model="sourcePath"
                data-testid="source-path"
                maxlength="32767"
                autocomplete="off"
                placeholder="例如 D:\\Documents\\draft.txt"
              />
            </label>
            <label class="path-field" for="destination-path">
              <span>目标路径（必须尚不存在）</span>
              <input
                id="destination-path"
                v-model="destinationPath"
                data-testid="destination-path"
                maxlength="32767"
                autocomplete="off"
                placeholder="例如 D:\\Documents\\archive\\draft.txt"
              />
            </label>
            <p class="file-move-notice">
              仅支持同一磁盘内的普通单文件移动，不覆盖目标；提交前会再次验证源文件版本。
            </p>
          </div>
          <div class="composer-actions">
            <label class="select-field">
              <span>隐私模式</span>
              <select v-model="privacyMode">
                <option value="local_only">仅本地</option>
                <option value="local_preferred">本地优先</option>
                <option value="balanced">平衡模式</option>
                <option value="quality_first">质量优先</option>
              </select>
            </label>
            <button
              class="primary-button"
              type="submit"
              :disabled="!canSubmit"
            >
              {{ submitting ? '创建中…' : taskInProgress ? '当前任务进行中' : '开始任务' }}
            </button>
          </div>
          <p v-if="submitError" class="form-error" role="alert">{{ submitError }}</p>
        </form>
      </section>

      <section class="task-grid">
        <article class="task-overview">
          <div class="section-heading">
            <div>
              <span class="eyebrow">TASK SNAPSHOT</span>
              <h2>任务状态</h2>
            </div>
            <span class="status-pill" :data-status="status">{{ statusLabel }}</span>
          </div>

          <template v-if="task">
            <p class="task-goal">{{ task.goal }}</p>
            <dl class="task-meta">
              <div><dt>任务 ID</dt><dd>{{ task.task_id }}</dd></div>
              <div><dt>运行模式</dt><dd>{{ task.mode }}</dd></div>
              <div><dt>隐私模式</dt><dd>{{ task.privacy_mode }}</dd></div>
              <div><dt>事件数量</dt><dd>{{ events.length }}</dd></div>
            </dl>
            <div v-if="status" class="task-controls-slot">
              <TaskControls
                :status="status"
                :active-action="activeAction"
                :message="controlMessage"
                :error="controlError"
                :disabled="approvalAction !== null"
                @control="handleTaskControl"
              />
            </div>

            <ApprovalCard
              v-if="approval || approvalLoading || approvalError"
              :approval="approval"
              :loading="approvalLoading"
              :active-action="approvalAction"
              :message="approvalMessage"
              :error="approvalError"
              :disabled="activeAction !== null || terminal"
              @resolve="handleApproval"
            />

            <ReconciliationEvidenceCard
              v-if="reconciliation || reconciliationLoading || reconciliationError"
              :reconciliation="reconciliation"
              :loading="reconciliationLoading"
              :refreshing="reconciliationRefreshing"
              :compensating="reconciliationCompensating"
              :message="reconciliationMessage"
              :error="reconciliationError"
              @refresh="handleEvidenceRefresh"
              @compensate="handleCompensation"
            />
          </template>
          <div v-else class="empty-state">提交上方任务后，这里会显示持久化快照。</div>

          <div class="plan-block">
            <div class="plan-title">
              <h3>结构化计划</h3>
              <span>{{ planSteps.length }} steps</span>
            </div>
            <ol v-if="planSteps.length" class="plan-list">
              <li v-for="step in planSteps" :key="step.step_id">
                <span>{{ step.step_id }}</span>
                <div><strong>{{ step.title }}</strong><small>{{ step.agent }}</small></div>
              </li>
            </ol>
            <p v-else class="muted">计划事件到达后在这里展示。</p>
          </div>
        </article>

        <article class="timeline-panel">
          <div class="section-heading">
            <div>
              <span class="eyebrow">LIVE EVENT STREAM</span>
              <h2>执行时间线</h2>
            </div>
            <span class="event-total">{{ events.length }} events</span>
          </div>
          <div
            v-if="streamError || recoveryMessage"
            class="stream-notice"
            :data-tone="streamError ? 'warning' : 'success'"
            role="status"
          >
            <div>
              <strong>{{ streamError ? '连接提示' : '连接已恢复' }}</strong>
              <p>{{ streamError ?? recoveryMessage }}</p>
              <small v-if="reconnectDetail">{{ reconnectDetail }}</small>
            </div>
            <button v-if="canReconnect" class="inline-button" type="button" @click="reconnectNow">
              立即重连
            </button>
          </div>
          <ol v-if="events.length" class="event-list">
            <TaskEventItem v-for="event in events" :key="event.event_id" :event="event" />
          </ol>
          <div v-else class="empty-state timeline-empty">
            事件将按数据库序号实时到达；断线后会从最后序号补拉。
          </div>
        </article>
      </section>
      </template>
      <ReconciliationCenter
        v-else-if="activeView === 'reconciliations'"
        :active-task-id="task?.task_id ?? null"
        :task-switch-locked="taskInProgress"
        @open-task="handleOpenHistoricalTask"
      />
      <ProviderSettings v-else />
    </section>
  </main>
</template>
