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

export interface KnowledgeSource {
  source_id: string
  canonical_path: string
  artifact_id: string
  source_version: string
  content_digest: string
  byte_size: number
  chunk_count: number
  manifest_digest: string
  imported_at: string
  updated_at: string
}

export interface KnowledgeCitation {
  source_id: string
  artifact_id: string
  chunk_id: string
  canonical_path: string
  locator: string
  snippet: string
  score: number
  text_digest: string
  chunk_proof_digest: string
  retrieval_proof_digest: string
}

export interface KnowledgeSearchResult {
  query_digest: string
  citations: KnowledgeCitation[]
  searched_sources: number
  stale_source_ids: string[]
  result_digest: string
}

export interface McpTool {
  name: string
  title: string
  description: string
  risk_floor: 'R0' | 'R1' | 'R2' | 'R3' | 'R4'
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  schema_digest: string
}

export interface McpServer {
  server_id: string
  title: string
  transport: 'stdio'
  protocol_version: string
  command_preview: string[]
  enabled: boolean
  revision: number
  network_access: boolean
  filesystem_roots: string[]
  client_capabilities: string[]
  tools: McpTool[]
  bundle_digest: string
  manifest_digest: string
  updated_at: string | null
}

export interface McpServerMutation {
  server: McpServer
  audit_event_id: string | null
}

export interface McpToolCallResult {
  server_id: string
  tool_name: string
  protocol_version: string
  structured_content: Record<string, unknown>
  request_digest: string
  result_digest: string
  audit_event_id: string
}

export interface McpAuditEvent {
  event_id: string
  sequence: number
  server_id: string
  action: 'enabled' | 'disabled' | 'tool_called' | 'tool_failed'
  request_digest: string
  result_digest: string
  previous_event_digest: string | null
  event_digest: string
  details: Record<string, unknown>
  occurred_at: string
}

export interface McpAuditPage {
  events: McpAuditEvent[]
  next_after_sequence: number
}

export interface EvaluationTrace {
  sequence: number
  case_id: string
  scenario: string
  status: 'passed' | 'failed'
  input_digest: string
  output_digest: string
  error_code: string | null
  duration_ms: number
  previous_event_digest: string | null
  event_digest: string
}

export interface EvaluationRun {
  run_id: string
  suite_id: string
  suite_version: number
  suite_digest: string
  status: 'passed' | 'failed'
  replay_of_run_id: string | null
  replay_match: boolean | null
  case_count: number
  passed_count: number
  failed_count: number
  safety_case_count: number
  safety_passed_count: number
  success_rate: number
  safety_rate: number
  duration_ms: number
  result_manifest: Record<string, unknown>
  manifest_digest: string
  traces: EvaluationTrace[]
  started_at: string
  completed_at: string
}

export interface EvaluationRunPage { runs: EvaluationRun[] }

export interface EvaluationTrendPoint {
  run_id: string
  status: 'passed' | 'failed'
  success_rate: number
  safety_rate: number
  duration_ms: number
  replay_of_run_id: string | null
  started_at: string
}

export interface EvaluationReport {
  schema_version: 'deskpilot.evaluation-report.v1'
  suite_id: string | null
  suite_version: number | null
  suite_digest: string | null
  as_of: string | null
  run_count: number
  passed_run_count: number
  failed_run_count: number
  run_success_rate: number
  run_duration_p50_ms: number | null
  run_duration_p95_ms: number | null
  case_duration_p50_ms: number | null
  case_duration_p95_ms: number | null
  failure_counts: Record<string, number>
  trend: EvaluationTrendPoint[]
  report_digest: string
}

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

export type LongTermMemoryKind =
  | 'preference'
  | 'restrictive_permission'
  | 'user_confirmed_fact'
  | 'verified_episode'
  | 'skill_template'

export type LongTermMemoryStatus =
  | 'proposal'
  | 'pending_confirmation'
  | 'confirmed'
  | 'active'
  | 'conflict'
  | 'expired'
  | 'deleted'
  | 'rejected'

