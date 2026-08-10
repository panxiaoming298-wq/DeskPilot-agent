import type {
  Approval,
  ApprovalAction,
  ApprovalResolutionCommand,
  ApprovalResolutionResponse,
  ApiProblem,
  LocalSession,
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
  ReconciliationOutcome,
  ReconciliationResolutionResponse,
  ReconciliationStatus,
  Task,
  TaskControlAction,
  TaskControlCommand,
  TaskCreate,
  TaskHistoryPage,
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
  command?: TaskControlCommand,
): Promise<Task> {
  const normalizedReason = command?.reason?.trim()
  return request<Task>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}:${action}`,
    {
      method: 'POST',
      body: normalizedReason ? JSON.stringify({ reason: normalizedReason }) : undefined,
    },
  )
}

export function pauseTask(taskId: string, command?: TaskControlCommand): Promise<Task> {
  return controlTask(taskId, 'pause', command)
}

export function resumeTask(taskId: string, command?: TaskControlCommand): Promise<Task> {
  return controlTask(taskId, 'resume', command)
}

export function cancelTask(taskId: string, command?: TaskControlCommand): Promise<Task> {
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
