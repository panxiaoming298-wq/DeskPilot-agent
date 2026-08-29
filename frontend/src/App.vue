<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { createTask, listTasks } from './api'
import ApprovalCard from './components/ApprovalCard.vue'
import EffectRuntimeOperations from './components/EffectRuntimeOperations.vue'
import KnowledgeBase from './components/KnowledgeBase.vue'
import MemoryControlCenter from './components/MemoryControlCenter.vue'
import McpConnections from './components/McpConnections.vue'
import EvaluationLab from './components/EvaluationLab.vue'
import ProviderSettings from './components/ProviderSettings.vue'
import ResearchArtifactWorkbench from './components/ResearchArtifactWorkbench.vue'
import ReconciliationEvidenceCard from './components/ReconciliationEvidenceCard.vue'
import ReconciliationCenter from './components/ReconciliationCenter.vue'
import TaskControls from './components/TaskControls.vue'
import TaskEventItem from './components/TaskEventItem.vue'
import { useTaskRuntimeCollection } from './composables/useTaskRuntimeCollection'
import { syncDesktopActiveTaskCount } from './desktopRuntime'
import { deriveTaskStatus } from './taskState'
import type {
  ApprovalAction,
  PlanStep,
  Task,
  TaskControlAction,
  TaskCreate,
  TaskEvent,
  TaskStatus,
} from './types'

const goal = ref('验证 DeskPilot 前后端任务事件闭环')
const taskKind = ref<'disk_usage' | 'file_move' | 'disk_pressure_guarded_file_move'>('disk_usage')
const sourcePath = ref('')
const destinationPath = ref('')
const maximumUsedPercent = ref(80)
type ActiveView = 'tasks' | 'research' | 'memory' | 'knowledge' | 'mcp' | 'evaluations' | 'reconciliations' | 'providers' | 'operations'
const activeView = ref<ActiveView>('research')
const privacyMode = ref<TaskCreate['privacy_mode']>('local_only')
const submitting = ref(false)
const submitError = ref<string | null>(null)
const {
  capacity: taskCapacity,
  selectedRuntime,
  taskCards,
  activeTaskCount,
  hasTaskCapacity,
  selectTask,
  trackTask,
} = useTaskRuntimeCollection()

const recoverableTaskStatuses = new Set<TaskStatus>([
  'created',
  'classifying',
  'running',
  'waiting_approval',
  'waiting_reconciliation',
  'paused',
])

async function restoreActiveTasks(): Promise<void> {
  try {
    const history = await listTasks(undefined, 100, 0)
    const recoverable = history.items
      .filter((snapshot) => recoverableTaskStatuses.has(snapshot.status))
      .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
      .slice(0, taskCapacity)
      .reverse()
    for (const snapshot of recoverable) trackTask(snapshot)
  } catch (error) {
    submitError.value = error instanceof Error
      ? `未能恢复未完成任务：${error.message}`
      : '未能恢复未完成任务。'
  }
}

onMounted(() => {
  void restoreActiveTasks()
})

watch(
  activeTaskCount,
  (count) => {
    void syncDesktopActiveTaskCount(count)
  },
  { immediate: true },
)

const task = computed<Task | null>(() => selectedRuntime.value?.task.value ?? null)
const events = computed<TaskEvent[]>(() => (
  selectedRuntime.value?.eventsRuntime.events.value ?? []
))
const connected = computed(() => selectedRuntime.value?.eventsRuntime.connected.value ?? false)
const streamError = computed(() => (
  selectedRuntime.value?.eventsRuntime.streamError.value ?? null
))
const connectionState = computed(() => (
  selectedRuntime.value?.eventsRuntime.connectionState.value ?? 'idle'
))
const recoveryMessage = computed(() => (
  selectedRuntime.value?.eventsRuntime.recoveryMessage.value ?? null
))
const reconnectAttempt = computed(() => (
  selectedRuntime.value?.eventsRuntime.reconnectAttempt.value ?? 0
))
const retryDelayMs = computed(() => (
  selectedRuntime.value?.eventsRuntime.retryDelayMs.value ?? 0
))
const activeAction = computed(() => (
  selectedRuntime.value?.controlRuntime.activeAction.value ?? null
))
const controlMessage = computed(() => (
  selectedRuntime.value?.controlRuntime.controlMessage.value ?? null
))
const controlError = computed(() => (
  selectedRuntime.value?.controlRuntime.controlError.value ?? null
))
const approval = computed(() => (
  selectedRuntime.value?.approvalRuntime.approval.value ?? null
))
const approvalLoading = computed(() => (
  selectedRuntime.value?.approvalRuntime.loading.value ?? false
))
const approvalAction = computed(() => (
  selectedRuntime.value?.approvalRuntime.activeAction.value ?? null
))
const approvalMessage = computed(() => (
  selectedRuntime.value?.approvalRuntime.approvalMessage.value ?? null
))
const approvalError = computed(() => (
  selectedRuntime.value?.approvalRuntime.approvalError.value ?? null
))
const reconciliation = computed(() => (
  selectedRuntime.value?.reconciliationRuntime.reconciliation.value ?? null
))
const reconciliationLoading = computed(() => (
  selectedRuntime.value?.reconciliationRuntime.loading.value ?? false
))
const reconciliationRefreshing = computed(() => (
  selectedRuntime.value?.reconciliationRuntime.refreshing.value ?? false
))
const reconciliationCompensating = computed(() => (
  selectedRuntime.value?.reconciliationRuntime.compensating.value ?? false
))
const reconciliationMessage = computed(() => (
  selectedRuntime.value?.reconciliationRuntime.message.value ?? null
))
const reconciliationError = computed(() => (
  selectedRuntime.value?.reconciliationRuntime.error.value ?? null
))

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