export interface MemoryProposal {
  proposal_id: string
  key: string
  kind: LongTermMemoryKind
  value: string | null
  source_type: 'user_explicit' | 'agent_result' | 'verified_delivery'
  source_id: string
  source_digest: string
  created_by: 'user' | 'agent' | 'system'
  scope: 'user'
  classification: 'public' | 'internal' | 'sensitive'
  confidence: number
  status: LongTermMemoryStatus
  value_digest: string
  proposal_digest: string
  created_at: string
  expires_at: string | null
  decided_at: string | null
}

export interface LongTermMemoryItem {
  memory_id: string
  proposal_id: string
  key: string
  version: number
  kind: LongTermMemoryKind
  value: string | null
  source_type: MemoryProposal['source_type']
  source_id: string
  source_digest: string
  created_by: MemoryProposal['created_by']
  scope: 'user'
  classification: MemoryProposal['classification']
  confidence: number
  status: LongTermMemoryStatus
  value_digest: string
  item_digest: string
  supersedes_memory_id: string | null
  created_at: string
  expires_at: string | null
  deleted_at: string | null
}

export interface MemoryConflict {
  conflict_id: string
  key: string
  kind: LongTermMemoryKind
  memory_ids: string[]
  status: 'open' | 'resolved'
  selected_memory_id: string | null
  conflict_digest: string
  created_at: string
  resolved_at: string | null
}

export interface MemoryUsage {
  usage_id: string
  memory_id: string
  memory_version: number
  task_id: string
  invocation_id: string
  context_manifest_id: string
  agent_id: string
  provider_id: string
  provider_location: string
  purpose: string
  supplied_at: string
  policy_reference: string
  deleted_after_use: boolean
}

export interface LongTermMemoryPage {
  items: LongTermMemoryItem[]
  proposals: MemoryProposal[]
  conflicts: MemoryConflict[]
  usage: MemoryUsage[]
}

export interface CreateLongTermMemory {
  key: string
  kind: LongTermMemoryKind
  value: string
  classification: MemoryProposal['classification']
  expires_at?: string
  verified_delivery_id?: string
}

export interface LongTermMemoryExport extends LongTermMemoryPage {
  schema_version: 'deskpilot.long-term-memory-export.v1'
  exported_at: string
  tombstones: Record<string, unknown>[]
  export_digest: string
}

export type WorkbenchStage =
  | 'idle'
  | 'interpreting'
  | 'planned'
  | 'researching'
  | 'awaiting_verification'
  | 'building_artifact'
  | 'verifying_browser'
  | 'ready_to_deliver'
  | 'delivered'
  | 'exported'
  | 'executing'
  | 'needs_clarification'
  | 'needs_user_action'
  | 'unsupported'
  | 'blocked'

export type WorkbenchAction =
  | 'interpret_turn'
  | 'activate_research_plan'
  | 'start_execution'
  | 'run_research'
  | 'verify_claims'
  | 'build_artifact'
  | 'verify_browser'
  | 'finalize_delivery'
  | 'execute_route'
  | 'replan_failed_execution'
  | 'commit_workspace_edit'
  | 'commit_workspace_patch'
  | 'commit_workspace_path_operation'
  | 'prepare_export'
  | 'stop_execution'

export type WorkbenchStepCommand =
  | 'research:run'
  | 'claims:verify'
  | 'artifacts:build'
  | 'browser:verify'
  | 'final-acceptance:run'

export interface WorkbenchActionState {
  action: WorkbenchAction
  enabled: boolean
  reason_code: string
  explanation: string
  effect_class: 'read_only' | 'workspace_write' | 'user_path_write' | 'execution_control'
}

