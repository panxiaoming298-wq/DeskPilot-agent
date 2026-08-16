export type TaskStatus =
  | 'created'
  | 'classifying'
  | 'running'
  | 'waiting_approval'
  | 'waiting_reconciliation'
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

export interface DiskPressureGuardedFileMoveToolRequest {
  kind: 'disk_pressure_guarded_file_move'
  source: string
  destination: string
  maximum_used_percent: number
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
  tool_request?: FileMoveToolRequest | DiskPressureGuardedFileMoveToolRequest | null
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
export type GraphRecoveryStatus = 'not_applicable' | 'pending' | 'applied'
export type GraphRecoveryAction = 'continue' | 'terminate'
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
  graph_recovery_status: GraphRecoveryStatus
  graph_recovery_action: GraphRecoveryAction | null
  graph_recovery_event_id: string | null
  graph_recovered_at: string | null
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

export interface EffectGraphRecoverySnapshot {
  graph_id: string
  task_id: string
  status: string
  fencing_token: number
  revision: number
}

export interface ReconciliationGraphRecoveryResponse {
  reconciliation: Reconciliation
  task: Task
  graph: EffectGraphRecoverySnapshot
  replayed: boolean
  resumed: boolean
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

export type OperationsAlertSeverity = 'warning' | 'critical'

export interface OperationsAlert {
  code: string
  severity: OperationsAlertSeverity
  domain: string
  count: number
}

export type OperationsAlertTransition = 'opened' | 'updated' | 'resolved'

export interface OperationsAlertNotification {
  notification_id: string
  sequence: number
  alert_code: string
  transition: OperationsAlertTransition
  severity: OperationsAlertSeverity
  domain: string
  count: number
  alert_revision: number
  snapshot_digest: string
  audit_event_id: string
  audit_sequence: number
  previous_event_digest: string | null
  event_digest: string
  occurred_at: string
}

export interface OperationsAlertNotificationPage {
  notifications: OperationsAlertNotification[]
  next_after_sequence: number
  has_more: boolean
}

export interface GraphControlOperationsMetrics {
  total: number
  pending: number
  processing: number
  applied: number
  superseded: number
  actionable: number
  claim_expired: number
  unrouted: number
  oldest_actionable_at: string | null
}

export interface AdmissionOperationsMetrics {
  total: number
  pending: number
  granted: number
  released: number
  cancelled: number
  withdrawn: number
  expired: number
  live_pending: number
  live_granted: number
  expired_leases: number
  scheduler_revision: number
  next_grant_sequence: number
  configuration_digest: string | null
  global_limit: number | null
  per_graph_limit: number | null
  default_tool_limit: number | null
}

export interface ReadyProjectionOperationsMetrics {
  projected_graphs: number
  projected_nodes: number
  ready_nodes: number
  missing_live_graphs: number
  event_drift_graphs: number
  row_count_drift_graphs: number
  rebuilds_observed: number
  last_rebuilt_at: string | null
}

export interface OutboxOperationsMetrics {
  total: number
  pending_ready: number
  retry_scheduled: number
  in_flight: number
  published: number
  dead_lettered: number
  inbox_receipts: number
  oldest_pending_at: string | null
  oldest_dead_lettered_at: string | null
}

export interface GraphControlOperationsRead {
  control_id: string
  task_id: string
  graph_id: string
  command: string
  request_digest: string
  status: string
  revision: number
  attempt_count: number
  target_owner_id: string | null
  target_fencing_token: number | null
  claim_owner_id: string | null
  claim_fencing_token: number
  claim_expires_at: string | null
  last_error_code: string | null
  updated_at: string
}

export interface AdmissionOperationsRead {
  admission_id: string
  batch_id: string
  graph_id: string
  node_id: string
  tool_name: string
  owner_id: string
  status: string
  revision: number
  fencing_token: number
  grant_sequence: number | null
  expires_at: string
  updated_at: string
}

export interface ReadyProjectionOperationsRead {
  graph_id: string
  graph_status: string
  graph_event_seq: number
  projection_revision: number
  projection_event_seq: number
  content_digest: string
  rebuild_count: number
  last_rebuild_duration_ms: number | null
  projected_nodes: number
  dependency_ready_nodes: number
  rebuilt_at: string
  updated_at: string
}

export type OutboxOperationsState = 'pending' | 'in_flight' | 'published' | 'dead_lettered'

export interface OutboxOperationsRead {
  message_id: string
  task_id: string
  event_id: string
  event_seq: number
  topic: string
  state: OutboxOperationsState
  payload_digest: string
  attempt_count: number
  claim_owner_id: string | null
  claim_fencing_token: number
  available_at: string
  claim_expires_at: string | null
  published_at: string | null
  dead_lettered_at: string | null
  error_digest: string | null
  created_at: string
}

export interface EffectRuntimeOperationsSnapshot {
  schema_version: 'deskpilot.effect-runtime-operations.v1'
  database_time: string
  graph_controls: GraphControlOperationsMetrics
  admissions: AdmissionOperationsMetrics
  ready_projection: ReadyProjectionOperationsMetrics
  outbox: OutboxOperationsMetrics
  alerts: OperationsAlert[]
  graph_control_samples: GraphControlOperationsRead[]
  admission_samples: AdmissionOperationsRead[]
  ready_projection_samples: ReadyProjectionOperationsRead[]
  outbox_samples: OutboxOperationsRead[]
  snapshot_digest: string
}

export interface EffectRuntimeAuditEvent {
  event_id: string
  sequence: number
  action: string
  actor_id: string
  request_digest: string
  result_digest: string
  previous_event_digest: string | null
  event_digest: string
  details: Record<string, unknown>
  occurred_at: string
}

export interface EffectRuntimeAuditPage {
  events: EffectRuntimeAuditEvent[]
  next_after_sequence: number
  has_more: boolean
}

export interface EffectRuntimeAuditExportPage {
  schema_version: 'deskpilot.effect-runtime-audit-export.v1'
  export_id: string
  database_time: string
  through_sequence: number
  through_event_digest: string | null
  events: EffectRuntimeAuditEvent[]
  page_digest: string
  next_cursor: string | null
  has_more: boolean
}

export interface MetricsAuditResult {
  snapshot: EffectRuntimeOperationsSnapshot
  audit_event: EffectRuntimeAuditEvent
  alert_notifications: OperationsAlertNotification[]
}

export interface RetentionCounts {
  graph_controls: number
  admissions: number
  ready_checkpoints: number
  ready_nodes: number
  ready_states: number
  published_outbox: number
  inbox_receipts: number
}

export interface RetentionRunResult {
  cutoff: string
  counts: RetentionCounts
  manifest_digest: string
  audit_event: EffectRuntimeAuditEvent
}

export interface OutboxRequeueResult {
  message_id: string
  attempt_count: number
  claim_fencing_token: number
  available_at: string
  audit_event: EffectRuntimeAuditEvent
}
