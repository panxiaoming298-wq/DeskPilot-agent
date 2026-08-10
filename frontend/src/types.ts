export type TaskStatus =
  | 'created'
  | 'classifying'
  | 'running'
  | 'waiting_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'paused'

export type TaskControlAction = 'pause' | 'resume' | 'cancel'

export interface TaskControlCommand {
  reason?: string | null
}

export interface FileMoveToolRequest {
  kind: 'file_move'
  source: string
  destination: string
}

export type TaskStreamState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'reauthenticating'
  | 'forbidden'
  | 'not_found'
  | 'archived'

export interface TaskCreate {
  goal: string
  privacy_mode: 'local_only' | 'local_preferred' | 'balanced' | 'quality_first'
  constraints: string[]
  tool_request?: FileMoveToolRequest | null
}

export interface Task {
  task_id: string
  conversation_id: string | null
  goal: string
  status: TaskStatus
  mode: string
  privacy_mode: string
  constraints: string[]
  last_event_seq: number
  event_stream: string
  created_at: string
  updated_at: string
}

export interface TaskHistoryPage {
  items: Task[]
  total: number
  limit: number
  offset: number
}

export interface TaskEvent {
  event_id: string
  task_id: string
  seq: number
  type: string
  timestamp: string
  trace_id: string
  payload: Record<string, unknown>
}

export type ApprovalStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'expired'
  | 'cancelled'

export type ApprovalDecision = 'approved' | 'rejected'
export type ApprovalAction = 'approve' | 'reject'
export type ApprovalRiskLevel = 'R0' | 'R1' | 'R2' | 'R3' | 'R4'

export interface ApprovalResourceScope {
  kind: string
  label: string
  operations: string[]
  version: string | null
}

export interface ApprovalDataEgress {
  enabled: boolean
  destination: string | null
}

export interface Approval {
  approval_id: string
  decision_id: string
  task_id: string
  call_id: string
  status: ApprovalStatus
  decision: ApprovalDecision | null
  preview_hash: string
  title: string
  purpose: string
  tool_name: string
  tool_version: string
  risk_level: ApprovalRiskLevel
  capabilities: string[]
  resource_scope: ApprovalResourceScope[]
  consequences: string[]
  reversible: boolean
  data_egress: ApprovalDataEgress
  policy_rule_id: string
  policy_revision: string
  reason_code: string
  requested_at: string
  expires_at: string
  resolved_at: string | null
  consumed_at: string | null
  resolution_reason: string | null
  updated_at: string
}

export interface ApprovalResolutionCommand {
  preview_hash: string
  scope: 'once'
  reason?: string
}

export interface ApprovalResolutionResponse {
  approval: Approval
  task: Task
  replayed: boolean
}

export type ReconciliationStatus = 'pending' | 'resolved'
export type ReconciliationOutcome =
  | 'confirmed_succeeded'
  | 'confirmed_failed'
  | 'confirmed_no_effect'
  | 'accepted_unknown'
export type ReconciliationEvidenceKind =
  | 'commit_receipt'
  | 'no_receipt'
  | 'query_failed'

export interface ToolCommitReceipt {
  receipt_id: string
  call_id: string
  tool_name: string
  tool_version: string
  status: 'committed'
  authorization_id: string
  approval_id: string
  preview_hash: string
  prepare_digest: string
  idempotency_key_digest: string
  resource_versions_before: Record<string, string>
  resource_versions_after: Record<string, string>
  commit_started_at: string
  receipt_recorded_at: string
}

export interface ReconciliationReceiptEvidence {
  evidence_id: string
  kind: ReconciliationEvidenceKind
  queried_runner_id: string | null
  commit_receipt: ToolCommitReceipt | null
  error_code: string | null
  observed_at: string
}

export interface ToolIdempotencyReceipt {
  receipt_id: string
  call_id: string
  tool_name: string
  tool_version: string
  key_digest: string
  arguments_digest: string
  created_at: string
}

export interface Reconciliation {
  reconciliation_id: string
  task_id: string
  call_id: string
  step_id: string
  attempt: number
  tool_name: string
  tool_version: string
  contract_digest: string
  arguments_digest: string
  idempotency: 'none' | 'key_optional' | 'key_required'
  runner_id: string | null
  call_error_code: string | null
  call_resolution_source: string | null
  call_requested_at: string
  call_started_at: string | null
  call_finished_at: string | null
  status: ReconciliationStatus
  outcome: ReconciliationOutcome | null
  evidence_summary: string | null
  resolved_by: string | null
  unknown_at: string
  resolved_at: string | null
  can_create_attempt: boolean
  new_attempt_task_id: string | null
  new_attempt_created_at: string | null
  can_create_compensation: boolean
  compensation_task_id: string | null
  compensation_receipt_id: string | null
  compensation_created_at: string | null
  idempotency_receipt: ToolIdempotencyReceipt | null
  receipt_evidence: ReconciliationReceiptEvidence[]
  updated_at: string
}

export interface ReconciliationEvidenceRefreshResponse {
  reconciliation: Reconciliation
  evidence: ReconciliationReceiptEvidence
  replayed: boolean
}

export interface ReconciliationCompensationResponse {
  reconciliation: Reconciliation
  task: Task
  replayed: boolean
}