export interface AgentReplanFailureSnapshot {
  schema_version:
    | 'deskpilot.agent-replan-failure-snapshot.v1'
    | 'deskpilot.agent-replan-failure-snapshot.v2'
  task_id: string
  source_run_id: string
  source_plan_generation: number
  source_plan_digest: string
  contract_version: number
  contract_digest: string
  route_id: 'workspace_directory_analyze' | 'workspace_dynamic_patch_test'
  route_parameter_digest: string
  route_revision: number
  stable_error_code:
    | 'AGENT_TASK_GRAPH_REJECTED'
    | 'AGENT_ROUTE_BINDING_REJECTED'
    | 'AGENT_LOOP_NO_PROGRESS'
    | 'AGENT_GRAPH_TEST_CONDITION_NOT_MET'
  failed_node_ids: string[]
  failed_invocation_ids: string[]
  failed_model_turn_ids: string[]
  condition_decision_digests?: string[]
  snapshot_digest: string
}

export interface AgentTaskGraphResultRef {
  schema_version: 'deskpilot.agent-task-graph-result-ref.v1'
  graph_id: string
  producer_local_key: string
  producer_node_id: string
  producer_invocation_id: string
  producer_result_id: string
  capability: { capability_id: string; version: string; digest: string }
  result_kind: 'file' | 'directory' | 'python_test' | 'node_test' | 'patch_test'
  agent_result_digest: string
  workspace_result_digest: string
  result_ref_digest: string
}

export interface AgentTaskGraphCondition {
  schema_version: 'deskpilot.agent-task-graph-condition.v1'
  source_local_key: string
  source_node_id: string
  predicate: 'test_passed'
  condition_digest: string
}

export interface AgentTaskGraphConditionDecision {
  schema_version: 'deskpilot.agent-task-graph-condition-decision.v1'
  graph_id: string
  source_local_key: string
  source_node_id: string
  target_local_key: string
  target_node_id: string
  predicate: 'test_passed'
  actual_status: 'passed' | 'failed' | 'error' | 'verified' | 'test_failed' | 'test_error'
  result_ref_digest: string
  matched: boolean
  decision_digest: string
}

export interface AgentReplanRepairAdvice {
  schema_version:
    | 'deskpilot.agent-replan-repair-advice.v1'
    | 'deskpilot.agent-replan-repair-advice.v2'
  failure_snapshot_digest: string
  stable_error_code: AgentReplanFailureSnapshot['stable_error_code']
  strategy_code:
    | 'rebuild_graph_from_current_offer'
    | 'reuse_verified_evidence_and_rebind_route'
    | 'simplify_graph_and_consume_verified_evidence'
    | 'propose_fresh_patch_after_failed_test'
  objective: string
  granted_capability_ids: string[]
  result_sources: Array<{
    schema_version: 'deskpilot.agent-replan-result-source.v1'
    source_key: string
    source_run_id: string
    source_plan_generation: number
    source_plan_digest: string
    source_graph_digest: string
    result_ref: AgentTaskGraphResultRef
    source_digest: string
  }>
  advice_digest: string
}

export interface AgentReplan {
  schema_version:
    | 'deskpilot.agent-replan.v1'
    | 'deskpilot.agent-replan.v2'
    | 'deskpilot.agent-replan.v3'
    | 'deskpilot.agent-replan.v4'
    | 'deskpilot.agent-replan.v5'
  replan_id: string
  task_id: string
  source_run_id: string
  source_plan_generation: number
  source_plan_digest: string
  target_run_id: string
  target_plan_generation: number
  target_plan_digest: string
  contract_version: number
  contract_digest: string
  failure_snapshot: AgentReplanFailureSnapshot
  repair_advice: AgentReplanRepairAdvice | null
  continuation_intent?: {
    schema_version: 'deskpilot.agent-replan-continuation-intent.v1'
    task_id: string
    message_id: string
    message_digest: string
    intent_code: 'continue_failed_patch_repair'
    requested_via: 'conversation_turn' | 'workbench_action'
    intent_digest: string
  }
  budget_proof?: {
    schema_version: 'deskpilot.agent-replan-budget-proof.v1'
    contract_digest: string
    maximum_plan_generations: number
    source_plan_generation: number
    target_plan_generation: number
    budget_limit: AgentReplanBudgetTotals
    allocated_before: AgentReplanBudgetTotals
    target_plan_allocation: AgentReplanBudgetTotals
    allocated_after_activation: AgentReplanBudgetTotals
    remaining_after_activation: AgentReplanBudgetTotals
    budget_digest: string
  }
  status: 'activated'
  created_at: string
  replan_digest: string
}

