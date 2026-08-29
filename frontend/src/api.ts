import type {
  Approval,
  ApprovalAction,
  ApprovalResolutionCommand,
  ApprovalResolutionResponse,
  ApiProblem,
  LocalSession,
  ManagedCredentialStatus,
  ProviderCatalogSnapshot,
  ProviderConfig,
  ProviderConfigAuditPage,
  ProviderHealthSnapshot,
  ModelGatewayRoutingSnapshot,
  ProviderMutationResult,
  Reconciliation,
  ReconciliationAttemptResponse,
  ReconciliationCompensationResponse,
  ReconciliationEvidenceRefreshResponse,
  GraphRecoveryAction,
  ReconciliationGraphRecoveryResponse,
  ReconciliationOutcome,
  ReconciliationResolutionResponse,
  ReconciliationStatus,
  Task,
  TaskControlAction,
  TaskControlCommand,
  TaskCreate,
  TaskHistoryPage,
  EffectRuntimeAuditPage,
  EffectRuntimeAuditExportPage,
  EffectRuntimeOperationsSnapshot,
  MetricsAuditResult,
  OutboxRequeueResult,
  OperationsAlertNotificationPage,
  RetentionRunResult,
  KnowledgeSearchResult,
  KnowledgeSource,
  McpAuditPage,
  McpServer,
  McpServerMutation,
  McpToolCallResult,
  EvaluationRun,
  EvaluationRunPage,
  EvaluationReport,
  CreateLongTermMemory,
  LongTermMemoryExport,
  LongTermMemoryPage,
  ArtifactExport,
  ContinueConversationTurn,
  CreateConversationTurn,
  CreateResearchWorkbenchTask,
  TaskWorkbench,
  WorkbenchRun,
  WorkbenchStepCommand,
} from './types'

const configuredBase = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '')
const API_BASE = configuredBase ?? ''
const CLIENT_HEADER = 'deskpilot-web-v1'

let sessionPromise: Promise<LocalSession> | null = null

export class ApiProblemError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly problem: ApiProblem | null,
  ) {
    super(message)
    this.name = 'ApiProblemError'
  }
}

async function toApiError(response: Response): Promise<ApiProblemError> {
  const body = (await response.json().catch(() => null)) as ApiProblem | null
  return new ApiProblemError(
    body?.detail ?? body?.title ?? `HTTP ${response.status}`,
    response.status,
    body?.code ?? 'HTTP_ERROR',
    body,
  )
}

export function invalidateLocalSession(): void {
  sessionPromise = null
}

export function getLocalSession(): Promise<LocalSession> {
  if (!sessionPromise) {
    sessionPromise = fetch(`${API_BASE}/api/v1/session`, {
      cache: 'no-store',
      credentials: 'omit',
      headers: { 'X-DeskPilot-Client': CLIENT_HEADER },
    }).then(async (response) => {
      if (!response.ok) throw await toApiError(response)
      return (await response.json()) as LocalSession
    })
    sessionPromise.catch(() => {
      sessionPromise = null
    })
  }
  return sessionPromise
}

async function authenticatedResponse(
  path: string,
  init?: RequestInit,
  canRetry = true,
): Promise<Response> {
  const session = await getLocalSession()
  const headers = new Headers(init?.headers)
  headers.set('Authorization', `Bearer ${session.access_token}`)
  if (init?.body !== undefined) headers.set('Content-Type', 'application/json')
  headers.set('X-DeskPilot-Client', CLIENT_HEADER)
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'omit',
    headers,
  })

  if (!response.ok) {
    if (response.status === 401 && canRetry) {
      invalidateLocalSession()
      return authenticatedResponse(path, init, false)
    }
    throw await toApiError(response)
  }

  return response
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedResponse(path, init)
  return (await response.json()) as T
}

export function createTask(command: TaskCreate): Promise<Task> {
  return request<Task>('/api/v1/tasks', {
    method: 'POST',
    body: JSON.stringify(command),
  })
}

export function listKnowledgeSources(): Promise<KnowledgeSource[]> {
  return request<KnowledgeSource[]>('/api/v1/knowledge/sources', { cache: 'no-store' })
}

export function importKnowledgeSource(path: string): Promise<KnowledgeSource> {
  return request<KnowledgeSource>('/api/v1/knowledge/sources:import', {
    method: 'POST',
    body: JSON.stringify({ path }),
  })
}

