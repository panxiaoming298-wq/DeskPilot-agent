import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Approval, LocalSession, Task } from './types'

const session: LocalSession = {
  access_token: 'local-test-token',
  token_type: 'Bearer',
  websocket_protocol: 'deskpilot.local.v1',
}

const task: Task = {
  task_id: 'task/with spaces?#',
  conversation_id: null,
  goal: 'Verify task controls',
  status: 'running',
  mode: 'standard',
  privacy_mode: 'local_only',
  constraints: [],
  last_event_seq: 3,
  event_stream: '/api/v1/ws/tasks/task%2Fwith%20spaces%3F%23',
  created_at: '2026-08-09T00:00:00Z',
  updated_at: '2026-08-09T00:00:01Z',
}

const approval: Approval = {
  approval_id: 'approval/with spaces?#',
  decision_id: 'decision-1',
  task_id: task.task_id,
  call_id: 'call-1',
  status: 'pending',
  decision: null,
  preview_hash: 'preview-hash-1',
  title: '确认操作',
  purpose: '完成任务',
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
  requested_at: '2026-08-09T00:00:00Z',
  expires_at: '2026-08-09T00:05:00Z',
  resolved_at: null,
  consumed_at: null,
  resolution_reason: null,
  updated_at: '2026-08-09T00:00:00Z',
}

function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
}

function expectAuthenticatedRequest(
  fetchMock: ReturnType<typeof vi.fn>,
  callIndex: number,
  expectedPath: string,
  expectedMethod?: string,
): RequestInit {
  const [url, init] = fetchMock.mock.calls[callIndex] as [string, RequestInit]
  const headers = new Headers(init.headers)

  expect(url).toBe(expectedPath)
  expect(init.method).toBe(expectedMethod)
  expect(init.credentials).toBe('omit')
  expect(headers.get('Authorization')).toBe('Bearer local-test-token')
  expect(headers.get('X-DeskPilot-Client')).toBe('deskpilot-web-v1')

  return init
}