export interface AgentReplanBudgetTotals {
  model_calls: number
  tool_calls: number
  input_tokens: number
  output_tokens: number
  wall_seconds: number
  retries: number
  cost_micros: number
  handoffs: number
}

export interface AgentRepairLoopStatus {
  schema_version: 'deskpilot.agent-repair-loop-status.v1'
  task_id: string
  current_plan_generation: number
  maximum_plan_generations: number
  remaining_replans: number
  budget_limit: AgentReplanBudgetTotals
  budget_allocated: AgentReplanBudgetTotals
  budget_remaining: AgentReplanBudgetTotals
  next_plan_allocation: AgentReplanBudgetTotals
  next_replan_available: boolean
  reason_code:
    | 'AVAILABLE'
    | 'GENERATION_LIMIT_REACHED'
    | 'CROSS_GENERATION_BUDGET_EXHAUSTED'
  status_digest: string
}

export interface WorkbenchNode {
  node_id: string
  local_key: string
  status: string
  revision: number
  attempt_count: number
  claim_owner_id: string | null
  claim_fencing_token: number
  claim_expires_at: string | null
  bound_agent: { agent_id: string } | null
  depends_on?: string[]
  handoff_parent_node_id?: string | null
  budget?: {
    model_calls: number
    tool_calls: number
    input_tokens: number
    output_tokens: number
    wall_seconds: number
    retries: number
    cost_micros: number
    handoffs: number
  }
  runtime_enabled: boolean
}

