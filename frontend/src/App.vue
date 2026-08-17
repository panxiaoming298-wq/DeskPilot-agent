<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import { createTask } from './api'
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

const canUseScrollTrigger = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
if (canUseScrollTrigger) gsap.registerPlugin(ScrollTrigger)

const coverPoster = `${import.meta.env.BASE_URL}media/deskpilot-ocean-poster.png`
const coverVideo = `${import.meta.env.BASE_URL}media/deskpilot-whale-shark.mp4`
const coverRef = ref<HTMLElement | null>(null)
const irisRef = ref<HTMLElement | null>(null)
const workspaceRef = ref<HTMLElement | null>(null)
const viewTransitionRef = ref<HTMLElement | null>(null)
const coverVisible = ref(true)
const transitioning = ref(false)
const viewTransitioning = ref(false)
let coverContext: ReturnType<typeof gsap.context> | null = null
let workspaceContext: ReturnType<typeof gsap.context> | null = null

function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}

function initWorkspaceMotion(): void {
  if (!workspaceRef.value || !canUseScrollTrigger || prefersReducedMotion()) return
  workspaceContext?.revert()
  workspaceContext = gsap.context(() => {
    gsap.timeline({ defaults: { ease: 'power4.out' } })
      .fromTo('.hud-reticle', { autoAlpha: 0, scale: 0.72, rotation: -18 }, {
        autoAlpha: 0.74,
        scale: 1,
        rotation: 0,
        duration: 1.1,
      })
      .fromTo('.sidebar', { autoAlpha: 0, x: -34 }, {
        autoAlpha: 1,
        x: 0,
        duration: 0.72,
      }, '-=0.9')
      .fromTo('.hud-telemetry span', { autoAlpha: 0, x: 18 }, {
        autoAlpha: 1,
        x: 0,
        duration: 0.42,
        stagger: 0.07,
      }, '-=0.58')
      .fromTo('.topbar', { autoAlpha: 0, y: -18 }, {
        autoAlpha: 1,
        y: 0,
        duration: 0.58,
      }, '-=0.5')

    gsap.fromTo('.composer', {
      autoAlpha: 0.28,
      y: 30,
      rotationX: -7,
      transformOrigin: '50% 0%',
    }, {
      autoAlpha: 1,
      y: 0,
      rotationX: 0,
      duration: 0.9,
      ease: 'power4.out',
      scrollTrigger: {
        trigger: '.composer',
        start: 'top 91%',
        once: true,
      },
    })

    gsap.timeline({
      defaults: { ease: 'none' },
      scrollTrigger: {
        trigger: '.task-grid',
        start: 'top 92%',
        end: 'top 42%',
        scrub: 0.7,
      },
    })
      .fromTo('.task-overview', { autoAlpha: 0.35, x: -44, rotationY: 3 }, {
        autoAlpha: 1,
        x: 0,
        rotationY: 0,
      }, 0)
      .fromTo('.timeline-panel', { autoAlpha: 0.35, x: 44, rotationY: -3 }, {
        autoAlpha: 1,
        x: 0,
        rotationY: 0,
      }, 0)
      .fromTo('.timeline-sweep', { yPercent: -120 }, { yPercent: 520 }, 0)
  }, workspaceRef.value)
  ScrollTrigger.refresh()
}

function finishCoverTransition(): void {
  coverVisible.value = false
  transitioning.value = false
  document.body.classList.remove('cover-open')
  window.scrollTo({ top: 0 })
  void nextTick(initWorkspaceMotion)
}

function enterWorkspace(): void {
  if (transitioning.value) return
  transitioning.value = true

  if (prefersReducedMotion() || !coverRef.value || !irisRef.value) {
    finishCoverTransition()
    return
  }

  gsap.timeline({ onComplete: finishCoverTransition })
    .to('.cover-copy [data-intro]', {
      autoAlpha: 0,
      scale: 0.985,
      duration: 0.28,
      stagger: 0.025,
      ease: 'power2.in',
    })
    .to(irisRef.value, {
      scale: 1,
      duration: 0.82,
      ease: 'power4.inOut',
    }, '<0.08')
}

onMounted(() => {
  document.body.classList.add('cover-open')
  if (!coverRef.value) return
  if (prefersReducedMotion()) {
    coverRef.value.querySelector('video')?.pause()
    return
  }

  coverContext = gsap.context(() => {
    gsap.timeline({ defaults: { ease: 'power3.out' } })
      .fromTo('.cover-brand', { autoAlpha: 0, y: -18 }, { autoAlpha: 1, y: 0, duration: 0.75 })
      .fromTo('.cover-title span', { autoAlpha: 0, yPercent: 34 }, {
        autoAlpha: 1,
        yPercent: 0,
        duration: 0.9,
        stagger: 0.11,
      }, '-=0.42')
      .fromTo('.cover-note, .cover-manifesto, .cover-enter', { autoAlpha: 0, y: 18 }, {
        autoAlpha: 1,
        y: 0,
        duration: 0.58,
        stagger: 0.08,
      }, '-=0.42')
  }, coverRef.value)
})