export function searchKnowledge(query: string, limit = 10): Promise<KnowledgeSearchResult> {
  return request<KnowledgeSearchResult>('/api/v1/knowledge/search', {
    method: 'POST',
    body: JSON.stringify({ query, limit }),
  })
}

export function getLongTermMemory(): Promise<LongTermMemoryPage> {
  return request<LongTermMemoryPage>('/api/v1/memory', { cache: 'no-store' })
}

export function createLongTermMemory(
  command: CreateLongTermMemory,
): Promise<LongTermMemoryPage> {
  return request<LongTermMemoryPage>('/api/v1/memory', {
    method: 'POST',
    body: JSON.stringify(command),
  })
}

export function confirmMemoryProposal(proposalId: string): Promise<LongTermMemoryPage> {
  return request<LongTermMemoryPage>(
    `/api/v1/memory/proposals/${encodeURIComponent(proposalId)}:confirm`,
    { method: 'POST' },
  )
}

export function rejectMemoryProposal(proposalId: string): Promise<LongTermMemoryPage> {
  return request<LongTermMemoryPage>(
    `/api/v1/memory/proposals/${encodeURIComponent(proposalId)}:reject`,
    { method: 'POST' },
  )
}

export function editLongTermMemory(
  memoryId: string,
  value: string,
  classification: 'public' | 'internal' | 'sensitive',
): Promise<LongTermMemoryPage> {
  return request<LongTermMemoryPage>(`/api/v1/memory/${encodeURIComponent(memoryId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ value, classification }),
  })
}

export function deleteLongTermMemory(memoryId: string): Promise<LongTermMemoryPage> {
  return request<LongTermMemoryPage>(`/api/v1/memory/${encodeURIComponent(memoryId)}`, {
    method: 'DELETE',
  })
}

export function resolveMemoryConflict(
  conflictId: string,
  selectedMemoryId: string,
): Promise<LongTermMemoryPage> {
  return request<LongTermMemoryPage>(
    `/api/v1/memory-conflicts/${encodeURIComponent(conflictId)}:resolve`,
    {
      method: 'POST',
      body: JSON.stringify({ selected_memory_id: selectedMemoryId }),
    },
  )
}

export function exportLongTermMemory(): Promise<LongTermMemoryExport> {
  return request<LongTermMemoryExport>('/api/v1/memory/export', { cache: 'no-store' })
}

export function listMcpServers(): Promise<McpServer[]> {
  return request<McpServer[]>('/api/v1/mcp/servers', { cache: 'no-store' })
}

export function setMcpServerEnabled(serverId: string, enabled: boolean): Promise<McpServerMutation> {
  const action = enabled ? 'enable' : 'disable'
  return request<McpServerMutation>(
    `/api/v1/mcp/servers/${encodeURIComponent(serverId)}:${action}`,
    { method: 'POST' },
  )
}

export function callMcpTool(
  serverId: string,
  toolName: string,
  argumentsValue: Record<string, unknown>,
): Promise<McpToolCallResult> {
  return request<McpToolCallResult>(
    `/api/v1/mcp/servers/${encodeURIComponent(serverId)}/tools:call`,
    {
      method: 'POST',
      body: JSON.stringify({ tool_name: toolName, arguments: argumentsValue }),
    },
  )
}

export function getMcpAudit(): Promise<McpAuditPage> {
  return request<McpAuditPage>('/api/v1/mcp/audit', { cache: 'no-store' })
}

export function listEvaluationRuns(): Promise<EvaluationRunPage> {
  return request<EvaluationRunPage>('/api/v1/evaluations/runs', { cache: 'no-store' })
}

export function runGoldenEvaluation(): Promise<EvaluationRun> {
  return request<EvaluationRun>('/api/v1/evaluations/golden:run', { method: 'POST' })
}

export function replayEvaluation(runId: string): Promise<EvaluationRun> {
  return request<EvaluationRun>(
    `/api/v1/evaluations/runs/${encodeURIComponent(runId)}:replay`,
    { method: 'POST' },
  )
}

export function getEvaluationReport(limit = 50): Promise<EvaluationReport> {
  return request<EvaluationReport>(
    `/api/v1/evaluations/reports/latest?limit=${encodeURIComponent(limit)}`,
    { cache: 'no-store' },
  )
}

export async function downloadEvaluationReport(limit = 50): Promise<void> {
  const response = await authenticatedResponse(
    `/api/v1/evaluations/reports/latest:export?limit=${encodeURIComponent(limit)}`,
    { cache: 'no-store' },
  )
  const report = (await response.json()) as EvaluationReport
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const filename = disposition.match(/filename="([^"]+)"/)?.[1]
    ?? 'deskpilot-evaluation-report-v1.json'
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {
    type: 'application/json',
  }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

export function listTasks(
  status?: Task['status'],
  limit = 50,
  offset = 0,
): Promise<TaskHistoryPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (status) query.set('status', status)
  return request<TaskHistoryPage>(`/api/v1/tasks?${query}`, {
    cache: 'no-store',
  })
}

export function getTask(taskId: string): Promise<Task> {
  return request<Task>(`/api/v1/tasks/${encodeURIComponent(taskId)}`, {
    cache: 'no-store',
  })
}

export function controlTask(
  taskId: string,
  action: TaskControlAction,
  command: TaskControlCommand,
): Promise<Task> {
  if (!command || !Number.isInteger(command.expected_last_event_seq) || command.expected_last_event_seq < 1) {
    throw new Error('任务控制必须绑定有效的 last_event_seq。')
  }
  const normalizedReason = command.reason?.trim()
  return request<Task>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}:${action}`,
    {
      method: 'POST',
      body: JSON.stringify({
        expected_last_event_seq: command.expected_last_event_seq,
        ...(normalizedReason ? { reason: normalizedReason } : {}),
      }),
    },
  )
}