export interface WorkbenchRun {
  run_id: string
  task_id: string
  status: string
  revision: number
  nodes: WorkbenchNode[]
  invocations: Array<{
    invocation_id: string
    parent_invocation_id?: string | null
    execution_status: string
    verification_status: string
    result_id?: string | null
  }>
  model_turns: Array<{
    turn_id: string
    invocation_id?: string
    turn_no: number
    status: string
    decision_kind: 'request_route' | 'submit_result' | 'needs_user_input' | 'propose_handoff' | 'propose_task_graph' | null
    decision_digest: string | null
    binding_id: string | null
    observation_digest: string | null
  }>
  input_requests?: Array<{
    input_request_id: string
    question_code: string
    question: string
    blocking_fields: string[]
    answer_schema: string
    request_digest: string
    status: 'pending' | 'resolved' | 'cancelled'
    resolved_task_id: string | null
    answer_digest: string | null
  }>
  delegations?: Array<{
    delegation_id: string
    parent_invocation_id: string
    child_invocation_id: string | null
    parent_node_id: string
    child_node_id: string
    decision_id: string
    binding_id: string
    status: 'waiting_child' | 'child_verified' | 'consumed' | 'cancelled' | 'failed'
    depth: number
    child_result_id: string | null
    observation_id: string | null
    budget_allocation: NonNullable<WorkbenchNode['budget']>
  }>
  task_graphs?: Array<{
    schema_version:
      | 'deskpilot.agent-task-graph.v1'
      | 'deskpilot.agent-task-graph.v2'
      | 'deskpilot.agent-task-graph.v3'
      | 'deskpilot.agent-task-graph.v4'
      | 'deskpilot.agent-task-graph.v5'
      | 'deskpilot.agent-task-graph.v6'
      | 'deskpilot.agent-task-graph.v7'
      | 'deskpilot.agent-task-graph.v8'
    graph_id: string
    binding_id: string
    parent_invocation_id: string
    parent_node_id: string
    decision_id: string
    status: 'running' | 'verified' | 'consumed' | 'cancelled' | 'failed'
    node_count: number
    max_depth: number
    graph_digest: string
    output_local_key: string | null
    output_node_id: string | null
    observation_id: string | null
    nodes: Array<{
      local_key: string
      node_id: string
      binding_id: string
      status: 'waiting_child' | 'child_verified' | 'consumed' | 'cancelled' | 'failed'
      depends_on: string[]
      target_agent: { agent_id: string; version: string }
      capability: { capability_id: string; version: string; digest: string }
      capability_input: {
        schema_version:
          | 'deskpilot.agent-task-graph-capability-input.v1'
          | 'deskpilot.agent-task-graph-capability-input.v2'
          | 'deskpilot.agent-task-graph-capability-input.v3'
          | 'deskpilot.agent-task-graph-capability-input.v4'
        source_key:
          | 'route_directory_path'
          | 'route_explicit_file_path'
          | 'route_python_test_spec'
          | 'route_node_test_spec'
          | 'route_patch_test_spec'
        source_ref: string
        read_kind: 'file' | 'directory' | 'python_test' | 'node_test' | 'patch_test'
        path: string
        test_path: string | null
        target_path: string | null
        test_kind: 'python' | 'node' | null
        objective: string | null
        binding_key?: string | null
        route_parameter_digest: string
        input_digest: string
      } | null
      conditions?: AgentTaskGraphCondition[]
      condition_decisions?: AgentTaskGraphConditionDecision[]
      import_sources?: string[]
      imported_result_refs?: AgentTaskGraphResultRef[]
      approval_binding?: {
        schema_version: 'deskpilot.agent-task-graph-approval-binding.v1'
        approval_binding_id: string
        approval_kind: 'workspace_patch'
        graph_id: string
        local_key: string
        node_id: string
        capability_input_digest: string
        confirmation_policy: 'fresh_user_confirmation_per_node_v1'
        manifest_policy: 'content_addressed_workspace_manifest_v1'
        approval_binding_digest: string
      } | null
      budget_allocation: NonNullable<WorkbenchNode['budget']>
      child_invocation_id: string | null
      child_result_id: string | null
      result_ref: AgentTaskGraphResultRef | null
      test_result: WorkspacePythonTestRead | WorkspaceNodeTestRead | null
      approval: WorkspacePatchPreview | null
      patch_result: WorkspacePatchTestRead | null
    }>
    created_at: string
    updated_at: string
  }>
  created_at: string
  updated_at: string
}

export interface WorkbenchClaim {
  claim_id: string
  statement: string
  citation_ids: string[]
  status: string
}

export interface WorkbenchCitation {
  citation_id: string
  claim_id: string
  locator_text: string
  status: string
}

export interface TurnRoute {
  schema_version: 'deskpilot.turn-route.v1'
  task_id: string
  conversation_id: string
  user_message_id: string
  decision: 'routed' | 'needs_clarification' | 'unsupported'
  route_id:
    | 'research_to_html'
    | 'knowledge_lookup'
    | 'mcp_text_metrics'
    | 'workspace_file_read'
    | 'workspace_file_replace'
    | 'workspace_patch_bundle'
    | 'workspace_agent_patch_test'
    | 'workspace_dynamic_patch_test'
    | 'workspace_file_create'
    | 'workspace_file_rename'
    | 'workspace_directory_list'
    | 'workspace_directory_analyze'
    | 'workspace_snapshot_check'
    | 'workspace_python_test'
    | 'workspace_node_test'
    | null
  route_version: '1' | '2' | null
  route_manifest_digest: string | null
  turn_planning_adjudication_id: string | null
  turn_plan_binding_id: string | null
  turn_planning_provenance_digest: string | null
  candidate_digest: string
  parameter_digest: string
  resolved_from_task_id: string | null
  resolution_rule: string | null
  resolution_digest: string | null
  reason_code: string
  status:
    | 'ready'
    | 'running'
    | 'waiting_user_input'
    | 'needs_user_action'
    | 'succeeded'
    | 'failed'
    | 'not_applicable'
  result_digest: string | null
  error_code: string | null
  revision: number
  created_at: string
  updated_at: string
}