const branchDecision = computed(() => {
  const event = [...events.value].reverse().find((item) => item.type === 'effect.branch.decided')
  if (!event || event.payload.decision_key !== 'disk_pressure_route') return null
  const outcome = event.payload.outcome
  if (outcome !== 'move' && outcome !== 'defer') return null
  return {
    outcome,
    label: outcome === 'move' ? '压力允许，进入移动审批' : '压力过高，已安全推迟写入',
  }
})

const taskStatusLabels: Record<TaskStatus, string> = {
  created: '已创建',
  classifying: '正在分类',
  running: '执行中',
  waiting_approval: '等待审批',
  waiting_reconciliation: '等待对账恢复',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  paused: '已暂停',
}
const statusLabel = computed(() => status.value ? taskStatusLabels[status.value] : '等待任务')

function formatTokenUsage(tokens: number): string {
  if (tokens < 1_000) return `${tokens} token`
  return `${(tokens / 1_000).toFixed(tokens < 10_000 ? 1 : 0)}k token`
}

const terminal = computed(() =>
  status.value === 'succeeded' || status.value === 'failed' || status.value === 'cancelled',
)
const fileMoveInputsValid = computed(() => {
  const source = sourcePath.value.trim()
  const destination = destinationPath.value.trim()
  return source.length > 0 && destination.length > 0 && source !== destination
})
const diskPressureThresholdValid = computed(() =>
  Number.isFinite(maximumUsedPercent.value) &&
  maximumUsedPercent.value >= 0 &&
  maximumUsedPercent.value <= 100,
)
const canSubmit = computed(() =>
  goal.value.trim().length > 0 &&
  !submitting.value &&
  hasTaskCapacity.value &&
  (
    taskKind.value === 'disk_usage' ||
    (
      fileMoveInputsValid.value &&
      (
        taskKind.value !== 'disk_pressure_guarded_file_move' ||
        diskPressureThresholdValid.value
      )
    )
  ),
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

function reconnectNow(): void {
  selectedRuntime.value?.eventsRuntime.reconnectNow()
}

function switchView(nextView: ActiveView): void {
  if (activeView.value === nextView) return
  activeView.value = nextView
}

const pageHeading = computed(() =>
  activeView.value === 'tasks'
    ? '执行详情'
    : activeView.value === 'research'
      ? 'Agent 会话'
    : activeView.value === 'memory'
      ? '长期记忆'
    : activeView.value === 'knowledge'
      ? '知识库'
      : activeView.value === 'mcp'
        ? 'Agent 与 MCP'
        : activeView.value === 'evaluations'
          ? '评测与 Trace'
    : activeView.value === 'reconciliations'
      ? '历史与对账'
      : activeView.value === 'providers'
        ? '模型与设置'
        : '运行时运维',
)

const pageEyebrow = computed(() => ({
  tasks: '任务运行与审批',
  research: '本地优先的持续对话',
  memory: '来源可追溯的上下文',
  knowledge: '本地检索与引用',
  mcp: '受控工具连接',
  evaluations: '版本化验收证据',
  reconciliations: '运行历史与恢复',
  providers: 'Provider 与安全凭据',
  operations: '受保护的运行时控制',
})[activeView.value])

const stageTitle = computed(() => ({
  tasks: '可控执行',
  research: '通用 Agent',
  memory: '长期记忆',
  knowledge: '本地知识',
  mcp: '受控 MCP',
  evaluations: '离线评测',
  reconciliations: '持久化对账',
  providers: '模型控制面',
  operations: '受保护运维',
})[activeView.value])

const stageDescription = computed(() => ({
  tasks: '通过检查点、实时事件和控制命令验证执行闭环。',
  research: '一个对话入口路由公开研究、本地知识与受控 MCP；不确定时先追问。',
  memory: '确认提案、处理冲突，并核对每次真实 Context 使用记录。',
  knowledge: '导入只读文本来源，以内容寻址分块和来源版本证明检索结果。',
  mcp: '固定 Server 命令、能力和 Schema，默认禁用并记录脱敏审计。',
  evaluations: '运行版本化离线黄金任务，记录内容寻址 trace 并验证语义 replay。',
  reconciliations: '查看任务历史、Runner 证据、裁决与后继血缘。',
  providers: '管理可切换的模型连接、健康状态与配置审计。',
  operations: '读取四域数据库真值，审计采样、retention 与 DLQ requeue。',
})[activeView.value])

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
        : taskKind.value === 'disk_pressure_guarded_file_move'
          ? [
              'trusted_conditional_graph',
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
        : taskKind.value === 'disk_pressure_guarded_file_move'
          ? {
              kind: 'disk_pressure_guarded_file_move',
              source: sourcePath.value.trim(),
              destination: destinationPath.value.trim(),
              maximum_used_percent: maximumUsedPercent.value,
            }
        : undefined,
    })
    if (!trackTask(createdTask)) {
      submitError.value = '三个活动任务槽位已占用；请等待任一任务结束后再创建。'
    }
  } catch (error) {
    submitError.value = error instanceof Error ? error.message : '创建任务失败'
  } finally {
    submitting.value = false
  }
}