export function pauseTask(taskId: string, command: TaskControlCommand): Promise<Task> {
  return controlTask(taskId, 'pause', command)
}

export function resumeTask(taskId: string, command: TaskControlCommand): Promise<Task> {
  return controlTask(taskId, 'resume', command)
}

export function cancelTask(taskId: string, command: TaskControlCommand): Promise<Task> {
  return controlTask(taskId, 'cancel', command)
}

export function listPendingApprovals(taskId?: string): Promise<Approval[]> {
  const query = new URLSearchParams({ status: 'pending' })
  if (taskId) query.set('task_id', taskId)
  return request<Approval[]>(`/api/v1/approvals?${query}`, {
    cache: 'no-store',
  })
}

export function getApproval(approvalId: string): Promise<Approval> {
  return request<Approval>(
    `/api/v1/approvals/${encodeURIComponent(approvalId)}`,
    { cache: 'no-store' },
  )
}

export function resolveApproval(
  approvalId: string,
  action: ApprovalAction,
  command: ApprovalResolutionCommand,
): Promise<ApprovalResolutionResponse> {
  const normalizedReason = command.reason?.trim()
  const body: ApprovalResolutionCommand = {
    preview_hash: command.preview_hash,
    scope: 'once',
  }
  if (normalizedReason) body.reason = normalizedReason

  return request<ApprovalResolutionResponse>(
    `/api/v1/approvals/${encodeURIComponent(approvalId)}:${action}`,
    {
      method: 'POST',
      body: JSON.stringify(body),
    },
  )
}

export function approveApproval(
  approvalId: string,
  previewHash: string,
): Promise<ApprovalResolutionResponse> {
  return resolveApproval(approvalId, 'approve', {
    preview_hash: previewHash,
    scope: 'once',
  })
}

export function rejectApproval(
  approvalId: string,
  previewHash: string,
  reason?: string,
): Promise<ApprovalResolutionResponse> {
  return resolveApproval(approvalId, 'reject', {
    preview_hash: previewHash,
    scope: 'once',
    reason,
  })
}

export function listReconciliations(
  taskId?: string,
  status?: ReconciliationStatus,
): Promise<Reconciliation[]> {
  const query = new URLSearchParams()
  if (status) query.set('status', status)
  if (taskId) query.set('task_id', taskId)
  const suffix = query.size ? `?${query}` : ''
  return request<Reconciliation[]>(`/api/v1/reconciliations${suffix}`, {
    cache: 'no-store',
  })
}

export function getReconciliation(reconciliationId: string): Promise<Reconciliation> {
  return request<Reconciliation>(
    `/api/v1/reconciliations/${encodeURIComponent(reconciliationId)}`,
    { cache: 'no-store' },
  )
}

export function refreshReconciliationEvidence(
  reconciliationId: string,
): Promise<ReconciliationEvidenceRefreshResponse> {
  return request<ReconciliationEvidenceRefreshResponse>(
    `/api/v1/reconciliations/${encodeURIComponent(reconciliationId)}:refresh-evidence`,
    { method: 'POST' },
  )
}