export type TurnPlannerRunStatus =
  | 'prepared'
  | 'dispatching'
  | 'succeeded'
  | 'failed'
  | 'outcome_unknown'
  | 'cancelled'

export interface TurnPlannerFailureProof {
  schema_version: 'deskpilot.turn-planner-failure-proof.v1'
  error_code:
    | 'PLANNER_TIMEOUT'
    | 'PLANNER_SCHEMA_REJECTED'
    | 'PLANNER_UNKNOWN_OFFER'
    | 'PLANNER_PROVIDER_UNAVAILABLE'
    | 'PLANNER_BINDING_REJECTED'
    | 'PLANNER_OUTCOME_UNKNOWN'
    | 'PLANNER_CANCELLED'
  detail_digest: string
  retry_policy: 'never_automatic'
  failure_digest: string
}

export interface TurnPlannerRunWorkbenchSummary {
  schema_version: 'deskpilot.turn-planner-run-workbench-summary.v1'
  status: TurnPlannerRunStatus
  offer_count: number
  offer_set_digest: string
  request_digest: string
  response_digest: string | null
  failure: TurnPlannerFailureProof | null
  revision: number
  run_digest: string
}

export interface TurnPlannerAdjudicationWorkbenchSummary {
  schema_version: 'deskpilot.turn-planner-adjudication-workbench-summary.v1'
  outcome:
    | 'single_step'
    | 'multi_step_deferred'
    | 'deterministic_fallback'
    | 'needs_user_input'
    | 'unsupported'
  selected_offer_count: number
  reason_code: string
  adjudication_digest: string
}

export interface TurnPlanBindingWorkbenchSummary {
  schema_version: 'deskpilot.turn-plan-binding-workbench-summary.v1'
  status: 'bound' | 'multi_step_deferred' | 'not_applicable'
  reason_code: string
  binding_digest: string
}

export interface TurnPlanningWorkbenchRead {
  schema_version: 'deskpilot.turn-planning-workbench-read.v1'
  run: TurnPlannerRunWorkbenchSummary
  adjudication: TurnPlannerAdjudicationWorkbenchSummary | null
  binding: TurnPlanBindingWorkbenchSummary | null
  revision: number
  planning_digest: string
}

export interface ArtifactExport {
  export_id: string
  delivery_id: string
  task_id: string
  artifact_id: string
  revision_id: string
  target_path: string
  conflict_policy: 'fail_if_exists'
  status: 'prepared' | 'committing' | 'committed' | 'failed'
  source_digest: string
  request_digest: string
  confirmation_digest: string
  receipt_digest: string | null
  byte_count: number
  error_code: string | null
  requested_at: string
  committed_at: string | null
}

export interface WorkspaceFileRead {
  schema_version: 'deskpilot.workspace-file-read.v1'
  relative_path: string
  byte_count: number
  content_digest: string
  version_digest: string
  content: string
  result_digest: string
}

export interface WorkspaceDirectoryEntry {
  name: string
  relative_path: string
  kind: 'directory' | 'file'
  byte_count: number | null
  version_digest: string
}

export interface WorkspaceDirectoryRead {
  schema_version: 'deskpilot.workspace-directory-read.v1'
  relative_path: string
  entries: WorkspaceDirectoryEntry[]
  truncated: boolean
  result_digest: string
}

export interface WorkspaceCheckIssue {
  relative_path: string
  line: number
  column: number
  code: 'JSON_INVALID' | 'PYTHON_SYNTAX_INVALID'
  message: string
}