describe('task API', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('uses encoded task paths, correct methods, normalized reasons, and local-session auth', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(jsonResponse(task))
      .mockResolvedValueOnce(jsonResponse({ ...task, status: 'paused' }))
      .mockResolvedValueOnce(jsonResponse(task))
      .mockResolvedValueOnce(jsonResponse({ ...task, status: 'cancelled' }))
    vi.stubGlobal('fetch', fetchMock)

    const { buildTaskSocketUrl, cancelTask, getTask, pauseTask, resumeTask } = await import('./api')
    const taskId = task.task_id

    await getTask(taskId)
    await pauseTask(taskId, {
      expected_last_event_seq: task.last_event_seq,
      reason: '  operator requested  ',
    })
    await resumeTask(taskId, {
      expected_last_event_seq: task.last_event_seq,
      reason: '   ',
    })
    await cancelTask(taskId, { expected_last_event_seq: task.last_event_seq })

    expect(buildTaskSocketUrl(taskId, 7)).toContain(
      '/api/v1/ws/tasks/task%2Fwith%20spaces%3F%23?after_seq=7',
    )

    expect(fetchMock).toHaveBeenCalledTimes(5)
    expect(fetchMock.mock.calls[0]).toEqual([
      '/api/v1/session',
      {
        cache: 'no-store',
        credentials: 'omit',
        headers: { 'X-DeskPilot-Client': 'deskpilot-web-v1' },
      },
    ])

    const encodedId = 'task%2Fwith%20spaces%3F%23'
    const getInit = expectAuthenticatedRequest(
      fetchMock,
      1,
      `/api/v1/tasks/${encodedId}`,
    )
    expect(getInit.cache).toBe('no-store')
    expect(getInit.body).toBeUndefined()
    expect(new Headers(getInit.headers).has('Content-Type')).toBe(false)

    const pauseInit = expectAuthenticatedRequest(
      fetchMock,
      2,
      `/api/v1/tasks/${encodedId}:pause`,
      'POST',
    )
    expect(pauseInit.body).toBe(JSON.stringify({
      expected_last_event_seq: task.last_event_seq,
      reason: 'operator requested',
    }))
    expect(new Headers(pauseInit.headers).get('Content-Type')).toBe('application/json')

    const resumeInit = expectAuthenticatedRequest(
      fetchMock,
      3,
      `/api/v1/tasks/${encodedId}:resume`,
      'POST',
    )
    expect(resumeInit.body).toBe(JSON.stringify({
      expected_last_event_seq: task.last_event_seq,
    }))
    expect(new Headers(resumeInit.headers).get('Content-Type')).toBe('application/json')

    const cancelInit = expectAuthenticatedRequest(
      fetchMock,
      4,
      `/api/v1/tasks/${encodedId}:cancel`,
      'POST',
    )
    expect(cancelInit.body).toBe(JSON.stringify({
      expected_last_event_seq: task.last_event_seq,
    }))
    expect(new Headers(cancelInit.headers).get('Content-Type')).toBe('application/json')
  })

  it('does not replay a control request after a network TypeError', async () => {
    const networkError = new TypeError('Failed to fetch')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockRejectedValueOnce(networkError)
    vi.stubGlobal('fetch', fetchMock)

    const { pauseTask } = await import('./api')

    await expect(pauseTask('task-1', {
      expected_last_event_seq: 1,
      reason: 'pause once',
    })).rejects.toBe(networkError)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expectAuthenticatedRequest(fetchMock, 1, '/api/v1/tasks/task-1:pause', 'POST')
  })

  it('任务历史查询使用有界分页、状态筛选和 no-store', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(jsonResponse({
        items: [task],
        total: 1,
        limit: 25,
        offset: 50,
      }))
    vi.stubGlobal('fetch', fetchMock)

    const { listTasks } = await import('./api')

    await listTasks('running', 25, 50)

    const init = expectAuthenticatedRequest(
      fetchMock,
      1,
      '/api/v1/tasks?limit=25&offset=50&status=running',
    )
    expect(init.cache).toBe('no-store')
    expect(init.body).toBeUndefined()
  })

  it('审批 API 使用编码路径、no-store 预览和精确的单次 scope', async () => {
    const approved = { ...approval, status: 'approved' as const }
    const rejected = { ...approval, status: 'rejected' as const }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(jsonResponse([approval]))
      .mockResolvedValueOnce(jsonResponse(approval))
      .mockResolvedValueOnce(jsonResponse({ approval: approved, task, replayed: false }))
      .mockResolvedValueOnce(jsonResponse({ approval: rejected, task, replayed: false }))
    vi.stubGlobal('fetch', fetchMock)

    const {
      approveApproval,
      getApproval,
      listPendingApprovals,
      rejectApproval,
    } = await import('./api')

    await listPendingApprovals(task.task_id)
    await getApproval(approval.approval_id)
    await approveApproval(approval.approval_id, approval.preview_hash)
    await rejectApproval(approval.approval_id, approval.preview_hash, '  目标不正确  ')

    const listInit = expectAuthenticatedRequest(
      fetchMock,
      1,
      '/api/v1/approvals?status=pending&task_id=task%2Fwith+spaces%3F%23',
    )
    expect(listInit.cache).toBe('no-store')

    const encodedApprovalId = 'approval%2Fwith%20spaces%3F%23'
    const getInit = expectAuthenticatedRequest(
      fetchMock,
      2,
      `/api/v1/approvals/${encodedApprovalId}`,
    )
    expect(getInit.cache).toBe('no-store')

    const approveInit = expectAuthenticatedRequest(
      fetchMock,
      3,
      `/api/v1/approvals/${encodedApprovalId}:approve`,
      'POST',
    )
    expect(approveInit.body).toBe(JSON.stringify({
      preview_hash: 'preview-hash-1',
      scope: 'once',
    }))

    const rejectInit = expectAuthenticatedRequest(
      fetchMock,
      4,
      `/api/v1/approvals/${encodedApprovalId}:reject`,
      'POST',
    )
    expect(rejectInit.body).toBe(JSON.stringify({
      preview_hash: 'preview-hash-1',
      scope: 'once',
      reason: '目标不正确',
    }))
  })

  it('网络错误后不自动重放审批 POST', async () => {
    const networkError = new TypeError('Failed to fetch')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockRejectedValueOnce(networkError)
    vi.stubGlobal('fetch', fetchMock)

    const { approveApproval } = await import('./api')

    await expect(approveApproval('approval-1', 'preview-1')).rejects.toBe(networkError)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expectAuthenticatedRequest(
      fetchMock,
      1,
      '/api/v1/approvals/approval-1:approve',
      'POST',
    )
  })

  it('对账证据 API 使用编码路径、no-store 查询和无调用重放语义的刷新 POST', async () => {
    const reconciliationId = 'reconciliation/with spaces?#'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({ reconciliation_id: reconciliationId }))
      .mockResolvedValueOnce(jsonResponse({ replayed: false }))
      .mockResolvedValueOnce(jsonResponse({ replayed: false }))
      .mockResolvedValueOnce(jsonResponse({ replayed: false, resumed: true, task }))
      .mockResolvedValueOnce(jsonResponse({ replayed: false, task }))
      .mockResolvedValueOnce(jsonResponse({ replayed: false, task }))
    vi.stubGlobal('fetch', fetchMock)

    const {
      getReconciliation,
      listReconciliations,
      refreshReconciliationEvidence,
      resolveReconciliation,
      recoverReconciliationGraph,
      createReconciliationAttempt,
      createReconciliationCompensation,
    } = await import('./api')

    await listReconciliations(task.task_id, 'pending')
    await getReconciliation(reconciliationId)
    await refreshReconciliationEvidence(reconciliationId)
    await resolveReconciliation(
      reconciliationId,
      'confirmed_no_effect',
      '  已核对外部状态  ',
      'resolve-key-once',
    )
    await recoverReconciliationGraph(reconciliationId, 'continue', 'recover-key-once')
    await createReconciliationAttempt(reconciliationId, 'attempt-key-once')
    await createReconciliationCompensation(reconciliationId, 'compensation-key-once')

    const listInit = expectAuthenticatedRequest(
      fetchMock,
      1,
      '/api/v1/reconciliations?status=pending&task_id=task%2Fwith+spaces%3F%23',
    )
    expect(listInit.cache).toBe('no-store')

    const encodedId = 'reconciliation%2Fwith%20spaces%3F%23'
    const getInit = expectAuthenticatedRequest(
      fetchMock,
      2,
      `/api/v1/reconciliations/${encodedId}`,
    )
    expect(getInit.cache).toBe('no-store')

    const refreshInit = expectAuthenticatedRequest(
      fetchMock,
      3,
      `/api/v1/reconciliations/${encodedId}:refresh-evidence`,
      'POST',
    )
    expect(refreshInit.body).toBeUndefined()
    expect(new Headers(refreshInit.headers).has('Content-Type')).toBe(false)
    expect(new Headers(refreshInit.headers).has('Idempotency-Key')).toBe(false)

    const resolveInit = expectAuthenticatedRequest(
      fetchMock,
      4,
      `/api/v1/reconciliations/${encodedId}:resolve`,
      'POST',
    )
    expect(resolveInit.body).toBe(JSON.stringify({
      outcome: 'confirmed_no_effect',
      evidence_summary: '已核对外部状态',
    }))
    expect(new Headers(resolveInit.headers).get('Idempotency-Key')).toBe(
      'resolve-key-once',
    )

    const attemptInit = expectAuthenticatedRequest(
      fetchMock,
      6,
      `/api/v1/reconciliations/${encodedId}:create-attempt`,
      'POST',
    )
    expect(attemptInit.body).toBeUndefined()
    expect(new Headers(attemptInit.headers).get('Idempotency-Key')).toBe(
      'attempt-key-once',
    )
    expect(new Headers(attemptInit.headers).has('Content-Type')).toBe(false)

    const compensationInit = expectAuthenticatedRequest(
      fetchMock,
      7,
      `/api/v1/reconciliations/${encodedId}:create-compensation`,
      'POST',
    )
    expect(compensationInit.body).toBeUndefined()
    expect(new Headers(compensationInit.headers).get('Idempotency-Key')).toBe(
      'compensation-key-once',
    )
    expect(new Headers(compensationInit.headers).has('Content-Type')).toBe(false)

    const recoverInit = expectAuthenticatedRequest(
      fetchMock,
      5,
      `/api/v1/reconciliations/${encodedId}:recover-graph`,
      'POST',
    )
    expect(recoverInit.body).toBe(JSON.stringify({ action: 'continue' }))
    expect(new Headers(recoverInit.headers).get('Idempotency-Key')).toBe(
      'recover-key-once',
    )
  })
})