export function resolveReconciliation(
  reconciliationId: string,
  outcome: ReconciliationOutcome,
  evidenceSummary: string,
  idempotencyKey: string,
): Promise<ReconciliationResolutionResponse> {
  return request<ReconciliationResolutionResponse>(
    `/api/v1/reconciliations/${encodeURIComponent(reconciliationId)}:resolve`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({
        outcome,
        evidence_summary: evidenceSummary.trim(),
      }),
    },
  )
}

export function recoverReconciliationGraph(
  reconciliationId: string,
  action: GraphRecoveryAction,
  idempotencyKey: string,
): Promise<ReconciliationGraphRecoveryResponse> {
  return request<ReconciliationGraphRecoveryResponse>(
    `/api/v1/reconciliations/${encodeURIComponent(reconciliationId)}:recover-graph`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ action }),
    },
  )
}

export function createReconciliationAttempt(
  reconciliationId: string,
  idempotencyKey: string,
): Promise<ReconciliationAttemptResponse> {
  return request<ReconciliationAttemptResponse>(
    `/api/v1/reconciliations/${encodeURIComponent(reconciliationId)}:create-attempt`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    },
  )
}

export function createReconciliationCompensation(
  reconciliationId: string,
  idempotencyKey: string,
): Promise<ReconciliationCompensationResponse> {
  return request<ReconciliationCompensationResponse>(
    `/api/v1/reconciliations/${encodeURIComponent(reconciliationId)}:create-compensation`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    },
  )
}