export interface WorkspaceCheckRead {
  schema_version: 'deskpilot.workspace-check.v1'
  profile: 'json-parse' | 'python-syntax'
  relative_path: string
  snapshot_digest: string
  status: 'failed' | 'passed'
  checked_file_count: number
  issues: WorkspaceCheckIssue[]
  isolation_mode: 'windows_appcontainer'
  network_access: false
  output_truncated: boolean
  result_digest: string
}

export interface WorkspacePythonTestRead {
  schema_version: 'deskpilot.workspace-python-test.v1'
  profile: 'pytest-file'
  project_path: string
  test_path: string
  snapshot_digest: string
  runtime_digest: string
  status: 'error' | 'failed' | 'passed'
  exit_code: number
  passed_count: number
  failed_count: number
  skipped_count: number
  error_count: number
  duration_ms: number
  output: string
  output_truncated: boolean
  isolation_mode: 'windows_appcontainer'
  network_access: false
  process_limit: 1
  result_digest: string
}

export interface WorkspaceNodeTestRead {
  schema_version: 'deskpilot.workspace-node-test.v1'
  profile: 'node-test-file'
  project_path: string
  test_path: string
  snapshot_digest: string
  runtime_digest: string
  status: 'error' | 'failed' | 'passed'
  exit_code: number
  passed_count: number
  failed_count: number
  skipped_count: number
  error_count: number
  duration_ms: number
  output: string
  output_truncated: boolean
  isolation_mode: 'windows_appcontainer'
  network_access: false
  process_limit: 1
  result_digest: string
}

export interface WorkspaceEditPreview {
  schema_version: 'deskpilot.workspace-edit-preview.v1'
  task_id: string
  relative_path: string
  expected_version_digest: string
  proposed_content_digest: string
  replacement_count: 1
  byte_count: number
  old_text: string
  new_text: string
  confirmation_digest: string
}

export interface WorkspaceEditReceipt {
  schema_version: 'deskpilot.workspace-edit-receipt.v1'
  task_id: string
  relative_path: string
  confirmation_digest: string
  previous_version_digest: string
  version_digest: string
  content_digest: string
  backup_relative_path: string
  byte_count: number
  committed_at: string
  receipt_digest: string
}

export interface WorkspacePatchChangePreview {
  index: number
  relative_path: string
  expected_version_digest: string
  original_content_digest: string
  proposed_content_digest: string
  byte_count: number
  old_text: string
  new_text: string
  change_digest: string
}

export interface WorkspacePatchPreview {
  schema_version: 'deskpilot.workspace-patch-preview.v1'
  task_id: string
  changes: WorkspacePatchChangePreview[]
  staging_workspace_ref: string
  manifest_digest: string
  total_byte_count: number
  confirmation_digest: string
}

export interface WorkspacePatchReceipt {
  schema_version: 'deskpilot.workspace-patch-receipt.v1'
  task_id: string
  status: 'committed' | 'partial'
  confirmation_digest: string
  change_receipts: WorkspaceEditReceipt[]
  failed_path: string | null
  error_code: string | null
  committed_at: string
  receipt_digest: string
}

export interface WorkspacePatchTestRead {
  schema_version: 'deskpilot.workspace-patch-test.v1'
  task_id: string
  status: 'verified' | 'test_failed' | 'test_error'
  test_kind: 'python' | 'node'
  confirmation_digest: string
  patch_receipt: WorkspacePatchReceipt
  python_test: WorkspacePythonTestRead | null
  node_test: WorkspaceNodeTestRead | null
  error_code: string | null
  result_digest: string
}

export interface WorkspacePathOperationPreview {
  schema_version: 'deskpilot.workspace-path-operation-preview.v1'
  task_id: string
  operation: 'create' | 'rename'
  source_path: string | null
  target_path: string
  expected_source_version_digest: string | null
  expected_target_parent_version_digest: string
  proposed_content_digest: string
  byte_count: number
  content: string | null
  confirmation_digest: string
}