describe('phase 76 workbench API', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('encodes identities and binds both export steps to idempotency keys', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockImplementation(() => Promise.resolve(jsonResponse({})))
    vi.stubGlobal('fetch', fetchMock)
    const {
      advanceTaskWorkbench,
      cancelWorkbenchExecution,
      commitArtifactExport,
      commitWorkspaceEdit,
      commitWorkspacePatch,
      commitWorkspacePathOperation,
      continueConversationTurn,
      createConversationTurn,
      createResearchWorkbenchTask,
      getTaskWorkbench,
      interpretTaskWorkbench,
      prepareArtifactExport,
      replanTaskWorkbench,
      runWorkbenchStep,
      stopTaskWorkbench,
    } = await import('./api')

    await createResearchWorkbenchTask({
      goal: '研究并生成 HTML', privacy_mode: 'balanced', constraints: [],
    })
    await createConversationTurn({
      message: '用对话研究并生成 HTML', privacy_mode: 'balanced', constraints: [],
    })
    await continueConversationTurn('task/with spaces?#', { message: '更换研究主题' })
    await advanceTaskWorkbench('task/with spaces?#')
    await interpretTaskWorkbench('task/with spaces?#')
    await stopTaskWorkbench('task/with spaces?#')
    await replanTaskWorkbench('task/with spaces?#')
    await commitWorkspaceEdit('task/with spaces?#', 'b'.repeat(64))
    await commitWorkspacePatch('task/with spaces?#', 'c'.repeat(64))
    await commitWorkspacePathOperation('task/with spaces?#', 'd'.repeat(64))
    await getTaskWorkbench('task/with spaces?#')
    await runWorkbenchStep('run/with spaces?#', 'claims:verify')
    await cancelWorkbenchExecution('run/with spaces?#')
    await prepareArtifactExport(
      'delivery/with spaces?#', 'D:\\Reports\\x.md', 'prepare-key-0001', `art_${'e'.repeat(64)}`,
    )
    await commitArtifactExport('export/with spaces?#', 'a'.repeat(64), 'commit-key-00001')

    expectAuthenticatedRequest(fetchMock, 1, '/api/v1/research-workbench/tasks', 'POST')
    expectAuthenticatedRequest(
      fetchMock, 2, '/api/v1/conversation-turns', 'POST',
    )
    expectAuthenticatedRequest(
      fetchMock, 3, '/api/v1/tasks/task%2Fwith%20spaces%3F%23/conversation-turns', 'POST',
    )
    expectAuthenticatedRequest(
      fetchMock, 4, '/api/v1/tasks/task%2Fwith%20spaces%3F%23/workbench:advance', 'POST',
    )
    const interpretInit = expectAuthenticatedRequest(
      fetchMock, 5, '/api/v1/tasks/task%2Fwith%20spaces%3F%23/workbench:interpret-turn', 'POST',
    )
    expect(interpretInit.body).toBeUndefined()
    expectAuthenticatedRequest(
      fetchMock, 6, '/api/v1/tasks/task%2Fwith%20spaces%3F%23/workbench:stop', 'POST',
    )
    expectAuthenticatedRequest(
      fetchMock, 7, '/api/v1/tasks/task%2Fwith%20spaces%3F%23/workbench:replan', 'POST',
    )
    expectAuthenticatedRequest(
      fetchMock, 8, '/api/v1/tasks/task%2Fwith%20spaces%3F%23/workspace-edit:commit', 'POST',
    )
    expectAuthenticatedRequest(
      fetchMock, 9, '/api/v1/tasks/task%2Fwith%20spaces%3F%23/workspace-patch:commit', 'POST',
    )
    expectAuthenticatedRequest(
      fetchMock, 10, '/api/v1/tasks/task%2Fwith%20spaces%3F%23/workspace-path-operation:commit', 'POST',
    )
    expectAuthenticatedRequest(
      fetchMock, 11, '/api/v1/tasks/task%2Fwith%20spaces%3F%23/workbench', undefined,
    )
    expectAuthenticatedRequest(
      fetchMock, 12, '/api/v1/execution-runs/run%2Fwith%20spaces%3F%23/claims:verify', 'POST',
    )
    expectAuthenticatedRequest(
      fetchMock, 13, '/api/v1/execution-runs/run%2Fwith%20spaces%3F%23:cancel', 'POST',
    )
    const prepareInit = expectAuthenticatedRequest(
      fetchMock, 14, '/api/v1/deliveries/delivery%2Fwith%20spaces%3F%23/exports:prepare', 'POST',
    )
    expect(new Headers(prepareInit.headers).get('Idempotency-Key')).toBe('prepare-key-0001')
    expect(prepareInit.body).toBe(JSON.stringify({
      target_path: 'D:\\Reports\\x.md', artifact_id: `art_${'e'.repeat(64)}`,
    }))
    const commitInit = expectAuthenticatedRequest(
      fetchMock, 15, '/api/v1/artifact-exports/export%2Fwith%20spaces%3F%23:commit', 'POST',
    )
    expect(new Headers(commitInit.headers).get('Idempotency-Key')).toBe('commit-key-00001')
  })
})