export function buildTaskSocketUrl(taskId: string, afterSeq: number): string {
  const encodedTaskId = encodeURIComponent(taskId)
  if (configuredBase) {
    const url = new URL(configuredBase)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    url.pathname = `/api/v1/ws/tasks/${encodedTaskId}`
    url.search = new URLSearchParams({ after_seq: String(afterSeq) }).toString()
    return url.toString()
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/v1/ws/tasks/${encodedTaskId}?after_seq=${afterSeq}`
}

export interface ProviderCatalogResponse {
  snapshot: ProviderCatalogSnapshot
  etag: string
}

export interface ProviderMutationResponse {
  result: ProviderMutationResult
  etag: string
}

function providerWriteHeaders(etag: string, idempotencyKey: string): HeadersInit {
  return {
    'If-Match': etag,
    'Idempotency-Key': idempotencyKey,
  }
}

export function createIdempotencyKey(): string {
  return `deskpilot-ui-${crypto.randomUUID()}`
}

export async function getProviderCatalog(): Promise<ProviderCatalogResponse> {
  const response = await authenticatedResponse('/api/v1/model-providers', {
    cache: 'no-store',
  })
  const etag = response.headers.get('ETag')
  if (!etag) {
    throw new ApiProblemError('Provider Catalog 响应缺少 ETag。', 502, 'PROVIDER_ETAG_MISSING', null)
  }
  return {
    snapshot: (await response.json()) as ProviderCatalogSnapshot,
    etag,
  }
}

export function getProviderAudit(providerId?: string): Promise<ProviderConfigAuditPage> {
  const query = new URLSearchParams({ after_sequence: '0', limit: '100' })
  if (providerId) query.set('provider_id', providerId)
  return request<ProviderConfigAuditPage>(`/api/v1/model-providers/audit?${query}`)
}

export function getProviderRouting(): Promise<ModelGatewayRoutingSnapshot> {
  return request<ModelGatewayRoutingSnapshot>('/api/v1/model-providers/routing')
}

async function mutateProvider(
  path: string,
  method: 'POST' | 'PUT' | 'DELETE',
  etag: string,
  idempotencyKey: string,
  body?: ProviderConfig,
): Promise<ProviderMutationResponse> {
  const response = await authenticatedResponse(path, {
    method,
    headers: providerWriteHeaders(etag, idempotencyKey),
    body: body ? JSON.stringify(body) : undefined,
  })
  return {
    result: (await response.json()) as ProviderMutationResult,
    etag: response.headers.get('ETag') ?? etag,
  }
}

export function createProvider(
  config: ProviderConfig,
  etag: string,
  idempotencyKey: string,
): Promise<ProviderMutationResponse> {
  return mutateProvider('/api/v1/model-providers', 'POST', etag, idempotencyKey, config)
}

export function updateProvider(
  providerId: string,
  config: ProviderConfig,
  etag: string,
  idempotencyKey: string,
): Promise<ProviderMutationResponse> {
  return mutateProvider(
    `/api/v1/model-providers/${encodeURIComponent(providerId)}`,
    'PUT',
    etag,
    idempotencyKey,
    config,
  )
}

export function setProviderEnabled(
  providerId: string,
  enabled: boolean,
  etag: string,
  idempotencyKey: string,
): Promise<ProviderMutationResponse> {
  const action = enabled ? 'enable' : 'disable'
  return mutateProvider(
    `/api/v1/model-providers/${encodeURIComponent(providerId)}:${action}`,
    'POST',
    etag,
    idempotencyKey,
  )
}

export function makeProviderDefault(
  providerId: string,
  etag: string,
  idempotencyKey: string,
): Promise<ProviderMutationResponse> {
  return mutateProvider(
    `/api/v1/model-providers/${encodeURIComponent(providerId)}:make-default`,
    'POST',
    etag,
    idempotencyKey,
  )
}

export function deleteProvider(
  providerId: string,
  etag: string,
  idempotencyKey: string,
): Promise<ProviderMutationResponse> {
  return mutateProvider(
    `/api/v1/model-providers/${encodeURIComponent(providerId)}`,
    'DELETE',
    etag,
    idempotencyKey,
  )
}

export function checkProviderHealth(providerId: string): Promise<ProviderHealthSnapshot> {
  return request<ProviderHealthSnapshot>(
    `/api/v1/model-providers/${encodeURIComponent(providerId)}/health`,
  )
}

export function getManagedCredentialStatus(
  identifier: string,
): Promise<ManagedCredentialStatus> {
  return request<ManagedCredentialStatus>(
    `/api/v1/model-providers/credentials/${encodeURIComponent(identifier)}`,
    { cache: 'no-store' },
  )
}

export function storeManagedCredential(
  identifier: string,
  secret: string,
): Promise<ManagedCredentialStatus> {
  return request<ManagedCredentialStatus>(
    `/api/v1/model-providers/credentials/${encodeURIComponent(identifier)}`,
    {
      method: 'PUT',
      cache: 'no-store',
      body: JSON.stringify({ secret }),
    },
  )
}

export function deleteManagedCredential(
  identifier: string,
): Promise<ManagedCredentialStatus> {
  return request<ManagedCredentialStatus>(
    `/api/v1/model-providers/credentials/${encodeURIComponent(identifier)}`,
    {
      method: 'DELETE',
      cache: 'no-store',
      headers: { 'X-DeskPilot-Credential-Confirmation': identifier },
    },
  )
}

export function getEffectRuntimeOperations(
  sampleLimit = 50,
): Promise<EffectRuntimeOperationsSnapshot> {
  const query = new URLSearchParams({ sample_limit: String(sampleLimit) })
  return request<EffectRuntimeOperationsSnapshot>(
    `/api/v1/operations/effect-runtime?${query}`,
    { cache: 'no-store' },
  )
}

export function getEffectRuntimeAudit(
  afterSequence = 0,
  limit = 100,
): Promise<EffectRuntimeAuditPage> {
  const query = new URLSearchParams({
    after_sequence: String(afterSequence),
    limit: String(limit),
  })
  return request<EffectRuntimeAuditPage>(
    `/api/v1/operations/effect-runtime/audit?${query}`,
    { cache: 'no-store' },
  )
}

export function getEffectRuntimeAlertNotifications(
  afterSequence = 0,
  limit = 100,
): Promise<OperationsAlertNotificationPage> {
  const query = new URLSearchParams({
    after_sequence: String(afterSequence),
    limit: String(limit),
  })
  return request<OperationsAlertNotificationPage>(
    `/api/v1/operations/effect-runtime/alerts?${query}`,
    { cache: 'no-store' },
  )
}

export function getEffectRuntimeAuditExport(
  cursor: string | null = null,
  limit = 500,
): Promise<EffectRuntimeAuditExportPage> {
  const query = new URLSearchParams({ limit: String(limit) })
  if (cursor) query.set('cursor', cursor)
  return request<EffectRuntimeAuditExportPage>(
    `/api/v1/operations/effect-runtime/audit/export?${query}`,
    { cache: 'no-store' },
  )
}

export function sampleEffectRuntimeMetrics(sampleLimit = 50): Promise<MetricsAuditResult> {
  const query = new URLSearchParams({ sample_limit: String(sampleLimit) })
  return request<MetricsAuditResult>(
    `/api/v1/operations/effect-runtime:sample?${query}`,
    { method: 'POST' },
  )
}

export function runEffectRuntimeRetention(
  retentionDays: number | null,
  idempotencyKey: string,
): Promise<RetentionRunResult> {
  return request<RetentionRunResult>(
    '/api/v1/operations/effect-runtime:run-retention',
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ retention_days: retentionDays }),
    },
  )
}

export function requeueOutboxDeadLetter(
  messageId: string,
  idempotencyKey: string,
): Promise<OutboxRequeueResult> {
  return request<OutboxRequeueResult>(
    `/api/v1/operations/outbox/${encodeURIComponent(messageId)}:requeue`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    },
  )
}

export function createResearchWorkbenchTask(
  command: CreateResearchWorkbenchTask,
): Promise<TaskWorkbench> {
  return request<TaskWorkbench>('/api/v1/research-workbench/tasks', {
    method: 'POST',
    body: JSON.stringify(command),
  })
}

export function createConversationTurn(
  command: CreateConversationTurn,
): Promise<TaskWorkbench> {
  return request<TaskWorkbench>('/api/v1/conversation-turns', {
    method: 'POST',
    body: JSON.stringify(command),
  })
}

export function continueConversationTurn(
  taskId: string,
  command: ContinueConversationTurn,
): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/conversation-turns`,
    { method: 'POST', body: JSON.stringify(command) },
  )
}