onUnmounted(() => {
  document.body.classList.remove('cover-open')
  coverContext?.revert()
  workspaceContext?.revert()
})

const goal = ref('验证 DeskPilot 前后端任务事件闭环')
const taskKind = ref<'disk_usage' | 'file_move' | 'disk_pressure_guarded_file_move'>('disk_usage')
const sourcePath = ref('')
const destinationPath = ref('')
const maximumUsedPercent = ref(80)
type ActiveView = 'tasks' | 'research' | 'memory' | 'knowledge' | 'mcp' | 'evaluations' | 'reconciliations' | 'providers' | 'operations'
const activeView = ref<ActiveView>('tasks')
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

const statusLabel = computed(() => {
  const labels: Partial<Record<TaskStatus, string>> = {
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
const diskPressureThresholdValid = computed(() =>
  Number.isFinite(maximumUsedPercent.value) &&
  maximumUsedPercent.value >= 0 &&
  maximumUsedPercent.value <= 100,
)
const canSubmit = computed(() =>
  goal.value.trim().length > 0 &&
  !submitting.value &&
  !taskInProgress.value &&
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

function switchView(nextView: ActiveView): void {
  if (activeView.value === nextView || viewTransitioning.value) return
  const overlay = viewTransitionRef.value
  activeView.value = nextView

  if (!overlay || prefersReducedMotion()) {
    void nextTick(() => ScrollTrigger.refresh())
    return
  }

  viewTransitioning.value = true
  gsap.set(overlay, { autoAlpha: 1, scale: 0 })
  void nextTick(() => {
    ScrollTrigger.refresh()
    gsap.timeline({
      onComplete: () => {
        gsap.set(overlay, { scale: 0 })
        viewTransitioning.value = false
      },
    })
      .to(overlay, {
        scale: 1,
        duration: 0.54,
        ease: 'power4.inOut',
      })
      .to(overlay, {
        autoAlpha: 0,
        scale: 1.06,
        duration: 0.34,
        ease: 'power3.out',
      })
  })
}

const pageHeading = computed(() =>
  activeView.value === 'tasks'
    ? '让任务过程成为可验证的数据'
    : activeView.value === 'research'
      ? '从 Claim 到 HTML，每一步都由证据解锁'
    : activeView.value === 'memory'
      ? '让每条长期记忆都有来源、版本与去向'
    : activeView.value === 'knowledge'
      ? '只返回来源仍然有效的本地知识'
      : activeView.value === 'mcp'
        ? '显式审阅并约束每个本地 MCP 连接'
        : activeView.value === 'evaluations'
          ? '用可重放黄金任务衡量真实安全行为'
    : activeView.value === 'reconciliations'
      ? '集中核对结果不确定的工具调用'
      : activeView.value === 'providers'
        ? '在本地安全切换云端与本地模型'
        : '用数据库真值观察并保护运行时',
)

const pageEyebrow = computed(() => ({
  tasks: 'WINDOWS MULTI-AGENT SYSTEM',
  research: 'VERIFIED RESEARCH DELIVERY',
  memory: 'PROTECTED MEMORY EVIDENCE LEDGER',
  knowledge: 'CONTENT-ADDRESSED LOCAL MEMORY',
  mcp: 'CONTROLLED MODEL CONTEXT PROTOCOL',
  evaluations: 'DETERMINISTIC EVALUATION TRACE',
  reconciliations: 'DURABLE EXECUTION LEDGER',
  providers: 'OPENAI-COMPATIBLE GATEWAY',
  operations: 'FENCED RUNTIME CONTROL PLANE',
})[activeView.value])

const stageTitle = computed(() => ({
  tasks: '阶段 2 · 可控任务',
  research: '阶段 76 · 研究交付台',
  memory: '阶段 73 · 长期记忆',
  knowledge: '阶段 3 · 本地知识',
  mcp: '阶段 3 · 受控 MCP',
  evaluations: '阶段 3 · 评测追踪',
  reconciliations: '阶段 2 · 持久化对账',
  providers: '阶段 2 · 模型控制面',
  operations: '阶段 2 · 受保护运维',
})[activeView.value])

const stageDescription = computed(() => ({
  tasks: '通过检查点、实时事件和控制命令验证执行闭环。',
  research: '统一查看 Claim、Citation、Artifact、PatchReceipt、浏览器验收与精确导出。',
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
  <section v-if="coverVisible" ref="coverRef" class="cover" aria-label="DeskPilot 首页">
    <div class="cover-stage" @click="enterWorkspace">
      <video
        class="cover-video"
        autoplay
        muted
        loop
        playsinline
        preload="metadata"
        :poster="coverPoster"
        aria-hidden="true"
      >
        <source :src="coverVideo" type="video/mp4">
      </video>
      <div class="cover-shade" aria-hidden="true" />
      <div class="cover-copy">
        <header class="cover-brand" data-intro>
          <div>
            <strong>DESKPILOT</strong>
            <small>LOCAL TASK AGENT</small>
          </div>
          <p>LOCAL&nbsp;&nbsp;/&nbsp;&nbsp;RESEARCH&nbsp;&nbsp;/&nbsp;&nbsp;ARTIFACTS</p>
        </header>

        <aside class="cover-note" data-intro>
          <span>FIELD NOTE · 01</span>
          <strong>在本地思考<br>必要时联网<br>最终交付产物</strong>
          <p>对话是入口，任务是主对象，证据和可带走的文件是出口。</p>
        </aside>

        <h1 class="cover-title" aria-label="The task agent">
          <span data-intro>THE</span>
          <span data-intro>TASK</span>
          <span data-intro>AGENT</span>
        </h1>

        <div class="cover-manifesto" data-intro>
          <span>GENERAL PURPOSE · LOCAL FIRST</span>
          <strong>会对话，会研究，会执行，也会把结果做成数字产物。</strong>
        </div>

        <button class="cover-enter" type="button" data-intro @click.stop="enterWorkspace">
          <span>进入工作台</span>
          <span aria-hidden="true">↗</span>
        </button>
      </div>
      <div class="cover-iris-wrap" aria-hidden="true">
        <div ref="irisRef" class="cover-iris" />
      </div>
    </div>
  </section>

  <main
    ref="workspaceRef"
    class="workspace"
    :inert="coverVisible || undefined"
    :aria-hidden="coverVisible ? 'true' : undefined"
  >
    <div class="hud-field" aria-hidden="true">
      <span class="hud-grid" />
      <span class="hud-horizon" />
      <span class="hud-reticle hud-reticle-main"><i /><i /><i /></span>
      <span class="hud-reticle hud-reticle-minor"><i /><i /></span>
    </div>
    <div class="hud-telemetry" aria-hidden="true">
      <span>LOCAL RUNTIME</span>
      <span>EGRESS GATED</span>
      <span>AUDIT ON</span>
    </div>
    <div ref="viewTransitionRef" class="hud-transition" aria-hidden="true" />

    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark">DP</span>
        <div>
          <strong>DeskPilot</strong>
          <small>Local agent workspace</small>
        </div>
      </div>

      <nav aria-label="工作区导航">
        <button class="nav-item" :class="{ active: activeView === 'tasks' }" type="button" @click="switchView('tasks')">
          <span>任务工作台</span>
          <span class="nav-count">TASK</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'research' }" type="button" @click="switchView('research')">
          <span>研究交付台</span>
          <span class="nav-count">P76</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'knowledge' }" type="button" @click="switchView('knowledge')">
          <span>知识库</span>
          <span class="nav-count">RAG</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'memory' }" type="button" @click="switchView('memory')">
          <span>长期记忆</span>
          <span class="nav-count">MEM</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'mcp' }" type="button" @click="switchView('mcp')">
          <span>Agent 与 MCP</span>
          <span class="nav-count">MCP</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'evaluations' }" type="button" @click="switchView('evaluations')">
          <span>评测与 Trace</span><span class="nav-count">EVAL</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'reconciliations' }" type="button" @click="switchView('reconciliations')">
          <span>历史与对账</span>
          <span class="nav-count">LOG</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'providers' }" type="button" @click="switchView('providers')">
          <span>模型与设置</span>
          <span class="nav-count">MODEL</span>
        </button>
        <button class="nav-item" :class="{ active: activeView === 'operations' }" type="button" @click="switchView('operations')">
          <span>运行时运维</span>
          <span class="nav-count">OPS</span>
        </button>
      </nav>

      <div class="stage-card">
        <span class="stage-label">当前阶段</span>
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
      <section class="composer motion-section" aria-labelledby="task-heading">
        <div class="composer-copy">
          <span class="eyebrow">NEW TASK</span>
          <h2 id="task-heading">创建可暂停的任务闭环</h2>
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
              {{ submitting ? '创建中…' : taskInProgress ? '当前任务进行中' : '开始任务' }}
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
        :task-switch-locked="taskInProgress"
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
