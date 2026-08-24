<script setup lang="ts">
import { gsap } from 'gsap'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  ApiProblemError,
  advanceTaskWorkbench,
  commitArtifactExport,
  commitWorkspaceEdit,
  commitWorkspacePathOperation,
  commitWorkspacePatch,
  continueConversationTurn,
  createConversationTurn,
  getTaskWorkbench,
  prepareArtifactExport,
  replanTaskWorkbench,
  setMcpServerEnabled,
  stopTaskWorkbench,
} from '../api'
import type {
  ArtifactExport,
  TaskWorkbench,
  WorkbenchAction,
  WorkbenchNode,
  WorkspaceEditPreview,
  WorkspacePatchPreview,
  WorkspacePathOperationPreview,
} from '../types'

type BusyAction =
  | 'turn'
  | 'auto'
  | 'stop'
  | 'enable_mcp'
  | 'commit_workspace_edit'
  | 'commit_workspace_patch'
  | 'commit_workspace_path_operation'
  | 'replan_failed_execution'
  | 'prepare_export'
  | 'commit_export'

interface FlowItem {
  key: string
  action: WorkbenchAction
  label: string
  detail: string
}

const MCP_SERVER_ID = 'deskpilot.readonly-text'

const prompt = ref('')
const privacyMode = ref<'local_preferred' | 'balanced'>('balanced')
const workbench = ref<TaskWorkbench | null>(null)
const busyAction = ref<BusyAction | null>(null)
const error = ref<string | null>(null)
const targetPath = ref('')
const selectedArtifactId = ref('')
const exportPreview = ref<ArtifactExport | null>(null)
const transcript = ref<HTMLElement | null>(null)
const promptInput = ref<HTMLTextAreaElement | null>(null)
let automationToken = 0
let motionContext: gsap.Context | null = null

const starterSuggestions = [
  '研究一个公开主题，给我带引用的 HTML、Markdown 与 PDF 报告',
  '在知识库里查询：verified edge',
  '统计字符数：DeskPilot Agent',
  '读取工作区文件：README.md',
  '在工作区文件 README.md 中把 "旧文本" 替换为 "新文本"',
  '批量修改工作区文件：在工作区文件 a.md 中把 "旧" 替换为 "新"；在工作区文件 b.md 中把 "旧" 替换为 "新"',
  '新建工作区文件："notes/todo.md" 内容："第一项"',
  '将工作区文件 "notes/old.md" 重命名为 "notes/new.md"',
  '列出工作区目录：backend/src',
  '运行工作区检查：python-syntax backend/src',
  '运行项目测试：backend tests/test_workspace_file_runtime.py',
  '运行 Node 测试：frontend tests/sample.test.js',
  '修复并测试工作区：文件："backend/example.py" Python项目："backend" Python测试："tests/test_example.py" 目标：修复失败测试',
]

const stageLabel: Record<string, string> = {
  idle: '等待指令', interpreting: '正在解释任务', planned: '计划已建立', researching: '正在公开取证',
  awaiting_verification: '正在独立核验', building_artifact: '正在构建交付物',
  verifying_browser: '正在隔离验收', ready_to_deliver: '正在形成交付',
  delivered: '已完成', exported: '已导出', executing: '正在执行',
  needs_clarification: '需要补充目标', needs_user_action: '等待你的授权',
  unsupported: '当前能力未覆盖', blocked: '需要处理',
}

const researchFlow: FlowItem[] = [
  { key: 'research', action: 'run_research' as const, label: '研究公开来源', detail: '受控搜索与安全页面读取' },
  { key: 'research', action: 'verify_claims' as const, label: '核验事实与引用', detail: '只让有证据的 Claim 进入下游' },
  { key: 'build_html', action: 'build_artifact' as const, label: '构建 HTML + Markdown + PDF', detail: 'PDF 逐页真实渲染后，与同源交付物分别留下 PatchReceipt' },
  { key: 'browser_verify', action: 'verify_browser' as const, label: '隔离浏览器验收', detail: '断网、无登录态、无动态脚本' },
  { key: 'final_acceptance', action: 'finalize_delivery' as const, label: '形成交付清单', detail: '汇总证据、限制与不可变摘要' },
]

const autoActions = new Set<WorkbenchAction>([
  'interpret_turn', 'run_research', 'verify_claims', 'build_artifact', 'verify_browser', 'finalize_delivery', 'execute_route', 'replan_failed_execution',
])

const routeLabels: Record<string, string> = {
  research_to_html: '公开研究 → HTML + Markdown + PDF',
  knowledge_lookup: '本地知识查询',
  mcp_text_metrics: '受控 MCP 文本统计',
  workspace_file_read: '工作区文件读取',
  workspace_file_replace: '工作区精确替换',
  workspace_patch_bundle: '工作区多文件补丁',
  workspace_agent_patch_test: 'Agent 补丁 → 固定测试',
  workspace_dynamic_patch_test: '动态 DAG → Patch/Approval → 固定测试',
  workspace_file_create: '工作区新建文件',
  workspace_file_rename: '工作区文件重命名',
  workspace_directory_list: '工作区目录列表',
  workspace_directory_analyze: '异构只读目录分析',
  workspace_snapshot_check: '断网快照检查',
  workspace_python_test: 'Python 项目测试',
  workspace_node_test: 'Node 项目测试',
}

const run = computed(() => workbench.value?.executions.runs.at(-1) ?? null)
const modelTurns = computed(() => run.value?.model_turns ?? [])
const inputRequests = computed(() => run.value?.input_requests ?? [])
const delegations = computed(() => run.value?.delegations ?? [])
const taskGraphs = computed(() => run.value?.task_graphs ?? [])
const actionMap = computed(() => new Map(
  (workbench.value?.actions ?? []).map((item) => [item.action, item]),
))
const nodeMap = computed(() => new Map(
  (run.value?.nodes ?? []).map((item) => [item.local_key, item]),
))
const verdictMap = computed(() => new Map(
  (workbench.value?.verification?.verdicts ?? []).map((item) => [item.claim_id, item]),
))
const enabledAutoAction = computed(() => workbench.value?.actions.find(
  (item) => item.enabled
    && autoActions.has(item.action)
    && !(
      item.action === 'replan_failed_execution'
      && workbench.value?.route?.route_id === 'workspace_dynamic_patch_test'
    ),
) ?? null)
const canConditionReplan = computed(() => Boolean(
  workbench.value?.route?.route_id === 'workspace_dynamic_patch_test'
  && actionMap.value.get('replan_failed_execution')?.enabled,
))
const repairLoop = computed(() => workbench.value?.repair_loop ?? null)
const conditionReplanLimitReached = computed(() => Boolean(
  workbench.value?.route?.route_id === 'workspace_dynamic_patch_test'
  && workbench.value.route.status === 'failed'
  && workbench.value.route.error_code === 'AGENT_GRAPH_TEST_CONDITION_NOT_MET'
  && repairLoop.value
  && !repairLoop.value.next_replan_available,
))
const repairBudgetPercent = computed(() => {
  const loop = repairLoop.value
  if (!loop || loop.budget_limit.cost_micros <= 0) return 0
  return Math.min(
    100,
    Math.round((loop.budget_allocated.cost_micros / loop.budget_limit.cost_micros) * 100),
  )
})
const failedConditionDecision = computed(() => taskGraphs.value
  .flatMap((graph) => graph.nodes)
  .flatMap((node) => node.condition_decisions ?? [])
  .find((decision) => !decision.matched) ?? null)