export function advanceTaskWorkbench(taskId: string): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/workbench:advance`,
    { method: 'POST' },
  )
}

export function interpretTaskWorkbench(taskId: string): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/workbench:interpret-turn`,
    { method: 'POST' },
  )
}

export function stopTaskWorkbench(taskId: string): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/workbench:stop`,
    { method: 'POST' },
  )
}

export function replanTaskWorkbench(taskId: string): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/workbench:replan`,
    { method: 'POST' },
  )
}

export function commitWorkspaceEdit(
  taskId: string,
  confirmationDigest: string,
): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/workspace-edit:commit`,
    {
      method: 'POST',
      body: JSON.stringify({ confirmation_digest: confirmationDigest }),
    },
  )
}

export function commitWorkspacePatch(
  taskId: string,
  confirmationDigest: string,
): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/workspace-patch:commit`,
    {
      method: 'POST',
      body: JSON.stringify({ confirmation_digest: confirmationDigest }),
    },
  )
}

export function commitWorkspaceGit(
  taskId: string,
  confirmationDigest: string,
): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/workspace-git:commit`,
    {
      method: 'POST',
      body: JSON.stringify({ confirmation_digest: confirmationDigest }),
    },
  )
}

export function commitWorkspacePathOperation(
  taskId: string,
  confirmationDigest: string,
): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/workspace-path-operation:commit`,
    {
      method: 'POST',
      body: JSON.stringify({ confirmation_digest: confirmationDigest }),
    },
  )
}

export function getTaskWorkbench(taskId: string): Promise<TaskWorkbench> {
  return request<TaskWorkbench>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/workbench`,
    { cache: 'no-store' },
  )
}

export function runWorkbenchStep(
  runId: string,
  command: WorkbenchStepCommand,
): Promise<unknown> {
  return request(`/api/v1/execution-runs/${encodeURIComponent(runId)}/${command}`, {
    method: 'POST',
  })
}

export function cancelWorkbenchExecution(runId: string): Promise<WorkbenchRun> {
  return request<WorkbenchRun>(
    `/api/v1/execution-runs/${encodeURIComponent(runId)}:cancel`,
    { method: 'POST' },
  )
}

export function prepareArtifactExport(
  deliveryId: string,
  targetPath: string,
  idempotencyKey: string,
  artifactId?: string,
): Promise<ArtifactExport> {
  return request<ArtifactExport>(
    `/api/v1/deliveries/${encodeURIComponent(deliveryId)}/exports:prepare`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ target_path: targetPath, artifact_id: artifactId }),
    },
  )
}

export function commitArtifactExport(
  exportId: string,
  confirmationDigest: string,
  idempotencyKey: string,
): Promise<ArtifactExport> {
  return request<ArtifactExport>(
    `/api/v1/artifact-exports/${encodeURIComponent(exportId)}:commit`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ confirmation_digest: confirmationDigest }),
    },
  )
}