function handleTaskControl(action: TaskControlAction): void {
  void selectedRuntime.value?.controlRuntime.runControl(action)
}

function handleApproval(action: ApprovalAction, reason?: string): void {
  void selectedRuntime.value?.approvalRuntime.runApproval(action, reason)
}

function handleEvidenceRefresh(): void {
  void selectedRuntime.value?.reconciliationRuntime.refreshEvidence()
}

async function handleCompensation(): Promise<void> {
  const compensationTask = await selectedRuntime.value?.reconciliationRuntime.createCompensation()
  if (!compensationTask) return
  if (!trackTask(compensationTask)) {
    submitError.value = '三个活动任务槽位已占用，无法打开补偿任务。'
  }
}

function handleOpenHistoricalTask(snapshot: Task): void {
  if (!trackTask(snapshot)) return
  activeView.value = 'tasks'
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
        <span class="nav-group-label">工作区</span>
        <button class="nav-item" :class="{ active: activeView === 'research' }" type="button" @click="switchView('research')">
          <span>Agent 会话</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'tasks' }" type="button" @click="switchView('tasks')">
          <span>执行详情</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'knowledge' }" type="button" @click="switchView('knowledge')">
          <span>知识库</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'memory' }" type="button" @click="switchView('memory')">
          <span>长期记忆</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'mcp' }" type="button" @click="switchView('mcp')">
          <span>Agent 与 MCP</span>
        </button>
        <span class="nav-group-label">系统</span>
        <button class="nav-item" :class="{ active: activeView === 'evaluations' }" type="button" @click="switchView('evaluations')">
          <span>评测与 Trace</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'reconciliations' }" type="button" @click="switchView('reconciliations')">
          <span>历史与对账</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'providers' }" type="button" @click="switchView('providers')">
          <span>模型与设置</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'operations' }" type="button" @click="switchView('operations')">
          <span>运行时运维</span>
        </button>
      </nav>

      <div class="stage-card">
        <span class="stage-label">当前模块</span>
        <strong>{{ stageTitle }}</strong>
        <p>{{ stageDescription }}</p>
      </div>
    </aside>

    <section class="main-panel">
      <header class="topbar">
        <div>
          <span class="eyebrow">{{ pageEyebrow }}</span>
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
      <section
        v-if="taskCards.length"
        class="task-runtime-deck motion-section"
        data-testid="task-runtime-deck"
        aria-label="后台任务"
      >
        <header class="task-runtime-header">
          <div>
            <h2>后台任务</h2>
            <p>切换焦点不会停止事件流；每个任务保留自己的 cursor、审批和控制状态。</p>
          </div>
          <span>{{ activeTaskCount }} / {{ taskCapacity }} 运行中</span>
        </header>
        <div class="task-runtime-list" role="group" aria-label="选择任务">
          <button
            v-for="card in taskCards"
            :key="card.task.task_id"
            class="task-runtime-card"
            :class="{ selected: card.selected, attention: card.pending_approval || card.pending_input || card.pending_reconciliation }"
            :data-status="card.status"
            :data-testid="`task-runtime-${card.slot_id}`"
            type="button"
            :aria-pressed="card.selected"
            @click="selectTask(card.task.task_id)"
          >
            <span class="task-runtime-card-top">
              <span class="task-runtime-state"><i />{{ taskStatusLabels[card.status] }}</span>
              <span class="task-runtime-slot">SLOT {{ card.slot_id }}</span>
            </span>
            <strong>{{ card.task.goal }}</strong>
            <span class="task-runtime-meta">
              <span>#{{ card.event_cursor }}</span>
              <span v-if="card.token_usage > 0">{{ formatTokenUsage(card.token_usage) }}</span>
              <span>{{ card.connected ? 'LIVE' : card.connection_state.toUpperCase() }}</span>
            </span>
            <span v-if="card.pending_approval || card.pending_input || card.pending_reconciliation || card.unread_count" class="task-runtime-attention">
              <span v-if="card.pending_approval">待审批</span>
              <span v-if="card.pending_input">待输入</span>
              <span v-if="card.pending_reconciliation">待对账</span>
              <span v-if="card.unread_count">{{ card.unread_count }} 条未读</span>
            </span>
          </button>
        </div>
      </section>
      <section class="composer motion-section" aria-labelledby="task-heading">
        <div class="composer-copy">
          <span class="eyebrow">NEW TASK</span>
          <h2 id="task-heading">创建可并行的任务闭环</h2>
          <p v-if="taskKind === 'disk_usage'">默认使用离线 Fake 模型与只读工具，不会联网或修改电脑。</p>
          <p v-else-if="taskKind === 'file_move'">文件移动使用固定应用计划；只有审批卡中的单个源和目标会进入受控提交。</p>
          <p v-else>先读取目标磁盘使用率，由受信条件证明决定进入移动审批或安全推迟分支。</p>
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
              <option value="disk_pressure_guarded_file_move">磁盘压力保护移动（条件图）</option>
            </select>
          </label>
          <div v-if="taskKind !== 'disk_usage'" class="file-move-fields">
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
            <label
              v-if="taskKind === 'disk_pressure_guarded_file_move'"
              class="path-field"
              for="maximum-used-percent"
            >
              <span>允许移动的最高磁盘使用率（%）</span>
              <input
                id="maximum-used-percent"
                v-model.number="maximumUsedPercent"
                data-testid="maximum-used-percent"
                type="number"
                min="0"
                max="100"
                step="0.01"
              />
            </label>
            <p class="file-move-notice">
              {{ taskKind === 'disk_pressure_guarded_file_move'
                ? '使用已持久化的磁盘 Tool 结果计算分支；只有 move 分支会创建一次性写审批。'
                : '仅支持同一磁盘内的普通单文件移动，不覆盖目标；提交前会再次验证源文件版本。' }}
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
              {{ submitting ? '创建中…' : !hasTaskCapacity ? '三个任务槽位均已占用' : '开始任务' }}
            </button>
          </div>
          <p v-if="submitError" class="form-error" role="alert">{{ submitError }}</p>
        </form>
      </section>

      <section class="task-grid motion-section">
        <article class="task-overview">
          <div class="section-heading">
            <div>
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
            <div
              v-if="branchDecision"
              class="stream-notice"
              :data-tone="branchDecision.outcome === 'move' ? 'success' : 'warning'"
              data-testid="branch-decision-summary"
            >
              <div>
                <strong>受信条件分支</strong>
                <p>{{ branchDecision.label }}</p>
              </div>
            </div>
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
          <span class="timeline-sweep" aria-hidden="true" />
          <div class="section-heading">
            <div>
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
      <ResearchArtifactWorkbench v-else-if="activeView === 'research'" />
      <ReconciliationCenter
        v-else-if="activeView === 'reconciliations'"
        :active-task-id="task?.task_id ?? null"
        :task-switch-locked="!hasTaskCapacity"
        @open-task="handleOpenHistoricalTask"
      />
      <KnowledgeBase v-else-if="activeView === 'knowledge'" />
      <MemoryControlCenter v-else-if="activeView === 'memory'" />
      <McpConnections v-else-if="activeView === 'mcp'" />
      <EvaluationLab v-else-if="activeView === 'evaluations'" />
      <ProviderSettings v-else-if="activeView === 'providers'" />
      <EffectRuntimeOperations v-else />
    </section>
  </main>
</template>