export interface ReconciliationResolutionResponse {
  reconciliation: Reconciliation
  replayed: boolean
}

export interface ReconciliationAttemptResponse {
  reconciliation: Reconciliation
  task: Task
  replayed: boolean
}

export interface PlanStep {
  step_id: string
  agent: string
  title: string
}

export interface LocalSession {
  access_token: string
  token_type: 'Bearer'
  websocket_protocol: string
}

export interface ApiProblem {
  type: string
  title: string
  status: number
  detail: string
  instance: string
  code: string
  errors?: Array<{ pointer: string; detail: string; code: string }>
  current_status?: TaskStatus
  target_status?: TaskStatus
  allowed_statuses?: TaskStatus[]
}

export type ModelProtocol = 'fake' | 'openai_compatible_chat' | 'openai_responses' | 'ollama'
export type ModelLocation = 'local' | 'cloud'
export type ModelRole = 'intent' | 'planner' | 'tool_agent' | 'summarizer' | 'verifier'
export type ProviderHealthStatus = 'ready' | 'degraded' | 'unavailable'

export interface ModelCapabilities {
  streaming: boolean
  structured_output: boolean
  strict_json_schema: boolean
  tool_calling: 'none' | 'prompted' | 'native'
  parallel_tool_calls: boolean
  vision: boolean
  embeddings: boolean
  max_context_tokens: number
}

export interface ModelProviderDescriptor {
  provider_id: string
  display_name: string
  model: string
  protocol: ModelProtocol
  location: ModelLocation
  capabilities: ModelCapabilities
}

export interface ProviderHealthSnapshot {
  provider_id: string
  status: ProviderHealthStatus
  checked_at: string
  latency_ms: number | null
  cache_status: 'fresh' | 'cached' | 'coalesced'
  expires_at: string
}

export interface ProviderCatalogEntry {
  descriptor: ModelProviderDescriptor
  enabled: boolean
  is_default: boolean
  cached_health: ProviderHealthSnapshot | null
}

export interface ProviderCatalogSnapshot {
  catalog_version: number
  imported_at: string
  default_provider_id: string
  providers: ProviderCatalogEntry[]
}

export interface CredentialReference {
  backend: 'environment' | 'windows_credential_manager'
  identifier: string
}

export interface FakeProviderConfig {
  kind: 'fake'
  enabled: boolean
  provider_id: string
  display_name: string
  model: string
  delay_seconds: number
}

export interface OpenAICompatibleProviderConfig {
  kind: 'openai_compatible_chat'
  enabled: boolean
  provider_id: string
  display_name: string
  model: string
  base_url: string
  location: ModelLocation
  credential_ref: CredentialReference | null
  allow_private_network: boolean
  supports_streaming: boolean
  supports_structured_output: boolean
  supports_strict_json_schema: boolean
  max_context_tokens: number
  max_tokens_field: 'max_tokens' | 'max_completion_tokens'
  max_response_bytes: number
  health_timeout_seconds: number
}

export type ProviderConfig = FakeProviderConfig | OpenAICompatibleProviderConfig

export interface ProviderMutationResult {
  action: 'created' | 'updated' | 'enabled' | 'disabled' | 'default_changed' | 'deleted'
  provider_id: string
  catalog_version: number
  config_revision: number
  default_provider_id: string
  credential_disposition:
    | 'not_applicable'
    | 'reference_unchanged'
    | 'reference_attached'
    | 'reference_changed_old_retained'
    | 'reference_removed_old_retained'
    | 'provider_deleted_credential_retained'
  replayed: boolean
}

export interface ProviderConfigAuditEvent {
  sequence: number
  event_id: string
  provider_id: string
  action: ProviderMutationResult['action']
  source: 'startup_import' | 'local_api'
  actor_type: 'system' | 'local_user'
  config_revision: number
  changed_fields: string[]
  credential_disposition: ProviderMutationResult['credential_disposition']
  correlation_id: string | null
  occurred_at: string
}

export interface ProviderConfigAuditPage {
  events: ProviderConfigAuditEvent[]
  next_sequence: number
}

export interface ModelRoleRouteSnapshot {
  role: ModelRole
  provider_ids: string[]
  strategy: 'priority' | 'latency_aware'
  configured: boolean
}

export interface ModelProviderPricing {
  provider_id: string
  input_micros_per_million_tokens: number
  cached_input_micros_per_million_tokens: number | null
  output_micros_per_million_tokens: number
}

export interface ModelProviderRoutingSnapshot {
  provider_id: string
  circuit_state: 'closed' | 'open' | 'half_open'
  latency_ewma_ms: number | null
  consecutive_failures: number
  request_count: number
  failure_count: number
  retry_count: number
  total_cost_micros: number
  retry_after_until: string | null
  circuit_open_until: string | null
  last_error_code: string | null
  pricing: ModelProviderPricing | null
}

export interface ModelGatewayRoutingSnapshot {
  generated_at: string
  default_provider_id: string
  default_max_attempts: number
  default_retry_delay_budget_seconds: number
  default_task_cost_budget_micros: number | null
  latency_ewma_alpha: number
  circuit_failure_threshold: number
  circuit_recovery_timeout_seconds: number
  active_task_budget_count: number
  routes: ModelRoleRouteSnapshot[]
  providers: ModelProviderRoutingSnapshot[]
}