describe('effect-runtime operations API', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('使用受认证路径、显式查询参数和仅写请求幂等键', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockImplementation(() => Promise.resolve(jsonResponse({})))
    vi.stubGlobal('fetch', fetchMock)

    const {
      getEffectRuntimeAlertNotifications,
      getEffectRuntimeAudit,
      getEffectRuntimeAuditExport,
      getEffectRuntimeOperations,
      requeueOutboxDeadLetter,
      runEffectRuntimeRetention,
      sampleEffectRuntimeMetrics,
    } = await import('./api')

    await getEffectRuntimeOperations(25)
    await getEffectRuntimeAudit(7, 40)
    await getEffectRuntimeAlertNotifications(8, 41)
    await getEffectRuntimeAuditExport('opaque-cursor', 42)
    await sampleEffectRuntimeMetrics(15)
    await runEffectRuntimeRetention(30, 'retention-key-0001')
    await requeueOutboxDeadLetter('message/with spaces?#', 'requeue-key-000001')

    const snapshotInit = expectAuthenticatedRequest(
      fetchMock,
      1,
      '/api/v1/operations/effect-runtime?sample_limit=25',
    )
    expect(snapshotInit.cache).toBe('no-store')

    const auditInit = expectAuthenticatedRequest(
      fetchMock,
      2,
      '/api/v1/operations/effect-runtime/audit?after_sequence=7&limit=40',
    )
    expect(auditInit.cache).toBe('no-store')

    const alertsInit = expectAuthenticatedRequest(
      fetchMock,
      3,
      '/api/v1/operations/effect-runtime/alerts?after_sequence=8&limit=41',
    )
    expect(alertsInit.cache).toBe('no-store')

    const exportInit = expectAuthenticatedRequest(
      fetchMock,
      4,
      '/api/v1/operations/effect-runtime/audit/export?limit=42&cursor=opaque-cursor',
    )
    expect(exportInit.cache).toBe('no-store')

    const sampleInit = expectAuthenticatedRequest(
      fetchMock,
      5,
      '/api/v1/operations/effect-runtime:sample?sample_limit=15',
      'POST',
    )
    expect(sampleInit.body).toBeUndefined()
    expect(new Headers(sampleInit.headers).has('Idempotency-Key')).toBe(false)

    const retentionInit = expectAuthenticatedRequest(
      fetchMock,
      6,
      '/api/v1/operations/effect-runtime:run-retention',
      'POST',
    )
    expect(retentionInit.body).toBe(JSON.stringify({ retention_days: 30 }))
    expect(new Headers(retentionInit.headers).get('Idempotency-Key')).toBe(
      'retention-key-0001',
    )

    const requeueInit = expectAuthenticatedRequest(
      fetchMock,
      7,
      '/api/v1/operations/outbox/message%2Fwith%20spaces%3F%23:requeue',
      'POST',
    )
    expect(requeueInit.body).toBeUndefined()
    expect(new Headers(requeueInit.headers).get('Idempotency-Key')).toBe(
      'requeue-key-000001',
    )
  })
})