export interface WorkspacePathOperationReceipt {
  schema_version: 'deskpilot.workspace-path-operation-receipt.v1'
  task_id: string
  operation: 'create' | 'rename'
  source_path: string | null
  target_path: string
  confirmation_digest: string
  version_digest: string
  content_digest: string
  byte_count: number
  committed_at: string
  receipt_digest: string
}

export interface TaskWorkbench {
  schema_version: 'deskpilot.task-workbench.v1'
  task: Task
  stage: WorkbenchStage
  actions: WorkbenchActionState[]
  conversation: Array<{
    message_id: string
    role: 'user' | 'assistant'
    content: string | null
    created_at: string
  }>
  route: TurnRoute | null
  turn_planning: TurnPlanningWorkbenchRead | null
  planning: Record<string, unknown> | null
  contract: Record<string, unknown> | null
  plans: { plans: Array<Record<string, unknown>> }
  executions: { runs: WorkbenchRun[] }
  replans: { replans: AgentReplan[] }
  repair_loop: AgentRepairLoopStatus | null
  research: {
    research_session_id: string
    status: string
    claims: WorkbenchClaim[]
    citations: WorkbenchCitation[]
    search_calls: Array<Record<string, unknown>>
    page_snapshots: Array<Record<string, unknown>>
  } | null
  verification: {
    verification_run_id: string
    status: string
    outcome: string
    verdicts: Array<{
      claim_id: string
      outcome: 'verified' | 'unsupported' | 'contradicted'
      reason_code: string
      citation_ids: string[]
    }>
  } | null
  workspace: {
    workspace_id: string
    status: string
    artifacts: Array<{
      artifact_id: string
      relative_path: string
      active_revision: {
        revision_id: string
        media_type: 'application/pdf' | 'text/html' | 'text/css' | 'text/markdown'
        content_digest: string
        byte_count: number
        patch_receipt_id: string
        pdf_render_verification: {
          profile_id: 'deskpilot.pdf-render.v1'
          status: 'passed'
          engine: string
          source_digest: string
          page_count: number
          page_width_points: number
          page_height_points: number
          render_dpi: number
          rendered_page_digests: string[]
          rendered_page_dimensions: Array<[number, number]>
          issue_codes: string[]
          evidence_digest: string
        } | null
      }
    }>
  } | null
  browser: {
    browser_run_id: string
    status: 'passed' | 'failed'
    engine: string
    viewport_width: number
    viewport_height: number
    title: string
    heading_count: number
    link_count: number
    external_request_count: number
    console_error_count: number
    page_error_count: number
    issue_codes: string[]
    screenshot_digest: string
  } | null
  delivery: {
    delivery_id: string
    artifact_id: string
    revision_id: string
    verified_claim_ids: string[]
    citation_ids: string[]
    limitation_codes: string[]
    manifest_digest: string
  } | null
  knowledge: KnowledgeSearchResult | null
  mcp: McpToolCallResult | null
  workspace_file: WorkspaceFileRead | null
  workspace_edit: WorkspaceEditPreview | WorkspaceEditReceipt | null
  workspace_patch: WorkspacePatchPreview | WorkspacePatchReceipt | null
  workspace_path_operation: WorkspacePathOperationPreview | WorkspacePathOperationReceipt | null
  workspace_directory: WorkspaceDirectoryRead | null
  workspace_check: WorkspaceCheckRead | null
  workspace_python_test: WorkspacePythonTestRead | null
  workspace_node_test: WorkspaceNodeTestRead | null
  exports: ArtifactExport[]
  projection_digest: string
}

export interface CreateResearchWorkbenchTask {
  goal: string
  privacy_mode: 'local_preferred' | 'balanced'
  constraints: string[]
}

export interface CreateConversationTurn {
  message: string
  privacy_mode: 'local_preferred' | 'balanced'
  constraints: string[]
}

export interface ContinueConversationTurn {
  message: string
}