const observesServerProgress = computed(() => Boolean(
  enabledAutoAction.value
  || workbench.value?.stage === 'interpreting'
  || (
    run.value
    && ['active', 'awaiting_verification'].includes(run.value.status)
    && ['researching', 'awaiting_verification', 'building_artifact', 'verifying_browser', 'ready_to_deliver', 'executing'].includes(workbench.value?.stage ?? '')
  )
))
const isLive = computed(() => busyAction.value === 'turn' || busyAction.value === 'auto')
const canStop = computed(() => Boolean(actionMap.value.get('stop_execution')?.enabled))
const exportArtifacts = computed(() => workbench.value?.workspace?.artifacts.filter(
  (artifact) => ['application/pdf', 'text/html', 'text/markdown'].includes(
    artifact.active_revision.media_type,
  ),
) ?? [])
const selectedArtifact = computed(() => exportArtifacts.value.find(
  (artifact) => artifact.artifact_id === selectedArtifactId.value,
) ?? null)
const exportPathPlaceholder = computed(() => {
  const mediaType = selectedArtifact.value?.active_revision.media_type
  if (mediaType === 'application/pdf') return 'D:\\Reports\\research.pdf'
  if (mediaType === 'text/markdown') return 'D:\\Reports\\research.md'
  return 'D:\\Reports\\research.html'
})
const workspaceEditPreview = computed<WorkspaceEditPreview | null>(() => {
  const edit = workbench.value?.workspace_edit
  return edit?.schema_version === 'deskpilot.workspace-edit-preview.v1' ? edit : null
})
const workspacePatchPreview = computed<WorkspacePatchPreview | null>(() => {
  const patch = workbench.value?.workspace_patch
  return patch?.schema_version === 'deskpilot.workspace-patch-preview.v1' ? patch : null
})
const workspacePathOperationPreview = computed<WorkspacePathOperationPreview | null>(() => {
  const operation = workbench.value?.workspace_path_operation
  return operation?.schema_version === 'deskpilot.workspace-path-operation-preview.v1'
    ? operation
    : null
})
const evidenceCount = computed(() => (
  (workbench.value?.research?.claims.length ?? 0)
  + (workbench.value?.research?.citations.length ?? 0)
  + (workbench.value?.workspace?.artifacts.length ?? 0)
  + (workbench.value?.knowledge?.citations.length ?? 0)
  + (workbench.value?.mcp ? 1 : 0)
  + (workbench.value?.workspace_file ? 1 : 0)
  + (workbench.value?.workspace_edit ? 1 : 0)
  + (workbench.value?.workspace_patch ? 1 : 0)
  + (workbench.value?.workspace_path_operation ? 1 : 0)
  + (workbench.value?.workspace_directory ? 1 : 0)
  + (workbench.value?.workspace_check ? 1 : 0)
  + (workbench.value?.workspace_python_test ? 1 : 0)
  + (workbench.value?.workspace_node_test ? 1 : 0)
  + (workbench.value?.turn_planning ? 1 : 0)
))
const routeFlow = computed<FlowItem[]>(() => {
  if (workbench.value?.stage === 'interpreting') {
    return [{ key: 'interpret_turn', action: 'interpret_turn', label: '解释任务并选择能力', detail: '模型只能引用服务器预编译 Offer；选择本身不授予权限' }]
  }
  if (workbench.value?.route?.route_id === 'knowledge_lookup') {
    return [{ key: 'knowledge_lookup', action: 'execute_route', label: '查询本地知识', detail: '内容寻址检索，返回片段与来源证明' }]
  }
  if (workbench.value?.route?.route_id === 'mcp_text_metrics') {
    return [{ key: 'mcp_text_metrics', action: 'execute_route', label: '调用只读文本工具', detail: '固定 Server 与 Schema，调用结果进入审计链' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_file_read') {
    return [{ key: 'workspace_file_read', action: 'execute_route', label: '读取工作区文件', detail: '限定根目录、文件类型、大小与 UTF-8 编码' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_directory_list') {
    return [{ key: 'workspace_directory_list', action: 'execute_route', label: '列出工作区目录', detail: '只返回受限直接子项与版本摘要' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_directory_analyze') {
    if (actionMap.value.get('replan_failed_execution')?.enabled) {
      return [{ key: 'workspace_directory_analyze', action: 'replan_failed_execution', label: '生成替换计划代', detail: '封存失败快照，保持旧 Plan、Run 与动态图不可变' }]
    }
    return [{ key: 'workspace_directory_analyze', action: 'execute_route', label: '运行异构只读 DAG', detail: '目录与显式文件输入均由服务器绑定' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_snapshot_check') {
    return [{ key: 'workspace_snapshot_check', action: 'execute_route', label: '运行固定快照检查', detail: '只解析内容快照，断网且不执行仓库代码' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_python_test') {
    return [{ key: 'workspace_python_test', action: 'execute_route', label: '运行 Python 测试', detail: '只读项目快照、固定 pytest 文件、断网 AppContainer' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_node_test') {
    return [{ key: 'workspace_node_test', action: 'execute_route', label: '运行 Node 测试', detail: '有界项目快照、固定 node:test 文件、断网 AppContainer' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_file_replace') {
    return [{ key: 'workspace_file_replace', action: 'commit_workspace_edit', label: '确认并提交精确替换', detail: '复核原版本后原子替换，并保留安全备份' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_patch_bundle') {
    return [{ key: 'workspace_patch_bundle', action: 'commit_workspace_patch', label: '确认并提交多文件补丁', detail: '先核验全部原版本，再按序提交并保留逐项备份' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_agent_patch_test') {
    if (workspacePatchPreview.value) {
      return [{ key: 'workspace_agent_patch_test', action: 'commit_workspace_patch', label: '确认补丁并运行固定测试', detail: '提交一个已绑定的精确替换，随后按服务器固定协议测试' }]
    }
    return [{ key: 'workspace_agent_patch_test', action: 'execute_route', label: '生成受约束补丁建议', detail: 'Agent 只读显式目标，建议本身没有写权限' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_dynamic_patch_test') {
    if (canConditionReplan.value) {
      return [{ key: 'workspace_dynamic_patch_test', action: 'replan_failed_execution', label: '生成新的修复计划代', detail: '旧补丁和 false decision 保持不可变；新 Patch 必须重新预演并再次确认' }]
    }
    if (workspacePatchPreview.value) {
      return [{ key: 'workspace_dynamic_patch_test', action: 'commit_workspace_patch', label: '批准图内补丁并运行固定测试', detail: '确认只覆盖当前 Patch 节点；摘要、文件版本或图绑定改变都会拒绝' }]
    }
    return [{ key: 'workspace_dynamic_patch_test', action: 'execute_route', label: '继续动态修复 DAG', detail: '只推进服务器判定为 ready 的节点和 verified join' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_file_create') {
    return [{ key: 'workspace_file_create', action: 'commit_workspace_path_operation', label: '确认并创建文件', detail: '仅当目标仍不存在时原子提交，并保留恢复清单' }]
  }
  if (workbench.value?.route?.route_id === 'workspace_file_rename') {
    return [{ key: 'workspace_file_rename', action: 'commit_workspace_path_operation', label: '确认并重命名文件', detail: '复核源版本和目标目录后原子改名，不改文件内容' }]
  }
  if (workbench.value?.route?.decision && workbench.value.route.decision !== 'routed') return []
  return researchFlow
})
const routeName = computed(() => {
  const route = workbench.value?.route
  if (workbench.value?.stage === 'interpreting') return '正在匹配安全能力'
  if (!route) return '未建立路由'
  if (route.decision === 'needs_clarification') return '需要澄清'
  if (route.decision === 'unsupported') return '未匹配能力'
  return route.route_id ? routeLabels[route.route_id] : '已路由'
})
const routeExplanation = computed(() => {
  const value = workbench.value
  if (!value) return ''
  if (value.stage === 'interpreting') return '服务器已冻结可用能力、契约、预算与参数边界；本地 Planner 只能从这些 opaque Offer 中提案。'
  if (value.stage === 'needs_clarification') return '请说明要研究公开来源、查询本地知识、统计文本，还是读取、修改或检查工作区。'
  if (value.stage === 'unsupported') return '这条指令没有被偷偷执行；系统只会运行已声明且当前可用的安全路由。'
  if (value.stage === 'needs_user_action' && value.route?.route_id === 'workspace_file_replace') {
    return '替换预览已绑定当前文件版本；确认前不会修改文件。'
  }
  if (value.stage === 'needs_user_action' && value.route?.route_id === 'workspace_patch_bundle') {
    return '多文件 diff 已在隔离副本中生成；一次确认前不会修改原文件。'
  }
  if (value.stage === 'needs_user_action' && value.route?.route_id === 'workspace_agent_patch_test') {
    return 'Agent 建议已绑定读取证据；确认前不写入，确认后只提交这一处并运行固定测试。'
  }
  if (value.stage === 'needs_user_action' && value.route?.route_id === 'workspace_dynamic_patch_test') {
    return '动态图已停在 Patch/Approval 节点；本次确认只授权当前摘要，测试通过后才会解锁下游。'
  }
  if (value.stage === 'blocked' && canConditionReplan.value) {
    return '固定测试没有通过。你可以显式生成一代新计划；旧补丁不会回滚或变成新授权。'
  }
  if (value.stage === 'needs_user_action' && value.route?.route_id === 'workspace_file_create') {
    return '新文件内容和目标父目录已绑定；不确认不写入，同名目标出现时会拒绝。'
  }
  if (value.stage === 'needs_user_action' && value.route?.route_id === 'workspace_file_rename') {
    return '源文件版本和目标父目录已绑定；不确认不改名，目标存在时会拒绝。'
  }
  if (value.stage === 'needs_user_action') return '路由已确定，但只读 MCP Server 默认关闭，需要你明确启用。'
  return enabledAutoAction.value?.explanation
    ?? (value.delivery || value.route?.status === 'succeeded' ? '所有验证边已闭合，结果与凭据可以检查。' : '等待下一条用户指令。')
})
const metricEntries = computed(() => Object.entries(workbench.value?.mcp?.structured_content ?? {}))

function shortDigest(value: string | null | undefined): string {
  return value ? `${value.slice(0, 9)}…${value.slice(-5)}` : '无'
}

function problemMessage(caught: unknown): string {
  if (caught instanceof ApiProblemError) return `${caught.message}（${caught.code}）`
  return caught instanceof Error ? caught.message : '操作失败，请重试。'
}

function nodeState(node: WorkbenchNode | undefined, action: WorkbenchAction): 'done' | 'active' | 'queued' {
  if (actionMap.value.get(action)?.enabled) return 'active'
  if (action === 'interpret_turn' && workbench.value?.stage === 'interpreting') return 'active'
  if (!node) return 'queued'
  if (action === 'run_research') {
    return ['awaiting_verification', 'verified'].includes(node.status) ? 'done' : 'queued'
  }
  return node.status === 'verified' ? 'done' : 'queued'
}

async function enableMcpAndContinue(): Promise<void> {
  const taskId = workbench.value?.task.task_id
  if (!taskId || busyAction.value) return
  const token = ++automationToken
  busyAction.value = 'enable_mcp'
  error.value = null
  try {
    await setMcpServerEnabled(MCP_SERVER_ID, true)
    workbench.value = await getTaskWorkbench(taskId)
    if (enabledAutoAction.value) {
      workbench.value = await advanceTaskWorkbench(taskId)
    }
    busyAction.value = null
    await nextTick()
    await observeAutomaticProgress(token)
  } catch (caught) {
    error.value = problemMessage(caught)
    if (token === automationToken) busyAction.value = null
  }
}

async function confirmWorkspaceEdit(): Promise<void> {
  const taskId = workbench.value?.task.task_id
  const preview = workspaceEditPreview.value
  if (!taskId || !preview || busyAction.value) return
  busyAction.value = 'commit_workspace_edit'
  error.value = null
  try {
    workbench.value = await commitWorkspaceEdit(taskId, preview.confirmation_digest)
  } catch (caught) {
    error.value = problemMessage(caught)
  } finally {
    busyAction.value = null
  }
}

async function confirmWorkspacePatch(): Promise<void> {
  const taskId = workbench.value?.task.task_id
  const preview = workspacePatchPreview.value
  if (!taskId || !preview || busyAction.value) return
  busyAction.value = 'commit_workspace_patch'
  error.value = null
  try {
    workbench.value = await commitWorkspacePatch(taskId, preview.confirmation_digest)
  } catch (caught) {
    error.value = problemMessage(caught)
    workbench.value = await getTaskWorkbench(taskId).catch(() => workbench.value)
  } finally {
    busyAction.value = null
  }
}

async function confirmWorkspacePathOperation(): Promise<void> {
  const taskId = workbench.value?.task.task_id
  const preview = workspacePathOperationPreview.value
  if (!taskId || !preview || busyAction.value) return
  busyAction.value = 'commit_workspace_path_operation'
  error.value = null
  try {
    workbench.value = await commitWorkspacePathOperation(taskId, preview.confirmation_digest)
  } catch (caught) {
    error.value = problemMessage(caught)
    workbench.value = await getTaskWorkbench(taskId).catch(() => workbench.value)
  } finally {
    busyAction.value = null
  }
}

async function editWorkspacePatch(): Promise<void> {
  prompt.value = workbench.value?.conversation.filter((item) => item.role === 'user').at(-1)?.content ?? ''
  await nextTick()
  promptInput.value?.focus()
}

function useSuggestion(suggestion: string): void {
  prompt.value = suggestion
}

function waitForServerProjection(delayMs: number): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, delayMs))
}

async function observeAutomaticProgress(token: number): Promise<void> {
  busyAction.value = 'auto'
  try {
    for (let observation = 0; observation < 600 && token === automationToken; observation += 1) {
      const taskId = workbench.value?.task.task_id
      if (!taskId || !observesServerProgress.value) break
      const previousDigest = workbench.value?.projection_digest
      try {
        workbench.value = await getTaskWorkbench(taskId)
      } catch (caught) {
        if (
          caught instanceof ApiProblemError
          && caught.status === 409
          && caught.code === 'TASK_WORKBENCH_CONFLICT'
        ) {
          await waitForServerProjection(250)
          continue
        }
        throw caught
      }
      await nextTick()
      if (workbench.value.projection_digest === previousDigest) {
        await waitForServerProjection(250)
      }
    }
  } catch (caught) {
    if (token === automationToken) error.value = problemMessage(caught)
  } finally {
    if (token === automationToken) busyAction.value = null
  }
}

async function submitTurn(): Promise<void> {
  const message = prompt.value.trim()
  if (!message || (busyAction.value && busyAction.value !== 'auto')) return
  const token = ++automationToken
  busyAction.value = 'turn'
  error.value = null
  exportPreview.value = null
  try {
    workbench.value = workbench.value
      ? await continueConversationTurn(workbench.value.task.task_id, { message })
      : await createConversationTurn({ message, privacy_mode: privacyMode.value, constraints: [] })
    prompt.value = ''
    await nextTick()
    await observeAutomaticProgress(token)
  } catch (caught) {
    error.value = problemMessage(caught)
    if (token === automationToken) busyAction.value = null
  }
}

async function stopExecution(): Promise<void> {
  const taskId = workbench.value?.task.task_id
  if (!taskId || !canStop.value || busyAction.value === 'stop') return
  automationToken += 1
  busyAction.value = 'stop'
  error.value = null
  try {
    workbench.value = await stopTaskWorkbench(taskId)
  } catch (caught) {
    error.value = problemMessage(caught)
  } finally {
    busyAction.value = null
  }
}

async function replanFailedExecution(): Promise<void> {
  const taskId = workbench.value?.task.task_id
  if (!taskId || !canConditionReplan.value || busyAction.value) return
  const token = ++automationToken
  busyAction.value = 'replan_failed_execution'
  error.value = null
  try {
    workbench.value = await replanTaskWorkbench(taskId)
    busyAction.value = null
    await nextTick()
    await observeAutomaticProgress(token)
  } catch (caught) {
    error.value = problemMessage(caught)
    if (token === automationToken) busyAction.value = null
  }
}

function freshKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
  return `${prefix}-${suffix}`
}

async function previewExport(): Promise<void> {
  const delivery = workbench.value?.delivery
  if (!delivery || !targetPath.value.trim() || busyAction.value) return
  busyAction.value = 'prepare_export'
  error.value = null
  try {
    exportPreview.value = await prepareArtifactExport(
      delivery.delivery_id, targetPath.value.trim(), freshKey('prepare'),
      selectedArtifactId.value || delivery.artifact_id,
    )
  } catch (caught) {
    error.value = problemMessage(caught)
  } finally {
    busyAction.value = null
  }
}

async function commitExport(): Promise<void> {
  if (!exportPreview.value || busyAction.value) return
  busyAction.value = 'commit_export'
  error.value = null
  try {
    exportPreview.value = await commitArtifactExport(
      exportPreview.value.export_id,
      exportPreview.value.confirmation_digest,
      freshKey('commit'),
    )
  } catch (caught) {
    error.value = problemMessage(caught)
  } finally {
    busyAction.value = null
  }
}

onMounted(() => {
  motionContext = gsap.context(() => undefined, transcript.value ?? undefined)
})

watch(
  () => [workbench.value?.delivery?.artifact_id, exportArtifacts.value.map((item) => item.artifact_id).join(',')] as const,
  ([deliveryArtifactId]) => {
    if (selectedArtifactId.value && exportArtifacts.value.some(
      (artifact) => artifact.artifact_id === selectedArtifactId.value,
    )) return
    selectedArtifactId.value = deliveryArtifactId ?? exportArtifacts.value[0]?.artifact_id ?? ''
  },
  { immediate: true },
)

watch(selectedArtifactId, () => {
  exportPreview.value = null
  targetPath.value = ''
})

watch(
  () => workbench.value?.conversation.length ?? 0,
  async (count, previous) => {
    if (count <= previous) return
    await nextTick()
    if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight
    if (globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    const latest = transcript.value?.querySelector('.message:last-child')
    if (!latest) return
    motionContext?.add(() => {
      gsap.fromTo(
        latest,
        { autoAlpha: 0, y: 8 },
        { autoAlpha: 1, y: 0, duration: 0.24, ease: 'power2.out' },
      )
    })
  },
)

onBeforeUnmount(() => {
  automationToken += 1
  motionContext?.revert()
})
</script>

<template>
  <section class="agent-workbench" aria-labelledby="agent-workbench-title">
    <aside class="session-rail">
      <header>
        <span class="eyebrow">DESKPILOT / AGENT</span>
        <h2 id="agent-workbench-title">把目标交给我</h2>
        <p>用自然语言开始；计划与工具调用由系统在后台建立。</p>
      </header>

      <nav aria-label="任务示例">
        <span>可以这样开始</span>
        <button
          v-for="suggestion in starterSuggestions"
          :key="suggestion"
          type="button"
          :disabled="Boolean(busyAction && busyAction !== 'auto')"
          @click="useSuggestion(suggestion)"
        >{{ suggestion }}</button>
      </nav>

      <section class="autonomy-card" aria-labelledby="autonomy-title">
        <div>
          <span id="autonomy-title">自动执行边界</span>
          <strong>安全步骤自动推进</strong>
        </div>
        <p>公开读取、工作区写入与验证会自动进行；写入用户路径仍需确认。</p>
        <label for="agent-privacy">联网策略</label>
        <select id="agent-privacy" v-model="privacyMode" :disabled="Boolean(workbench)">
          <option value="balanced">平衡模式</option>
          <option value="local_preferred">本地优先</option>
        </select>
      </section>

      <footer v-if="workbench" class="session-fingerprint">
        <span>当前 Route</span>
        <code>{{ routeName }}</code>
        <span>当前 Task</span>
        <code>{{ shortDigest(workbench.task.task_id.replace('tsk_', '')) }}</code>
        <span>Projection</span>
        <code>{{ shortDigest(workbench.projection_digest) }}</code>
      </footer>
    </aside>

    <main class="conversation-column">
      <header class="conversation-header">
        <div>
          <span class="eyebrow">CONVERSATION</span>
          <h2>{{ workbench?.task.goal ?? '今天要完成什么？' }}</h2>
        </div>
        <div class="run-controls">
          <span class="run-state" :data-live="isLive">
            <i aria-hidden="true" />{{ stageLabel[workbench?.stage ?? 'idle'] }}
          </span>
          <button type="button" :disabled="!canStop || busyAction === 'stop'" @click="stopExecution">
            {{ busyAction === 'stop' ? '停止中…' : '停止' }}
          </button>
        </div>
      </header>

      <p v-if="error" class="workbench-error" role="alert">{{ error }}</p>

      <section ref="transcript" class="transcript" aria-live="polite">
        <div v-if="!workbench" class="empty-conversation">
          <span class="empty-mark" aria-hidden="true">DP</span>
          <h3>一句话描述结果，不用先拆步骤。</h3>
          <p>我会把任务转成可停止、可核验、可追溯的执行过程，并在这里解释正在做什么。</p>
        </div>

        <article
          v-for="message in workbench?.conversation ?? []"
          :key="message.message_id"
          class="message"
          :data-role="message.role"
        >
          <span class="message-author">{{ message.role === 'user' ? '你' : 'DP' }}</span>
          <div>
            <strong>{{ message.role === 'user' ? '你的要求' : 'DeskPilot' }}</strong>
            <p>{{ message.content ?? '这条内容保留为本地引用。' }}</p>
            <time>{{ new Date(message.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</time>
          </div>
        </article>

        <article v-if="workbench" class="run-card" :data-live="isLive">
          <header>
            <div><span class="eyebrow">CURRENT RUN</span><h3>{{ stageLabel[workbench.stage] }}</h3></div>
            <span>{{ routeName }} · {{ workbench.route?.status ?? run?.status ?? 'preparing' }}</span>
          </header>
          <p>{{ routeExplanation }}</p>
          <div v-if="modelTurns.length" class="model-loop-proof" aria-label="Agent Model Loop 证明">
            <span>MODEL LOOP</span>
            <ol>
              <li v-for="turn in modelTurns" :key="turn.turn_id">
                <b>TURN {{ turn.turn_no }}</b>
                <strong>{{ turn.decision_kind === 'request_route' ? '请求受控 Route' : turn.decision_kind === 'propose_task_graph' ? '提议完整子任务 DAG' : turn.decision_kind === 'propose_handoff' ? '提议受控子 Agent' : turn.decision_kind === 'submit_result' ? '提交候选结果' : turn.decision_kind === 'needs_user_input' ? '等待用户输入' : turn.status }}</strong>
                <code>{{ shortDigest(turn.observation_digest ?? turn.decision_digest) }}</code>
              </li>
            </ol>
          </div>
          <details v-if="['workspace_directory_list', 'workspace_directory_analyze', 'workspace_dynamic_patch_test'].includes(workbench.route?.route_id ?? '') && run" class="agent-task-tree" open>
            <summary>AGENT TASK GRAPH · {{ taskGraphs.length ? 'DYNAMIC DAG · SERVER-ADJUDICATED' : delegations.length ? 'SERVER-ADJUDICATED' : 'PRECOMPILED' }}</summary>
            <ol>
              <li v-for="node in run.nodes.filter((item) => item.bound_agent)" :key="node.node_id">
                <div>
                  <b>{{ node.bound_agent?.agent_id }}</b>
                  <span>{{ node.handoff_parent_node_id ? 'CHILD' : 'PARENT' }} · {{ node.status }}</span>
                </div>
                <small v-if="node.budget">MODEL {{ node.budget.model_calls }} · TOOL {{ node.budget.tool_calls }} · HANDOFF {{ node.budget.handoffs }}</small>
              </li>
            </ol>
            <p v-for="item in delegations" :key="item.delegation_id">
              DEPTH {{ item.depth }} · {{ item.status }} · EVIDENCE {{ shortDigest(item.observation_id ?? item.child_result_id) }}
            </p>
            <section v-for="graph in taskGraphs" :key="graph.graph_id" class="dynamic-task-graph">
              <p>GRAPH {{ graph.status }} · NODES {{ graph.node_count }} · DEPTH {{ graph.max_depth }} · {{ graph.nodes.some((item) => item.conditions?.length) ? 'SERVER-CONDITIONAL · ' : '' }}OUTPUT {{ graph.output_local_key ?? 'LEGACY' }} · PROOF {{ shortDigest(graph.graph_digest) }}</p>
              <ol>
                <li v-for="item in graph.nodes" :key="item.node_id">
                  <div>
                    <b>{{ item.local_key }} · {{ item.target_agent.agent_id }}@{{ item.target_agent.version }}</b>
                    <span>{{ item.status }} · {{ item.capability.capability_id }}</span>
                  </div>
                  <small>DEPENDS {{ item.depends_on.length ? item.depends_on.join(' + ') : 'ROOT' }} · {{ item.local_key === graph.output_local_key ? 'OUTPUT · ' : '' }}RESULTREF {{ shortDigest(item.result_ref?.result_ref_digest ?? item.child_result_id) }}</small>
                  <small v-if="item.capability_input">INPUT {{ item.capability_input.source_key }} · {{ item.capability_input.path }}{{ item.capability_input.test_path ? ` / ${item.capability_input.test_path}` : '' }} · {{ shortDigest(item.capability_input.input_digest) }}</small>
                  <small v-if="item.capability_input?.target_path">PATCH TARGET {{ item.capability_input.target_path }} · {{ item.capability_input.test_kind?.toUpperCase() }} FIXED TEST</small>
                  <small v-for="condition in item.conditions ?? []" :key="condition.condition_digest">CONDITION {{ condition.source_local_key }}.{{ condition.predicate.toUpperCase() }} · {{ item.condition_decisions?.find((decision) => decision.source_node_id === condition.source_node_id)?.matched === true ? 'PASSED' : item.condition_decisions?.find((decision) => decision.source_node_id === condition.source_node_id)?.matched === false ? 'BLOCKED' : 'WAITING' }} · {{ shortDigest(item.condition_decisions?.find((decision) => decision.source_node_id === condition.source_node_id)?.decision_digest ?? condition.condition_digest) }}</small>
                  <small v-if="item.import_sources?.length">REUSED VERIFIED {{ item.import_sources.join(' + ') }} · {{ item.imported_result_refs?.map((ref) => shortDigest(ref.result_ref_digest)).join(' + ') }}</small>
                  <small v-if="item.approval">APPROVAL {{ item.patch_result ? 'CONSUMED' : 'WAITING USER' }} · {{ shortDigest(item.approval.confirmation_digest) }} · ONE PATCH ONLY</small>
                  <small v-if="item.patch_result">PATCH {{ item.patch_result.status }} · RECEIPT {{ shortDigest(item.patch_result.patch_receipt.receipt_digest) }} · RESULT {{ shortDigest(item.patch_result.result_digest) }}</small>
                  <small v-if="item.test_result">FIXED TEST {{ item.test_result.status }} · {{ item.test_result.passed_count }} passed / {{ item.test_result.failed_count }} failed · SNAPSHOT {{ shortDigest(item.test_result.snapshot_digest) }} · RUNTIME {{ shortDigest(item.test_result.runtime_digest) }} · OFFLINE</small>
                </li>
              </ol>
            </section>
          </details>
          <details v-if="workbench.replans?.replans?.length" class="agent-task-tree" open>
            <summary>
              REPLAN LINEAGE · IMMUTABLE GENERATIONS
              <template v-if="repairLoop"> · GEN {{ repairLoop.current_plan_generation }}/{{ repairLoop.maximum_plan_generations }}</template>
            </summary>
            <div v-if="repairLoop" class="repair-budget-proof">
              <small>
                TOTAL BUDGET · MODEL {{ repairLoop.budget_allocated.model_calls }}/{{ repairLoop.budget_limit.model_calls }}
                · TOOL {{ repairLoop.budget_allocated.tool_calls }}/{{ repairLoop.budget_limit.tool_calls }}
                · COST {{ repairLoop.budget_allocated.cost_micros }}/{{ repairLoop.budget_limit.cost_micros }} μ
              </small>
              <span
                role="progressbar"
                aria-label="跨计划代成本预算"
                :aria-valuenow="repairBudgetPercent"
                aria-valuemin="0"
                aria-valuemax="100"
              ><i :style="{ width: `${repairBudgetPercent}%` }" /></span>
            </div>
            <ol>
              <li v-for="item in workbench.replans?.replans ?? []" :key="item.replan_id">
                <div>
                  <b>GEN {{ item.source_plan_generation }} → {{ item.target_plan_generation }}</b>
                  <span>{{ item.status }} · {{ item.failure_snapshot.stable_error_code }}</span>
                </div>
                <small>FAILURE {{ shortDigest(item.failure_snapshot.snapshot_digest) }} · LINEAGE {{ shortDigest(item.replan_digest) }}</small>
                <small v-if="item.repair_advice">REPAIR {{ item.repair_advice.strategy_code }} · IMPORTS {{ item.repair_advice.result_sources.length }} · GRANTS {{ item.repair_advice.granted_capability_ids.length }}</small>
                <small v-if="item.budget_proof">BUDGET {{ item.budget_proof.allocated_after_activation.cost_micros }}/{{ item.budget_proof.budget_limit.cost_micros }} μ · REMAINING {{ item.budget_proof.remaining_after_activation.model_calls }} MODEL · {{ shortDigest(item.budget_proof.budget_digest) }}</small>
              </li>
            </ol>
          </details>
          <div v-if="inputRequests.some((item) => item.status === 'pending')" class="route-action">
            <strong>{{ inputRequests.find((item) => item.status === 'pending')?.question }}</strong>
            <span>你的下一条消息会建立新的不可变 Task，并绑定到这次输入请求。</span>
          </div>
          <div
            v-if="workbench.stage === 'needs_user_action' && workbench.route?.route_id === 'mcp_text_metrics'"
            class="route-action"
          >
            <strong>只读 Server 不会自动开启</strong>
            <button type="button" :disabled="Boolean(busyAction)" @click="enableMcpAndContinue">
              {{ busyAction === 'enable_mcp' ? '启用中…' : '启用只读 MCP 并继续' }}
            </button>
          </div>
          <section
            v-else-if="workspacePathOperationPreview"
            class="workspace-approval workspace-path-operation-approval"
            :data-state="busyAction === 'commit_workspace_path_operation' ? 'loading' : error ? 'error' : 'default'"
            aria-labelledby="workspace-path-operation-title"
          >
            <header>
              <div>
                <span>等待你的确认</span>
                <strong id="workspace-path-operation-title">
                  {{ workspacePathOperationPreview.operation === 'create'
                    ? `创建 ${workspacePathOperationPreview.target_path}`
                    : `将 ${workspacePathOperationPreview.source_path} 重命名为 ${workspacePathOperationPreview.target_path}` }}
                </strong>
              </div>
              <b>R1</b>
            </header>
            <dl>
              <div>
                <dt>要做什么</dt>
                <dd>{{ workspacePathOperationPreview.operation === 'create' ? '创建 1 个当前不存在的 UTF-8 文件' : '只改变 1 个文件的相对路径，不改变内容' }}</dd>
              </div>
              <div>
                <dt>影响什么</dt>
                <dd>{{ workspacePathOperationPreview.byte_count }} B · 不建目录、不覆盖同名文件</dd>
              </div>
              <div>
                <dt>凭什么</dt>
                <dd>
                  父目录 {{ shortDigest(workspacePathOperationPreview.expected_target_parent_version_digest) }}
                  · {{ workspacePathOperationPreview.operation === 'rename' ? `源版本 ${shortDigest(workspacePathOperationPreview.expected_source_version_digest)}` : `内容 ${shortDigest(workspacePathOperationPreview.proposed_content_digest)}` }}
                </dd>
              </div>
            </dl>
            <div v-if="workspacePathOperationPreview.operation === 'create'" class="workspace-diff" aria-label="新建文件内容预览">
              <div><span>目标</span><code>{{ workspacePathOperationPreview.target_path }}（当前不存在）</code></div>
              <div><span>拟写内容</span><code>{{ workspacePathOperationPreview.content || '（空文件）' }}</code></div>
            </div>
            <div v-else class="workspace-diff" aria-label="文件重命名预览">
              <div><span>原路径</span><code>{{ workspacePathOperationPreview.source_path }}</code></div>
              <div><span>新路径</span><code>{{ workspacePathOperationPreview.target_path }}（当前不存在）</code></div>
            </div>
            <footer>
              <small>不确认就不会写入；任何版本或目录变化都会使本次预览失效。</small>
              <div class="approval-actions">
                <button type="button" class="is-secondary" :disabled="Boolean(busyAction)" @click="stopExecution">拒绝</button>
                <button type="button" class="is-secondary" :disabled="Boolean(busyAction)" @click="editWorkspacePatch">改一下</button>
                <button type="button" :disabled="Boolean(busyAction)" @click="confirmWorkspacePathOperation">
                  {{ busyAction === 'commit_workspace_path_operation' ? '提交中…' : workspacePathOperationPreview.operation === 'create' ? '确认创建' : '确认重命名' }}
                </button>
              </div>
            </footer>
          </section>
          <section
            v-else-if="workspacePatchPreview"
            class="workspace-approval workspace-patch-approval"
            :data-state="busyAction === 'commit_workspace_patch' ? 'loading' : error ? 'error' : 'default'"
            aria-labelledby="workspace-patch-title"
          >
            <header>
              <div>
                <span>等待你的确认</span>
                <strong id="workspace-patch-title">{{ ['workspace_agent_patch_test', 'workspace_dynamic_patch_test'].includes(workbench?.route?.route_id ?? '') ? '提交 Agent 的单文件建议并运行固定测试' : `向 ${workspacePatchPreview.changes.length} 个文件提交一组精确替换` }}</strong>
              </div>
              <b>R1</b>
            </header>
            <dl>
              <div><dt>要做什么</dt><dd>按清单顺序替换 {{ workspacePatchPreview.changes.length }} 处完全匹配文本</dd></div>
              <div><dt>影响什么</dt><dd>{{ workspacePatchPreview.total_byte_count }} B · 每个原文件都会保留独立备份</dd></div>
              <div><dt>凭什么</dt><dd>隔离清单 {{ shortDigest(workspacePatchPreview.manifest_digest) }} · <a href="#workspace-patch-diffs">查看逐文件 diff</a></dd></div>
            </dl>
            <div id="workspace-patch-diffs" class="workspace-patch-diffs" aria-label="多文件替换前后对比">
              <article v-for="change in workspacePatchPreview.changes" :key="change.change_digest">
                <header>
                  <strong>{{ change.relative_path }}</strong>
                  <span>{{ change.byte_count }} B · v {{ shortDigest(change.expected_version_digest) }}</span>
                </header>
                <div class="workspace-diff">
                  <div><span>替换前</span><code>{{ change.old_text }}</code></div>
                  <div><span>替换后</span><code>{{ change.new_text || '（空文本）' }}</code></div>
                </div>
              </article>
            </div>
            <footer>
              <small>{{ ['workspace_agent_patch_test', 'workspace_dynamic_patch_test'].includes(workbench?.route?.route_id ?? '') ? '建议本身没有写权限；确认后若固定测试失败，会保留真实写入事实且不会自动继续修改。' : '若提交期间外部编辑造成部分完成，页面会明确列出已写文件与备份，不会伪报成功。' }}</small>
              <div class="approval-actions">
                <button type="button" class="is-secondary" :disabled="Boolean(busyAction)" @click="stopExecution">拒绝</button>
                <button type="button" class="is-secondary" :disabled="Boolean(busyAction)" @click="editWorkspacePatch">改一下</button>
                <button type="button" :disabled="Boolean(busyAction)" @click="confirmWorkspacePatch">
                  {{ busyAction === 'commit_workspace_patch' ? '提交并测试中…' : ['workspace_agent_patch_test', 'workspace_dynamic_patch_test'].includes(workbench?.route?.route_id ?? '') ? '确认补丁并测试' : '确认全部替换' }}
                </button>
              </div>
            </footer>
          </section>
          <section
            v-else-if="workspaceEditPreview"
            class="workspace-approval"
            :data-state="busyAction === 'commit_workspace_edit' ? 'loading' : error ? 'error' : 'default'"
            aria-labelledby="workspace-approval-title"
          >
            <header>
              <div>
                <span>等待你的确认</span>
                <strong id="workspace-approval-title">替换 {{ workspaceEditPreview.relative_path }} 中的一处文本</strong>
              </div>
              <b>R1</b>
            </header>
            <dl>
              <div><dt>要做什么</dt><dd>只替换 1 处完全匹配的文本</dd></div>
              <div><dt>影响什么</dt><dd>{{ workspaceEditPreview.byte_count }} B · 原文件会保留安全备份</dd></div>
              <div><dt>凭什么</dt><dd>版本 {{ shortDigest(workspaceEditPreview.expected_version_digest) }} · 确认 {{ shortDigest(workspaceEditPreview.confirmation_digest) }}</dd></div>
            </dl>
            <div class="workspace-diff" aria-label="替换前后文本">
              <div><span>替换前</span><code>{{ workspaceEditPreview.old_text }}</code></div>
              <div><span>替换后</span><code>{{ workspaceEditPreview.new_text || '（空文本）' }}</code></div>
            </div>
            <footer>
              <small>不确认就不会写入；文件版本变化时提交会被拒绝。</small>
              <button
                type="button"
                :disabled="Boolean(busyAction)"
                @click="confirmWorkspaceEdit"
              >{{ busyAction === 'commit_workspace_edit' ? '提交中…' : '确认替换并保留备份' }}</button>
            </footer>
          </section>
          <section
            v-else-if="workbench.workspace_edit?.schema_version === 'deskpilot.workspace-edit-receipt.v1'"
            class="workspace-approval"
            data-state="success"
            aria-label="工作区替换成功"
          >
            <header>
              <div><span>提交完成</span><strong>{{ workbench.workspace_edit.relative_path }}</strong></div>
              <b>✓</b>
            </header>
            <dl>
              <div><dt>安全备份</dt><dd>{{ workbench.workspace_edit.backup_relative_path }}</dd></div>
              <div><dt>提交回执</dt><dd>{{ shortDigest(workbench.workspace_edit.receipt_digest) }}</dd></div>
            </dl>
          </section>
          <section
            v-else-if="workbench.workspace_path_operation?.schema_version === 'deskpilot.workspace-path-operation-receipt.v1'"
            class="workspace-approval workspace-path-operation-approval"
            data-state="success"
            aria-label="工作区路径操作成功"
          >
            <header>
              <div>
                <span>提交完成</span>
                <strong>{{ workbench.workspace_path_operation.operation === 'create' ? '文件已创建' : '文件已重命名' }}</strong>
              </div>
              <b>✓</b>
            </header>
            <dl>
              <div v-if="workbench.workspace_path_operation.source_path"><dt>原路径</dt><dd>{{ workbench.workspace_path_operation.source_path }}</dd></div>
              <div><dt>当前路径</dt><dd>{{ workbench.workspace_path_operation.target_path }}</dd></div>
              <div><dt>内容摘要</dt><dd>{{ shortDigest(workbench.workspace_path_operation.content_digest) }}</dd></div>
              <div><dt>提交回执</dt><dd>{{ shortDigest(workbench.workspace_path_operation.receipt_digest) }}</dd></div>
            </dl>
          </section>
          <section
            v-else-if="workbench.workspace_patch?.schema_version === 'deskpilot.workspace-patch-receipt.v1'"
            class="workspace-approval workspace-patch-approval"
            :data-state="workbench.workspace_patch.status === 'partial' ? 'error' : 'success'"
            :aria-label="workbench.workspace_patch.status === 'partial' ? '工作区补丁部分完成' : '工作区补丁成功'"
          >
            <header>
              <div>
                <span>{{ workbench.workspace_patch.status === 'partial' ? '部分完成' : '提交完成' }}</span>
                <strong>已写入 {{ workbench.workspace_patch.change_receipts.length }} 个文件</strong>
              </div>
              <b>{{ workbench.workspace_patch.status === 'partial' ? '!' : '✓' }}</b>
            </header>
            <dl>
              <div v-for="item in workbench.workspace_patch.change_receipts" :key="item.receipt_digest">
                <dt>{{ item.relative_path }}</dt><dd>备份 {{ item.backup_relative_path }}</dd>
              </div>
              <div v-if="workbench.workspace_patch.failed_path"><dt>未完成</dt><dd>{{ workbench.workspace_patch.failed_path }} · {{ workbench.workspace_patch.error_code }}</dd></div>
              <div><dt>补丁回执</dt><dd>{{ shortDigest(workbench.workspace_patch.receipt_digest) }}</dd></div>
            </dl>
          </section>
          <section v-if="canConditionReplan" class="route-action condition-replan-action" aria-label="测试失败后的新计划确认">
            <div>
              <span>TEST CONDITION BLOCKED</span>
              <strong>测试未通过，是否生成第 {{ (repairLoop?.current_plan_generation ?? 1) + 1 }}/{{ repairLoop?.maximum_plan_generations ?? 2 }} 代修复计划？</strong>
              <small>
                FALSE {{ shortDigest(failedConditionDecision?.decision_digest) }} ·
                旧补丁已发生且仍可审计；新补丁会产生不同的确认摘要。
                剩余 {{ repairLoop?.remaining_replans ?? 1 }} 次，也可直接输入“继续修复”。
              </small>
            </div>
            <button
              type="button"
              :disabled="Boolean(busyAction)"
              @click="replanFailedExecution"
            >{{ busyAction === 'replan_failed_execution' ? '正在封存并换代…' : `生成第 ${(repairLoop?.current_plan_generation ?? 1) + 1} 代` }}</button>
          </section>
          <section v-else-if="conditionReplanLimitReached" class="route-action condition-replan-action" aria-label="修复计划代上限已达到">
            <div>
              <span>TEST CONDITION BLOCKED · LIMIT REACHED</span>
              <strong>当前任务已到 Plan {{ repairLoop?.current_plan_generation }}/{{ repairLoop?.maximum_plan_generations }}，不会继续换代</strong>
              <small>
                {{ repairLoop?.reason_code }} · 已分配 MODEL {{ repairLoop?.budget_allocated.model_calls }}/{{ repairLoop?.budget_limit.model_calls }}
                · COST {{ repairLoop?.budget_allocated.cost_micros }}/{{ repairLoop?.budget_limit.cost_micros }} μ。
                旧 Plan、补丁和确认保持可审计；如需继续，请发送一条完整的新任务指令。
              </small>
            </div>
          </section>
          <ol v-if="routeFlow.length">
            <li
              v-for="(item, index) in routeFlow"
              :key="item.action"
              :data-state="nodeState(nodeMap.get(item.key), item.action)"
            >
              <span>{{ String(index + 1).padStart(2, '0') }}</span>
              <div><strong>{{ item.label }}</strong><small>{{ item.detail }}</small></div>
              <b>{{ nodeState(nodeMap.get(item.key), item.action) === 'done' ? '完成' : nodeState(nodeMap.get(item.key), item.action) === 'active' ? '执行中' : '排队' }}</b>
            </li>
          </ol>
          <div v-else class="route-notice">
            <strong>{{ workbench.stage === 'unsupported' ? '未创建可执行 Run' : '等待你补充一条更具体的指令' }}</strong>
            <small>Reason {{ workbench.route?.reason_code ?? '无' }}</small>
          </div>
        </article>
      </section>

      <form class="agent-composer" @submit.prevent="submitTurn">
        <label for="agent-prompt">{{ workbench ? '继续调整任务' : '发送任务' }}</label>
        <textarea
          id="agent-prompt"
          ref="promptInput"
          v-model="prompt"
          rows="3"
          maxlength="4000"
          :placeholder="workbench ? '补充新要求；发送后会停止未完成的旧运行并重新规划' : '例如：研究本地优先 Agent 的设计方法，生成带来源的 HTML 报告'"
          @keydown.ctrl.enter.prevent="submitTurn"
          @keydown.meta.enter.prevent="submitTurn"
        />
        <footer>
          <span>Ctrl / ⌘ + Enter 发送</span>
          <button type="submit" :disabled="Boolean(busyAction && busyAction !== 'auto') || !prompt.trim()">
            {{ busyAction === 'auto' ? '替换当前任务' : workbench ? '发送新指令' : '开始任务' }}
          </button>
        </footer>
      </form>
    </main>

    <aside class="evidence-panel">
      <header>
        <div><h2>证据与交付</h2></div>
        <strong>{{ evidenceCount }}</strong>
      </header>
      <p class="panel-intro">这里是结果的证据层，不是操作主入口。</p>

      <details v-if="workbench?.turn_planning" open>
        <summary>Turn Planner Proof <span>{{ workbench.turn_planning.run.status }}</span></summary>
        <dl class="route-proof">
          <div><dt>预编译 Offer</dt><dd>{{ workbench.turn_planning.run.offer_count }} 个</dd></div>
          <div><dt>Planner 状态</dt><dd>{{ workbench.turn_planning.run.status }}</dd></div>
          <div><dt>请求证明</dt><dd>{{ shortDigest(workbench.turn_planning.run.request_digest) }}</dd></div>
          <div><dt>运行证明</dt><dd>{{ shortDigest(workbench.turn_planning.run.run_digest) }}</dd></div>
          <div v-if="workbench.turn_planning.run.failure">
            <dt>失败证明</dt>
            <dd>{{ workbench.turn_planning.run.failure.error_code }} · {{ shortDigest(workbench.turn_planning.run.failure.failure_digest) }} · 不自动重放</dd>
          </div>
          <div v-if="workbench.turn_planning.adjudication">
            <dt>服务器裁决</dt>
            <dd>{{ workbench.turn_planning.adjudication.outcome }} · {{ workbench.turn_planning.adjudication.reason_code }} · {{ shortDigest(workbench.turn_planning.adjudication.adjudication_digest) }}</dd>
          </div>
          <div v-if="workbench.turn_planning.binding">
            <dt>计划绑定</dt>
            <dd>{{ workbench.turn_planning.binding.status }} · {{ shortDigest(workbench.turn_planning.binding.binding_digest) }}</dd>
          </div>
          <div><dt>投影证明</dt><dd>{{ shortDigest(workbench.turn_planning.planning_digest) }}</dd></div>
        </dl>
        <p class="empty-proof">这里只显示服务器证明摘要；模型原始响应、参数原文与用户消息不会作为权限展示，失败也不会自动重放。</p>
      </details>

      <details v-if="workbench?.route" open>
        <summary>Route Receipt <span>{{ workbench.route.decision }}</span></summary>
        <dl class="route-proof">
          <div><dt>能力路由</dt><dd>{{ routeName }}</dd></div>
          <div><dt>判定摘要</dt><dd>{{ shortDigest(workbench.route.candidate_digest) }}</dd></div>
          <div><dt>参数摘要</dt><dd>{{ shortDigest(workbench.route.parameter_digest) }}</dd></div>
          <div v-if="workbench.route.turn_planning_provenance_digest"><dt>Planner 来源证明</dt><dd>{{ shortDigest(workbench.route.turn_planning_provenance_digest) }}</dd></div>
          <div v-if="workbench.route.resolution_digest"><dt>对话补全证明</dt><dd>{{ shortDigest(workbench.route.resolution_digest) }}</dd></div>
          <div><dt>结果摘要</dt><dd>{{ shortDigest(workbench.route.result_digest) }}</dd></div>
        </dl>
      </details>

      <details v-if="workbench?.route?.route_id === 'knowledge_lookup'" open>
        <summary>Knowledge Evidence <span>{{ workbench.knowledge?.citations.length ?? 0 }}</span></summary>
        <article v-for="citation in workbench.knowledge?.citations ?? []" :key="citation.retrieval_proof_digest" class="knowledge-card">
          <strong>{{ citation.canonical_path }}</strong>
          <p>{{ citation.snippet }}</p>
          <small>{{ citation.locator }} · {{ shortDigest(citation.retrieval_proof_digest) }}</small>
        </article>
        <p v-if="!workbench.knowledge" class="empty-proof">执行后显示内容寻址片段与检索证明。</p>
      </details>

      <details v-if="workbench?.route?.route_id === 'mcp_text_metrics'" open>
        <summary>MCP Audit Proof <span>{{ workbench.mcp ? '已记录' : '无' }}</span></summary>
        <dl v-if="workbench.mcp" class="mcp-proof">
          <div v-for="([key, value]) in metricEntries" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></div>
          <div><dt>request</dt><dd>{{ shortDigest(workbench.mcp.request_digest) }}</dd></div>
          <div><dt>result</dt><dd>{{ shortDigest(workbench.mcp.result_digest) }}</dd></div>
          <div><dt>audit</dt><dd>{{ shortDigest(workbench.mcp.audit_event_id) }}</dd></div>
        </dl>
        <p v-else class="empty-proof">明确启用只读 Server 后，调用结果与审计事件在这里闭合。</p>
      </details>

      <details v-if="workbench?.route?.route_id === 'workspace_file_read'" open>
        <summary>Workspace File <span>{{ workbench.workspace_file ? '已读取' : '无' }}</span></summary>
        <article v-if="workbench.workspace_file" class="workspace-file-proof">
          <strong>{{ workbench.workspace_file.relative_path }}</strong>
          <small>{{ workbench.workspace_file.byte_count }} B · version {{ shortDigest(workbench.workspace_file.version_digest) }}</small>
          <pre>{{ workbench.workspace_file.content }}</pre>
        </article>
        <p v-else class="empty-proof">执行后显示文件内容、版本与内容摘要。</p>
      </details>

      <details v-if="workbench && ['workspace_directory_list', 'workspace_directory_analyze'].includes(workbench.route?.route_id ?? '')" open>
        <summary>Workspace Directory <span>{{ workbench.workspace_directory ? `${workbench.workspace_directory.entries.length} 项` : '无' }}</span></summary>
        <template v-if="workbench.workspace_directory">
          <dl class="route-proof">
            <div><dt>目录</dt><dd>{{ workbench.workspace_directory.relative_path }}</dd></div>
            <div><dt>完整度</dt><dd>{{ workbench.workspace_directory.truncated ? '已按上限截断' : '完整' }}</dd></div>
            <div><dt>结果摘要</dt><dd>{{ shortDigest(workbench.workspace_directory.result_digest) }}</dd></div>
          </dl>
          <ul class="workspace-entry-list" aria-label="工作区目录子项">
            <li v-for="item in workbench.workspace_directory.entries" :key="item.relative_path">
              <span>{{ item.kind === 'directory' ? '目录' : `${item.byte_count} B` }}</span>
              <strong>{{ item.relative_path }}</strong>
              <small>{{ shortDigest(item.version_digest) }}</small>
            </li>
          </ul>
        </template>
        <p v-else class="empty-proof">执行后显示直接子项、类型与版本摘要。</p>
      </details>

      <details v-if="workbench?.route?.route_id === 'workspace_snapshot_check'" open>
        <summary>Workspace Check <span>{{ workbench.workspace_check?.status ?? '无' }}</span></summary>
        <template v-if="workbench.workspace_check">
          <dl class="route-proof">
            <div><dt>Profile</dt><dd>{{ workbench.workspace_check.profile }}</dd></div>
            <div><dt>快照范围</dt><dd>{{ workbench.workspace_check.relative_path }}</dd></div>
            <div><dt>已检查</dt><dd>{{ workbench.workspace_check.checked_file_count }} 个文件</dd></div>
            <div><dt>隔离</dt><dd>AppContainer · 断网</dd></div>
            <div><dt>快照摘要</dt><dd>{{ shortDigest(workbench.workspace_check.snapshot_digest) }}</dd></div>
          </dl>
          <ul v-if="workbench.workspace_check.issues.length" class="workspace-issue-list" aria-label="工作区检查问题">
            <li v-for="issue in workbench.workspace_check.issues" :key="`${issue.relative_path}:${issue.line}:${issue.column}`">
              <strong>{{ issue.relative_path }}:{{ issue.line }}:{{ issue.column }}</strong>
              <span>{{ issue.message }}</span>
              <small>{{ issue.code }}</small>
            </li>
          </ul>
          <p v-else class="workspace-check-pass">固定解析检查通过；未执行仓库代码。</p>
        </template>
        <p v-else class="empty-proof">执行后显示内容快照、隔离边界与逐项解析结果。</p>
      </details>

      <details v-if="workbench?.route?.route_id === 'workspace_python_test'" open>
        <summary>Python Test <span>{{ workbench.workspace_python_test?.status ?? '无' }}</span></summary>
        <template v-if="workbench.workspace_python_test">
          <dl class="route-proof">
            <div><dt>测试文件</dt><dd>{{ workbench.workspace_python_test.test_path }}</dd></div>
            <div><dt>结果</dt><dd>{{ workbench.workspace_python_test.passed_count }} passed · {{ workbench.workspace_python_test.failed_count }} failed</dd></div>
            <div><dt>用时</dt><dd>{{ workbench.workspace_python_test.duration_ms }} ms</dd></div>
            <div><dt>隔离</dt><dd>AppContainer · 断网 · 1 process</dd></div>
            <div><dt>快照摘要</dt><dd>{{ shortDigest(workbench.workspace_python_test.snapshot_digest) }}</dd></div>
            <div><dt>运行时摘要</dt><dd>{{ shortDigest(workbench.workspace_python_test.runtime_digest) }}</dd></div>
          </dl>
          <pre class="python-test-output">{{ workbench.workspace_python_test.output }}</pre>
          <p v-if="workbench.workspace_python_test.output_truncated" class="empty-proof">输出已按安全上限保留首尾。</p>
        </template>
        <p v-else class="empty-proof">执行后显示 pytest 结果、快照与隔离证明。</p>
      </details>

      <details v-if="workbench?.route?.route_id === 'workspace_node_test'" open>
        <summary>Node Test <span>{{ workbench.workspace_node_test?.status ?? '无' }}</span></summary>
        <template v-if="workbench.workspace_node_test">
          <dl class="route-proof">
            <div><dt>测试文件</dt><dd>{{ workbench.workspace_node_test.test_path }}</dd></div>
            <div><dt>结果</dt><dd>{{ workbench.workspace_node_test.passed_count }} passed · {{ workbench.workspace_node_test.failed_count }} failed</dd></div>
            <div><dt>用时</dt><dd>{{ workbench.workspace_node_test.duration_ms }} ms</dd></div>
            <div><dt>隔离</dt><dd>AppContainer · 断网 · 1 process</dd></div>
            <div><dt>快照摘要</dt><dd>{{ shortDigest(workbench.workspace_node_test.snapshot_digest) }}</dd></div>
            <div><dt>运行时摘要</dt><dd>{{ shortDigest(workbench.workspace_node_test.runtime_digest) }}</dd></div>
          </dl>
          <pre class="python-test-output">{{ workbench.workspace_node_test.output }}</pre>
          <p v-if="workbench.workspace_node_test.output_truncated" class="empty-proof">输出已按安全上限保留首尾。</p>
        </template>
        <p v-else class="empty-proof">执行后显示 node:test 结果、快照与隔离证明。</p>
      </details>

      <details v-if="workbench?.route?.route_id === 'workspace_file_replace'" open>
        <summary>Workspace Edit <span>{{ workbench.workspace_edit?.schema_version.endsWith('receipt.v1') ? '已提交' : '待确认' }}</span></summary>
        <dl v-if="workbench.workspace_edit" class="route-proof">
          <div><dt>文件</dt><dd>{{ workbench.workspace_edit.relative_path }}</dd></div>
          <template v-if="workbench.workspace_edit.schema_version === 'deskpilot.workspace-edit-receipt.v1'">
            <div><dt>安全备份</dt><dd>{{ workbench.workspace_edit.backup_relative_path }}</dd></div>
            <div><dt>新版本</dt><dd>{{ shortDigest(workbench.workspace_edit.version_digest) }}</dd></div>
            <div><dt>提交回执</dt><dd>{{ shortDigest(workbench.workspace_edit.receipt_digest) }}</dd></div>
          </template>
          <template v-else>
            <div><dt>原版本</dt><dd>{{ shortDigest(workbench.workspace_edit.expected_version_digest) }}</dd></div>
            <div><dt>拟写内容</dt><dd>{{ shortDigest(workbench.workspace_edit.proposed_content_digest) }}</dd></div>
          </template>
        </dl>
      </details>

      <details v-if="workbench && ['workspace_patch_bundle', 'workspace_agent_patch_test', 'workspace_dynamic_patch_test'].includes(workbench.route?.route_id ?? '')" open>
        <summary>Workspace Patch <span>{{ workbench.workspace_patch?.schema_version.endsWith('receipt.v1') ? '已提交' : '待确认' }}</span></summary>
        <dl v-if="workbench.workspace_patch" class="route-proof">
          <template v-if="workbench.workspace_patch.schema_version === 'deskpilot.workspace-patch-receipt.v1'">
            <div><dt>状态</dt><dd>{{ workbench.workspace_patch.status }}</dd></div>
            <div><dt>已写文件</dt><dd>{{ workbench.workspace_patch.change_receipts.length }}</dd></div>
            <div><dt>提交回执</dt><dd>{{ shortDigest(workbench.workspace_patch.receipt_digest) }}</dd></div>
          </template>
          <template v-else>
            <div><dt>文件数</dt><dd>{{ workbench.workspace_patch.changes.length }}</dd></div>
            <div><dt>隔离副本</dt><dd>{{ workbench.workspace_patch.staging_workspace_ref }}</dd></div>
            <div><dt>清单摘要</dt><dd>{{ shortDigest(workbench.workspace_patch.manifest_digest) }}</dd></div>
          </template>
        </dl>
      </details>

      <details v-if="workbench?.route?.route_id === 'workspace_file_create' || workbench?.route?.route_id === 'workspace_file_rename'" open>
        <summary>Workspace Path Receipt <span>{{ workbench.workspace_path_operation?.schema_version.endsWith('receipt.v1') ? '已提交' : '待确认' }}</span></summary>
        <dl v-if="workbench.workspace_path_operation" class="route-proof">
          <div><dt>操作</dt><dd>{{ workbench.workspace_path_operation.operation }}</dd></div>
          <div v-if="workbench.workspace_path_operation.source_path"><dt>源路径</dt><dd>{{ workbench.workspace_path_operation.source_path }}</dd></div>
          <div><dt>目标路径</dt><dd>{{ workbench.workspace_path_operation.target_path }}</dd></div>
          <template v-if="workbench.workspace_path_operation.schema_version === 'deskpilot.workspace-path-operation-receipt.v1'">
            <div><dt>结果版本</dt><dd>{{ shortDigest(workbench.workspace_path_operation.version_digest) }}</dd></div>
            <div><dt>提交回执</dt><dd>{{ shortDigest(workbench.workspace_path_operation.receipt_digest) }}</dd></div>
          </template>
          <template v-else>
            <div><dt>父目录版本</dt><dd>{{ shortDigest(workbench.workspace_path_operation.expected_target_parent_version_digest) }}</dd></div>
            <div><dt>确认摘要</dt><dd>{{ shortDigest(workbench.workspace_path_operation.confirmation_digest) }}</dd></div>
          </template>
        </dl>
      </details>

      <details v-if="!workbench?.route || workbench.route.route_id === 'research_to_html'" open>
        <summary>Claim / Citation <span>{{ workbench?.research?.claims.length ?? 0 }}</span></summary>
        <article v-for="claim in workbench?.research?.claims ?? []" :key="claim.claim_id" class="claim-card">
          <div>
            <i :data-verdict="verdictMap.get(claim.claim_id)?.outcome ?? 'pending'" />
            <strong>{{ verdictMap.get(claim.claim_id)?.outcome ?? '待核验' }}</strong>
          </div>
          <p>{{ claim.statement }}</p>
          <small>{{ claim.citation_ids.length }} 条引用 · {{ shortDigest(claim.claim_id) }}</small>
        </article>
        <p v-if="!workbench?.research?.claims.length" class="empty-proof">完成取证后显示可核验事实。</p>
      </details>

      <details v-if="!workbench?.route || workbench.route.route_id === 'research_to_html'" open>
        <summary>Artifact Workspace <span>{{ workbench?.workspace?.artifacts.length ?? 0 }}</span></summary>
        <article v-for="artifact in workbench?.workspace?.artifacts ?? []" :key="artifact.artifact_id" class="artifact-card">
          <strong>{{ artifact.relative_path }}</strong><span>{{ artifact.active_revision.byte_count }} B</span>
          <small>Revision {{ shortDigest(artifact.active_revision.revision_id) }}</small>
          <small>PatchReceipt {{ shortDigest(artifact.active_revision.patch_receipt_id) }}</small>
          <small v-if="artifact.active_revision.pdf_render_verification">
            PDF Render {{ artifact.active_revision.pdf_render_verification.page_count }} 页 ·
            {{ artifact.active_revision.pdf_render_verification.render_dpi }} DPI ·
            {{ shortDigest(artifact.active_revision.pdf_render_verification.evidence_digest) }}
          </small>
        </article>
        <p v-if="!workbench?.workspace" class="empty-proof">验证事实解锁后才建立工作区。</p>
      </details>

      <details v-if="!workbench?.route || workbench.route.route_id === 'research_to_html'" open>
        <summary>Browser Verifier <span>{{ workbench?.browser?.status ?? '无' }}</span></summary>
        <dl v-if="workbench?.browser" class="browser-proof">
          <div><dt>外部请求</dt><dd>{{ workbench.browser.external_request_count }}</dd></div>
          <div><dt>控制台错误</dt><dd>{{ workbench.browser.console_error_count }}</dd></div>
          <div><dt>页面错误</dt><dd>{{ workbench.browser.page_error_count }}</dd></div>
          <div><dt>视口</dt><dd>{{ workbench.browser.viewport_width }}×{{ workbench.browser.viewport_height }}</dd></div>
        </dl>
        <p v-else class="empty-proof">Artifact 完成后进行断网验收。</p>
      </details>

      <section v-if="workbench?.delivery" class="export-section" aria-labelledby="export-heading">
        <span class="eyebrow">EXACT EXPORT</span><h3 id="export-heading">导出交付物</h3>
        <p>目标存在时拒绝覆盖；预览不会写入。</p>
        <div class="export-form">
          <label for="export-artifact">选择交付物</label>
          <select id="export-artifact" v-model="selectedArtifactId">
            <option
              v-for="artifact in exportArtifacts"
              :key="artifact.artifact_id"
              :value="artifact.artifact_id"
            >{{ artifact.relative_path }} · {{ artifact.active_revision.media_type }}</option>
          </select>
          <label for="export-target">绝对目标路径</label>
          <input id="export-target" v-model="targetPath" :placeholder="exportPathPlaceholder" />
          <button type="button" :disabled="Boolean(busyAction) || !selectedArtifactId || !targetPath.trim()" @click="previewExport">
            {{ busyAction === 'prepare_export' ? '校验中…' : '预览写入' }}
          </button>
        </div>
        <article v-if="exportPreview" class="export-receipt" :data-status="exportPreview.status">
          <strong>{{ exportPreview.target_path }}</strong>
          <small>{{ exportPreview.byte_count }} B · {{ shortDigest(exportPreview.source_digest) }}</small>
          <button
            v-if="exportPreview.status === 'prepared'"
            class="confirm-write"
            type="button"
            :disabled="Boolean(busyAction)"
            @click="commitExport"
          >{{ busyAction === 'commit_export' ? '写入中…' : '确认写入此路径' }}</button>
          <span v-else>不可变导出回执 · {{ shortDigest(exportPreview.receipt_digest) }}</span>
        </article>
      </section>
    </aside>
  </section>
</template>

<style scoped>
/* finesse · register=product-agent-workbench · A=warm-paper+forest+vermilion · B=humanist-sans+mono-proof · C=conversation-stream+evidence-folio · D=feedback-motion+single-live-signal · E=field-notebook · SOUL=7 SPECTACLE=2 DENSITY=8 */
:global(:root) {
  --aw-paper: #e7e4da; --aw-paper-deep: #d9d5c9; --aw-sheet: #f0ede4;
  --aw-ink: #21302c; --aw-ink-muted: #64706a; --aw-forest: #1f4b3f;
  --aw-forest-deep: #16372f; --aw-forest-pale: #d6dfd8; --aw-vermilion: #b6472d;
  --aw-vermilion-pale: #ead5cc; --aw-line: #b8b5aa; --aw-line-strong: #898c84;
  --aw-shadow: rgba(33, 48, 44, 0.16); --aw-overlay: rgba(240, 237, 228, 0.82);
  --aw-font-ui: "Segoe UI", "Microsoft YaHei UI", sans-serif;
  --aw-font-proof: ui-monospace, "Cascadia Mono", monospace;
}

.agent-workbench {
  min-height: calc(100dvh - 108px); display: grid;
  grid-template-columns: 232px minmax(480px, 1fr) minmax(260px, 350px);
  overflow: hidden; border: 1px solid var(--aw-line-strong); background: var(--aw-paper);
  color: var(--aw-ink); box-shadow: 0 22px 60px var(--aw-shadow); font-family: var(--aw-font-ui);
}
.eyebrow { color: var(--aw-vermilion); font: 750 10px/1.2 var(--aw-font-proof); letter-spacing: 0.13em; }
.session-rail {
  display: flex; flex-direction: column; gap: 28px; padding: 26px 20px 20px;
  background: var(--aw-forest-deep); color: var(--aw-sheet); border-right: 1px solid var(--aw-line-strong);
}
.session-rail h2, .conversation-header h2, .evidence-panel h2, .run-card h3,
.empty-conversation h3, .export-section h3 { margin: 0; }
.session-rail header h2 { margin-top: 8px; font-size: 25px; letter-spacing: -0.04em; }
.session-rail header p, .autonomy-card p { margin: 8px 0 0; color: var(--aw-paper-deep); font-size: 12px; line-height: 1.6; }
.session-rail nav { display: grid; }
.session-rail nav > span, .autonomy-card label { margin-bottom: 8px; color: var(--aw-paper-deep); font-size: 10px; font-weight: 750; letter-spacing: 0.08em; }
.session-rail nav button {
  min-height: 44px; padding: 12px 0; border: 0; border-top: 1px solid var(--aw-line-strong); background: transparent;
  color: var(--aw-sheet); text-align: left; font: 12px/1.45 var(--aw-font-ui); cursor: pointer;
}
.session-rail nav button:last-child { border-bottom: 1px solid var(--aw-line-strong); }
.session-rail nav button:hover { color: var(--aw-vermilion-pale); }
.session-rail nav button:focus-visible, .session-rail select:focus-visible, .run-controls button:focus-visible,
.agent-composer textarea:focus-visible, .agent-composer button:focus-visible, .export-form input:focus-visible,
.export-form select:focus-visible,
.export-form button:focus-visible, .confirm-write:focus-visible, .route-action button:focus-visible { outline: 2px solid var(--aw-vermilion); outline-offset: 2px; }
.workspace-approval button:focus-visible { outline: 2px solid var(--aw-forest-deep); outline-offset: 3px; }
.autonomy-card { padding-top: 16px; border-top: 1px solid var(--aw-line-strong); }
.autonomy-card > div { display: grid; gap: 4px; }
.autonomy-card span { color: var(--aw-paper-deep); font-size: 10px; }
.autonomy-card strong { font-size: 13px; }
.autonomy-card label { display: block; margin: 16px 0 7px; }
.autonomy-card select {
  width: 100%; min-height: 44px; border: 1px solid var(--aw-line-strong); border-radius: 2px;
  background: var(--aw-forest); color: var(--aw-sheet); padding: 0 9px;
}
.session-fingerprint { margin-top: auto; display: grid; gap: 5px; padding-top: 16px; border-top: 1px solid var(--aw-line-strong); }
.session-fingerprint span { color: var(--aw-paper-deep); font-size: 9px; }
.session-fingerprint code { margin-bottom: 5px; color: var(--aw-sheet); font: 10px/1.3 var(--aw-font-proof); }

.conversation-column {
  min-width: 0; min-height: 0; display: grid; grid-template-rows: auto auto minmax(240px, 1fr) auto;
  background: var(--aw-sheet);
}
.conversation-header {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 20px;
  padding: 24px 28px 20px; border-bottom: 1px solid var(--aw-line);
}
.conversation-header h2 { margin-top: 7px; max-width: 680px; font-size: clamp(20px, 2vw, 30px); line-height: 1.1; letter-spacing: -0.035em; }
.run-controls { display: flex; align-items: center; gap: 10px; flex: none; }
.run-state { display: inline-flex; align-items: center; gap: 7px; color: var(--aw-ink-muted); font-size: 11px; font-weight: 750; }
.run-state i { width: 8px; height: 8px; border: 1px solid var(--aw-line-strong); border-radius: 50%; background: var(--aw-paper-deep); }
.run-state[data-live='true'] i { border-color: var(--aw-forest); background: var(--aw-forest); animation: live-signal 1.3s ease-in-out infinite; }
.run-controls button {
  min-height: 44px; border: 1px solid var(--aw-vermilion); border-radius: 2px; background: transparent;
  color: var(--aw-vermilion); padding: 0 12px; font-weight: 750; cursor: pointer;
}
.run-controls button:disabled { opacity: 0.3; cursor: not-allowed; }
.workbench-error { margin: 14px 28px 0; padding: 11px 12px; border: 1px solid var(--aw-vermilion); background: var(--aw-vermilion-pale); color: var(--aw-ink); font-size: 12px; }
.transcript { min-height: 0; overflow-y: auto; padding: 28px; }
.empty-conversation { min-height: 290px; display: grid; align-content: center; justify-items: start; max-width: 600px; }
.empty-mark { display: grid; place-items: center; width: 42px; height: 42px; border: 1px solid var(--aw-forest); color: var(--aw-forest); font: 800 11px/1 var(--aw-font-proof); }
.empty-conversation h3 { margin-top: 18px; font-size: clamp(24px, 3vw, 38px); line-height: 1.06; letter-spacing: -0.045em; }
.empty-conversation p { max-width: 530px; margin: 13px 0 0; color: var(--aw-ink-muted); font-size: 14px; line-height: 1.7; }
.message { display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 12px; max-width: 760px; margin: 0 0 22px; }
.message-author { display: grid; place-items: center; width: 30px; height: 30px; border: 1px solid var(--aw-line-strong); color: var(--aw-ink-muted); font: 750 10px/1 var(--aw-font-proof); }
.message[data-role='assistant'] .message-author { border-color: var(--aw-forest); background: var(--aw-forest); color: var(--aw-sheet); }
.message > div { padding-top: 1px; }
.message strong { font-size: 11px; }
.message p { margin: 5px 0 0; font-size: 14px; line-height: 1.65; white-space: pre-wrap; }
.message time { display: block; margin-top: 5px; color: var(--aw-ink-muted); font: 9px/1 var(--aw-font-proof); }

.run-card { max-width: 760px; margin: 30px 0 12px 46px; border: 1px solid var(--aw-line-strong); background: var(--aw-paper); }
.run-card > header { display: flex; justify-content: space-between; gap: 16px; padding: 15px 17px; border-bottom: 1px solid var(--aw-line); }
.run-card h3 { margin-top: 5px; font-size: 17px; }
.run-card > header > span { color: var(--aw-ink-muted); font: 10px/1.3 var(--aw-font-proof); }
.run-card > p { margin: 0; padding: 13px 17px; border-bottom: 1px solid var(--aw-line); color: var(--aw-ink-muted); font-size: 12px; line-height: 1.55; }
.model-loop-proof { padding: 12px 17px; border-bottom: 1px solid var(--aw-line); background: var(--aw-forest-pale); }
.model-loop-proof > span { color: var(--aw-ink-muted); font: 800 9px/1 var(--aw-font-proof); letter-spacing: .12em; }
.run-card .model-loop-proof ol { display: grid; gap: 7px; margin: 9px 0 0; padding: 0; list-style: none; }
.run-card .model-loop-proof li { min-height: 0; display: grid; grid-template-columns: 62px minmax(0, 1fr) auto; gap: 8px; border: 0; font-size: 10px; }
.model-loop-proof b, .model-loop-proof code { font: 9px/1.3 var(--aw-font-proof); }
.agent-task-tree { padding: 13px 17px; border-bottom: 1px solid var(--aw-line); }
.agent-task-tree summary { cursor: pointer; color: var(--aw-forest-deep); font: 800 9px/1.3 var(--aw-font-proof); letter-spacing: .08em; }
.agent-task-tree ol { display: grid; gap: 7px; margin: 10px 0 0; padding: 0; list-style: none; }
.agent-task-tree li { display: grid; gap: 4px; padding: 8px 9px; border: 1px solid var(--aw-line); }
.agent-task-tree li div { display: flex; justify-content: space-between; gap: 12px; }
.agent-task-tree li b, .agent-task-tree li span, .agent-task-tree li small, .agent-task-tree p { overflow-wrap: anywhere; font: 9px/1.45 var(--aw-font-proof); }
.agent-task-tree li span, .agent-task-tree li small { color: var(--aw-ink-muted); }
.repair-budget-proof { display: grid; gap: 6px; margin-top: 10px; }
.repair-budget-proof small { color: var(--aw-ink-muted); font: 9px/1.45 var(--aw-font-proof); }
.repair-budget-proof > span { display: block; height: 3px; overflow: hidden; background: var(--aw-line); }
.repair-budget-proof i { display: block; height: 100%; background: var(--aw-vermilion); }
.dynamic-task-graph { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--aw-line-strong); }
.dynamic-task-graph > p { margin: 0; color: var(--aw-forest-deep); }
.route-action { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 17px; border-bottom: 1px solid var(--aw-line); background: var(--aw-vermilion-pale); }
.route-action > div { min-width: 0; display: grid; gap: 4px; }
.route-action span { color: var(--aw-vermilion); font: 750 9px/1.2 var(--aw-font-proof); letter-spacing: .08em; }
.route-action strong { font-size: 11px; }
.route-action small { color: var(--aw-ink-muted); overflow-wrap: anywhere; font: 9px/1.45 var(--aw-font-proof); }
.route-action button { min-height: 44px; flex: none; white-space: nowrap; border: 1px solid var(--aw-vermilion); border-radius: 2px; outline: 2px solid transparent; background: var(--aw-vermilion); color: var(--aw-sheet); padding: 0 13px; font-weight: 750; cursor: pointer; }
.route-action button:disabled { opacity: 0.38; cursor: not-allowed; }
@media (hover: hover) { .route-action button:hover { background: var(--aw-forest); border-color: var(--aw-forest); } }
.route-action button:active { transform: translateY(1px); }
/* finesse · component: workspace-approval · register=product
 * states: default · hover · focus-visible · active · disabled · loading · error · success
 * tokens: inherited (ResearchArtifactWorkbench.vue :root) */
.workspace-approval {
  display: grid; gap: 13px; padding: 16px 17px; border-bottom: 1px solid var(--aw-vermilion);
  background: var(--aw-vermilion-pale);
}
.workspace-approval > header, .workspace-approval > footer {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
}
.workspace-approval > header div { display: grid; gap: 4px; min-width: 0; }
.workspace-approval > header span { color: var(--aw-vermilion); font: 750 9px/1.2 var(--aw-font-proof); letter-spacing: 0.08em; }
.workspace-approval > header strong { overflow-wrap: anywhere; font-size: 13px; }
.workspace-approval > header b { display: grid; place-items: center; min-width: 36px; height: 36px; border: 1px solid var(--aw-vermilion); color: var(--aw-vermilion); font: 800 11px/1 var(--aw-font-proof); }
.workspace-approval dl { display: grid; gap: 7px; margin: 0; }
.workspace-approval dl div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 10px; }
.workspace-approval dt { color: var(--aw-ink-muted); font-size: 10px; }
.workspace-approval dd { margin: 0; overflow-wrap: anywhere; font: 10px/1.45 var(--aw-font-proof); }
.workspace-approval dd a { color: var(--aw-forest-deep); text-underline-offset: 3px; }
.workspace-patch-diffs { display: grid; gap: 9px; max-height: 310px; overflow: auto; }
.workspace-patch-diffs > article { border: 1px solid var(--aw-line-strong); background: var(--aw-sheet); }
.workspace-patch-diffs > article > header { display: flex; justify-content: space-between; gap: 12px; padding: 9px 10px; border-bottom: 1px solid var(--aw-line); }
.workspace-patch-diffs > article > header strong { overflow-wrap: anywhere; font-size: 11px; }
.workspace-patch-diffs > article > header span { color: var(--aw-ink-muted); font: 9px/1.4 var(--aw-font-proof); white-space: nowrap; }
.workspace-diff { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); border: 1px solid var(--aw-line-strong); background: var(--aw-sheet); }
.workspace-patch-diffs .workspace-diff { border: 0; }
.workspace-diff div { min-width: 0; padding: 10px; }
.workspace-diff div + div { border-left: 1px solid var(--aw-line-strong); }
.workspace-diff span { display: block; margin-bottom: 7px; color: var(--aw-ink-muted); font-size: 9px; }
.workspace-diff code { display: block; max-height: 96px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--aw-ink); font: 10px/1.5 var(--aw-font-proof); }
.workspace-approval > footer { align-items: center; }
.workspace-approval > footer small { max-width: 360px; color: var(--aw-ink-muted); font-size: 9px; line-height: 1.45; }
.workspace-approval button {
  min-height: 44px; flex: none; white-space: nowrap; border: 1px solid var(--aw-vermilion);
  border-radius: 2px; outline: 2px solid transparent; background: var(--aw-vermilion);
  color: var(--aw-sheet); padding: 0 14px; font-weight: 750; cursor: pointer;
}
.approval-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.workspace-approval button.is-secondary { background: transparent; color: var(--aw-vermilion); }
@media (hover: hover) {
  .workspace-approval button:hover, .workspace-approval button.is-hover { background: var(--aw-forest); border-color: var(--aw-forest); }
  .workspace-approval button.is-secondary:hover { background: var(--aw-sheet); border-color: var(--aw-vermilion); color: var(--aw-vermilion); }
}
.workspace-approval button:active, .workspace-approval button.is-active { transform: translateY(1px); }
.workspace-approval button:disabled, .workspace-approval button.is-disabled { opacity: 0.5; cursor: not-allowed; }
.workspace-approval[data-state='loading'] { opacity: 0.76; }
.workspace-approval[data-state='error'] { border-color: var(--aw-vermilion); box-shadow: inset 3px 0 0 var(--aw-vermilion); }
.workspace-approval[data-state='success'] { border-color: var(--aw-forest); background: var(--aw-forest-pale); }
.workspace-approval[data-state='success'] > header span,
.workspace-approval[data-state='success'] > header b { color: var(--aw-forest); border-color: var(--aw-forest); }
.route-notice { display: grid; gap: 6px; padding: 16px 17px; }
.route-notice strong { font-size: 12px; }
.route-notice small { color: var(--aw-ink-muted); font: 9px/1.4 var(--aw-font-proof); }
.run-card ol { list-style: none; margin: 0; padding: 0 17px; }
.run-card li { display: grid; grid-template-columns: 28px minmax(0, 1fr) auto; align-items: center; gap: 9px; min-height: 58px; border-bottom: 1px solid var(--aw-line); }
.run-card li:last-child { border-bottom: 0; }
.run-card li > span { color: var(--aw-ink-muted); font: 10px/1 var(--aw-font-proof); }
.run-card li div { display: grid; gap: 3px; }
.run-card li strong { font-size: 12px; }
.run-card li small { color: var(--aw-ink-muted); font-size: 10px; }
.run-card li b { color: var(--aw-ink-muted); font-size: 10px; font-weight: 750; }
.run-card li[data-state='done'] b { color: var(--aw-forest); }
.run-card li[data-state='active'] { background: var(--aw-forest-pale); }
.run-card li[data-state='active'] b { color: var(--aw-vermilion); }

.agent-composer { padding: 16px 28px 22px; border-top: 1px solid var(--aw-line); background: var(--aw-overlay); }
.agent-composer > label { display: block; margin-bottom: 7px; font-size: 11px; font-weight: 750; }
.agent-composer textarea {
  width: 100%; min-height: 82px; resize: vertical; border: 1px solid var(--aw-line-strong); border-radius: 2px;
  background: var(--aw-sheet); color: var(--aw-ink); padding: 11px 12px; font: 14px/1.5 var(--aw-font-ui);
}
.agent-composer footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; }
.agent-composer footer span { color: var(--aw-ink-muted); font-size: 10px; }
.agent-composer button, .export-form button, .confirm-write {
  min-height: 44px; border: 1px solid var(--aw-forest); border-radius: 2px; background: var(--aw-forest);
  color: var(--aw-sheet); padding: 0 16px; font-weight: 750; cursor: pointer;
}
.agent-composer button:disabled, .export-form button:disabled, .confirm-write:disabled { opacity: 0.38; cursor: not-allowed; }

.evidence-panel { min-width: 0; overflow-y: auto; padding: 24px 20px 28px; border-left: 1px solid var(--aw-line-strong); background: var(--aw-paper-deep); }
.evidence-panel > header { display: flex; justify-content: space-between; gap: 16px; }
.evidence-panel h2 { margin-top: 6px; font-size: 22px; letter-spacing: -0.035em; }
.evidence-panel > header > strong { display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid var(--aw-line-strong); font: 750 11px/1 var(--aw-font-proof); }
.panel-intro { margin: 9px 0 20px; color: var(--aw-ink-muted); font-size: 11px; line-height: 1.55; }
.evidence-panel details { border-top: 1px solid var(--aw-line-strong); }
.evidence-panel details:last-of-type { border-bottom: 1px solid var(--aw-line-strong); }
.evidence-panel summary { display: flex; justify-content: space-between; gap: 10px; padding: 15px 0; cursor: pointer; font-size: 11px; font-weight: 800; }
.evidence-panel summary span { color: var(--aw-ink-muted); font: 10px/1.3 var(--aw-font-proof); }
.route-proof, .mcp-proof { display: grid; gap: 0; margin: 0 0 15px; }
.route-proof div, .mcp-proof div { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; padding: 8px 0; border-top: 1px solid var(--aw-line); }
.route-proof dt, .mcp-proof dt { color: var(--aw-ink-muted); font-size: 9px; overflow-wrap: anywhere; }
.route-proof dd, .mcp-proof dd { margin: 0; max-width: 170px; text-align: right; overflow-wrap: anywhere; font: 9px/1.4 var(--aw-font-proof); }
.workspace-file-proof { display: grid; gap: 7px; padding-bottom: 15px; }
.workspace-file-proof strong { overflow-wrap: anywhere; font-size: 11px; }
.workspace-file-proof small { color: var(--aw-ink-muted); font: 9px/1.45 var(--aw-font-proof); }
.workspace-file-proof pre { max-height: 260px; margin: 0; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; border: 1px solid var(--aw-line); background: var(--aw-sheet); padding: 10px; font: 10px/1.55 var(--aw-font-proof); }
.workspace-entry-list, .workspace-issue-list { max-height: 260px; margin: 0 0 15px; padding: 0; overflow: auto; list-style: none; border: 1px solid var(--aw-line); background: var(--aw-sheet); }
.workspace-entry-list li, .workspace-issue-list li { min-height: 44px; display: grid; gap: 4px 8px; padding: 9px 10px; border-bottom: 1px solid var(--aw-line); }
.workspace-entry-list li:last-child, .workspace-issue-list li:last-child { border-bottom: 0; }
.workspace-entry-list li { grid-template-columns: 58px minmax(0, 1fr) auto; align-items: center; }
.workspace-entry-list span, .workspace-entry-list small, .workspace-issue-list small { color: var(--aw-ink-muted); font: 9px/1.35 var(--aw-font-proof); }
.workspace-entry-list strong, .workspace-issue-list strong { min-width: 0; overflow-wrap: anywhere; font-size: 10px; }
.workspace-issue-list li { grid-template-columns: minmax(0, 1fr) auto; }
.workspace-issue-list span { grid-column: 1 / -1; overflow-wrap: anywhere; color: var(--aw-ink-muted); font-size: 10px; line-height: 1.45; }
.workspace-check-pass { margin: 0 0 15px; padding: 10px; border: 1px solid var(--aw-forest); background: var(--aw-forest-pale); color: var(--aw-forest-deep); font-size: 10px; line-height: 1.5; }
.python-test-output { max-height: 280px; margin: 0 0 15px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; border: 1px solid var(--aw-line); background: var(--aw-sheet); padding: 10px; font: 9px/1.55 var(--aw-font-proof); }
.knowledge-card { padding: 0 0 14px; }
.knowledge-card + .knowledge-card { padding-top: 14px; border-top: 1px solid var(--aw-line); }
.knowledge-card strong { font-size: 11px; overflow-wrap: anywhere; }
.knowledge-card p { margin: 7px 0; font-size: 11px; line-height: 1.55; }
.knowledge-card small { color: var(--aw-ink-muted); font: 9px/1.45 var(--aw-font-proof); overflow-wrap: anywhere; }
.claim-card { padding: 12px 0; border-top: 1px solid var(--aw-line); }
.claim-card > div { display: flex; align-items: center; gap: 7px; }
.claim-card i { width: 8px; height: 8px; border: 1px solid var(--aw-line-strong); }
.claim-card i[data-verdict='verified'] { border-color: var(--aw-forest); background: var(--aw-forest); }
.claim-card i[data-verdict='unsupported'], .claim-card i[data-verdict='contradicted'] { border-color: var(--aw-vermilion); background: var(--aw-vermilion); }
.claim-card strong { font: 750 9px/1 var(--aw-font-proof); text-transform: uppercase; }
.claim-card p { margin: 8px 0; font-size: 11px; line-height: 1.55; }
.claim-card small, .artifact-card small, .empty-proof { color: var(--aw-ink-muted); font: 9px/1.5 var(--aw-font-proof); }
.empty-proof { margin: 0 0 15px; }
.artifact-card { display: grid; grid-template-columns: 1fr auto; gap: 5px 10px; padding: 0 0 15px; }
.artifact-card strong { overflow-wrap: anywhere; font-size: 11px; }
.artifact-card span { font-size: 10px; }
.artifact-card small { grid-column: 1 / -1; overflow-wrap: anywhere; }
.browser-proof { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 0 0 15px; }
.browser-proof div { padding-top: 7px; border-top: 2px solid var(--aw-forest); }
.browser-proof dt { color: var(--aw-ink-muted); font-size: 9px; }
.browser-proof dd { margin: 3px 0 0; font: 750 10px/1 var(--aw-font-proof); }

.export-section { margin-top: 24px; padding-top: 18px; border-top: 2px solid var(--aw-ink); }
.export-section h3 { margin-top: 5px; font-size: 18px; }
.export-section > p { margin: 7px 0 14px; color: var(--aw-ink-muted); font-size: 10px; line-height: 1.5; }
.export-form { display: grid; gap: 7px; }
.export-form label { font-size: 10px; font-weight: 750; }
.export-form input, .export-form select { min-width: 0; min-height: 44px; border: 1px solid var(--aw-line-strong); border-radius: 2px; background: var(--aw-sheet); color: var(--aw-ink); padding: 0 9px; font: 10px/1 var(--aw-font-proof); }
.export-form button { min-height: 44px; }
.export-receipt { display: grid; gap: 7px; margin-top: 12px; padding: 11px; border: 1px solid var(--aw-vermilion); background: var(--aw-vermilion-pale); }
.export-receipt[data-status='committed'] { border-color: var(--aw-forest); background: var(--aw-forest-pale); }
.export-receipt strong { overflow-wrap: anywhere; font: 10px/1.45 var(--aw-font-proof); }
.export-receipt small, .export-receipt > span { color: var(--aw-ink-muted); font: 9px/1.4 var(--aw-font-proof); }
.confirm-write { margin-top: 4px; background: var(--aw-vermilion); border-color: var(--aw-vermilion); }

@keyframes live-signal { 50% { opacity: 0.28; transform: scale(0.78); } }
@media (prefers-reduced-motion: reduce) { .run-state[data-live='true'] i { animation: none; } }
@media (max-width: 1500px) {
  .agent-workbench { grid-template-columns: 220px minmax(0, 1fr); }
  .evidence-panel { grid-column: 1 / -1; border-top: 1px solid var(--aw-line-strong); border-left: 0; }
}
@media (max-width: 860px) {
  .agent-workbench { display: block; }
  .session-rail { min-height: auto; }
  .session-fingerprint { margin-top: 0; }
  .conversation-column { min-height: 720px; }
  .conversation-header { display: grid; padding: 20px 16px; }
  .run-controls { justify-content: space-between; }
  .transcript { padding: 22px 16px; }
  .run-card { margin-left: 0; }
  .route-action { align-items: stretch; flex-direction: column; }
  .route-action button { width: 100%; }
  .workspace-approval > header, .workspace-approval > footer { align-items: stretch; flex-direction: column; }
  .workspace-approval button { width: 100%; white-space: nowrap; }
  .approval-actions { display: grid; grid-template-columns: 1fr 1fr; }
  .approval-actions button:last-child { grid-column: 1 / -1; }
  .workspace-patch-diffs > article > header { align-items: flex-start; flex-direction: column; }
  .workspace-patch-diffs > article > header span { white-space: normal; }
  .workspace-diff { grid-template-columns: 1fr; }
  .workspace-diff div + div { border-top: 1px solid var(--aw-line-strong); border-left: 0; }
  .workspace-entry-list li { grid-template-columns: 52px minmax(0, 1fr); }
  .workspace-entry-list small { grid-column: 2; }
  .agent-composer { padding: 14px 16px 18px; }
  .agent-composer footer { align-items: stretch; flex-direction: column; }
  .agent-composer button { width: 100%; }
}
</style>