describe('managed Provider credential API', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('只在写请求体发送密钥，并为删除绑定精确确认头', async () => {
    const status = {
      schema_version: 'deskpilot.managed-credential-status.v1',
      backend: 'windows_credential_manager',
      identifier: 'OPENAI_RESPONSES',
      state: 'available',
      writable: true,
      deleted: false,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(jsonResponse(status))
      .mockResolvedValueOnce(jsonResponse(status))
      .mockResolvedValueOnce(jsonResponse({ ...status, state: 'missing', deleted: true }))
    vi.stubGlobal('fetch', fetchMock)

    const {
      deleteManagedCredential,
      getManagedCredentialStatus,
      storeManagedCredential,
    } = await import('./api')
    const secret = 'api-spec-secret-only-in-write-body'

    const read = await getManagedCredentialStatus('OPENAI_RESPONSES')
    const stored = await storeManagedCredential('OPENAI_RESPONSES', secret)
    const deleted = await deleteManagedCredential('OPENAI_RESPONSES')

    const readInit = expectAuthenticatedRequest(
      fetchMock,
      1,
      '/api/v1/model-providers/credentials/OPENAI_RESPONSES',
    )
    expect(readInit.cache).toBe('no-store')
    expect(readInit.body).toBeUndefined()

    const storeInit = expectAuthenticatedRequest(
      fetchMock,
      2,
      '/api/v1/model-providers/credentials/OPENAI_RESPONSES',
      'PUT',
    )
    expect(storeInit.body).toBe(JSON.stringify({ secret }))

    const deleteInit = expectAuthenticatedRequest(
      fetchMock,
      3,
      '/api/v1/model-providers/credentials/OPENAI_RESPONSES',
      'DELETE',
    )
    expect(new Headers(deleteInit.headers).get('X-DeskPilot-Credential-Confirmation')).toBe(
      'OPENAI_RESPONSES',
    )
    expect(JSON.stringify({ read, stored, deleted })).not.toContain(secret)
  })
})
