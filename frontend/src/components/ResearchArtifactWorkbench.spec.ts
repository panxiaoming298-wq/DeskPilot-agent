import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  advanceTaskWorkbench,
  commitArtifactExport,
  commitWorkspaceEdit,
  commitWorkspacePatch,
  commitWorkspacePathOperation,
  createConversationTurn,
  getTaskWorkbench,
  prepareArtifactExport,
  replanTaskWorkbench,
  setMcpServerEnabled,
} from '../api'
import type { ArtifactExport, TaskWorkbench } from '../types'
import ResearchArtifactWorkbench from './ResearchArtifactWorkbench.vue'

vi.mock('../api', () => ({
  ApiProblemError: class extends Error {},
  advanceTaskWorkbench: vi.fn(),
  cancelWorkbenchExecution: vi.fn(),
  commitArtifactExport: vi.fn(),
  commitWorkspaceEdit: vi.fn(),
  commitWorkspacePatch: vi.fn(),
  commitWorkspacePathOperation: vi.fn(),
  continueConversationTurn: vi.fn(),
  createConversationTurn: vi.fn(),
  createResearchWorkbenchTask: vi.fn(),
  getTaskWorkbench: vi.fn(),
  prepareArtifactExport: vi.fn(),
  replanTaskWorkbench: vi.fn(),
  runWorkbenchStep: vi.fn(),
  setMcpServerEnabled: vi.fn(),
  stopTaskWorkbench: vi.fn(),
}))

const preview: ArtifactExport = {
  export_id: `xpt_${'1'.repeat(64)}`, delivery_id: `dlv_${'2'.repeat(64)}`,
  task_id: `tsk_${'3'.repeat(64)}`, artifact_id: `art_${'4'.repeat(64)}`,
  revision_id: `rev_${'5'.repeat(64)}`, target_path: 'D:\\Reports\\research.html',
  conflict_policy: 'fail_if_exists', status: 'prepared', source_digest: '6'.repeat(64),
  request_digest: '7'.repeat(64), confirmation_digest: '8'.repeat(64), receipt_digest: null,
  byte_count: 1024, error_code: null, requested_at: '2026-08-18T00:00:00Z', committed_at: null,
}
const markdownArtifactId = `art_${'a'.repeat(64)}`
const pdfArtifactId = `art_${'d'.repeat(64)}`

const workbench = {
  schema_version: 'deskpilot.task-workbench.v1',
  task: { task_id: preview.task_id, conversation_id: null, goal: '生成可核验研究页', status: 'running', mode: 'standard', privacy_mode: 'balanced', constraints: [], last_event_seq: 0, event_stream: '/events', created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:00Z' },
  stage: 'delivered',
  actions: [
    { action: 'prepare_export', enabled: true, reason_code: 'AVAILABLE', explanation: '预览写入', effect_class: 'user_path_write' },
    { action: 'stop_execution', enabled: false, reason_code: 'EXECUTION_NOT_ACTIVE', explanation: '停止', effect_class: 'execution_control' },
  ],
  conversation: [{ message_id: 'msg_1', role: 'user', content: '生成可核验研究页', created_at: '2026-08-18T00:00:00Z' }],
  turn_planning: null,
  task_loop: null,
  planning: {}, contract: {}, plans: { plans: [] },
  executions: { runs: [{ run_id: `run_${'9'.repeat(64)}`, task_id: preview.task_id, status: 'succeeded', revision: 5, created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:01Z', invocations: [], model_turns: [
    { turn_id: `amt_${'1'.repeat(64)}`, turn_no: 1, status: 'succeeded', decision_kind: 'request_route', decision_digest: '2'.repeat(64), binding_id: `rbn_${'3'.repeat(64)}`, observation_digest: '4'.repeat(64) },
    { turn_id: `amt_${'5'.repeat(64)}`, turn_no: 2, status: 'succeeded', decision_kind: 'submit_result', decision_digest: '6'.repeat(64), binding_id: null, observation_digest: null },
  ], nodes: [
    { node_id: 'node-1', local_key: 'research', status: 'verified', revision: 2, attempt_count: 1, claim_owner_id: null, claim_fencing_token: 1, claim_expires_at: null, bound_agent: null, runtime_enabled: true },
    { node_id: 'node-2', local_key: 'build_html', status: 'verified', revision: 2, attempt_count: 1, claim_owner_id: null, claim_fencing_token: 1, claim_expires_at: null, bound_agent: null, runtime_enabled: true },
    { node_id: 'node-3', local_key: 'browser_verify', status: 'verified', revision: 2, attempt_count: 1, claim_owner_id: null, claim_fencing_token: 1, claim_expires_at: null, bound_agent: null, runtime_enabled: true },
    { node_id: 'node-4', local_key: 'final_acceptance', status: 'verified', revision: 2, attempt_count: 1, claim_owner_id: null, claim_fencing_token: 1, claim_expires_at: null, bound_agent: null, runtime_enabled: true },
  ] }] },
  research: { research_session_id: 'rs_1', status: 'verified', search_calls: [], page_snapshots: [], claims: [{ claim_id: 'clm_1', statement: '这是一条已核验事实。', citation_ids: ['cit_1'], status: 'awaiting_verification' }], citations: [] },
  verification: { verification_run_id: 'vrf_1', status: 'completed', outcome: 'verified', verdicts: [{ claim_id: 'clm_1', outcome: 'verified', reason_code: 'SUPPORTED', citation_ids: ['cit_1'] }] },
  workspace: { workspace_id: 'wsp_1', status: 'delivered', artifacts: [
    { artifact_id: preview.artifact_id, relative_path: 'index.html', active_revision: { revision_id: preview.revision_id, media_type: 'text/html', content_digest: preview.source_digest, byte_count: 1024, patch_receipt_id: 'ptr_1' } },
    { artifact_id: markdownArtifactId, relative_path: 'report.md', active_revision: { revision_id: `rev_${'b'.repeat(64)}`, media_type: 'text/markdown', content_digest: 'c'.repeat(64), byte_count: 512, patch_receipt_id: 'ptr_2' } },
    { artifact_id: pdfArtifactId, relative_path: 'report.pdf', active_revision: { revision_id: `rev_${'d'.repeat(64)}`, media_type: 'application/pdf', content_digest: 'e'.repeat(64), byte_count: 2048, patch_receipt_id: 'ptr_3', pdf_render_verification: { profile_id: 'deskpilot.pdf-render.v1', status: 'passed', engine: 'chromium-print+poppler-pdftoppm', source_digest: 'e'.repeat(64), page_count: 2, page_width_points: 595, page_height_points: 842, render_dpi: 144, rendered_page_digests: ['f'.repeat(64), '0'.repeat(64)], rendered_page_dimensions: [[1190, 1684], [1190, 1684]], issue_codes: [], evidence_digest: '1'.repeat(64) } } },
  ] },
  browser: { browser_run_id: 'brr_1', status: 'passed', engine: 'chromium', viewport_width: 1280, viewport_height: 720, title: '研究页', heading_count: 2, link_count: 1, external_request_count: 0, console_error_count: 0, page_error_count: 0, issue_codes: [], screenshot_digest: '0'.repeat(64) },
  delivery: { delivery_id: preview.delivery_id, artifact_id: preview.artifact_id, revision_id: preview.revision_id, verified_claim_ids: ['clm_1'], citation_ids: ['cit_1'], limitation_codes: [], manifest_digest: '1'.repeat(64) },
  exports: [], projection_digest: '2'.repeat(64),
} as unknown as TaskWorkbench

describe('ResearchArtifactWorkbench', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(createConversationTurn).mockResolvedValue(workbench)
    vi.mocked(advanceTaskWorkbench).mockResolvedValue(workbench)
    vi.mocked(getTaskWorkbench).mockResolvedValue(workbench)
    vi.mocked(setMcpServerEnabled).mockResolvedValue({} as never)
    vi.mocked(prepareArtifactExport).mockResolvedValue(preview)
    vi.mocked(commitArtifactExport).mockResolvedValue({ ...preview, status: 'committed', receipt_digest: 'a'.repeat(64), committed_at: '2026-08-18T00:00:02Z' })
    vi.mocked(commitWorkspaceEdit).mockResolvedValue(workbench)
    vi.mocked(commitWorkspacePatch).mockResolvedValue(workbench)
    vi.mocked(commitWorkspacePathOperation).mockResolvedValue(workbench)
  })

  it('shows server proof and requires a separate export confirmation', async () => {
    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('生成可核验研究页')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('这是一条已核验事实。')
    expect(wrapper.text()).toContain('外部请求')
    expect(wrapper.text()).toContain('0')
    expect(wrapper.text()).toContain('PatchReceipt')
    expect(wrapper.text()).toContain('PDF Render 2 页 · 144 DPI')
    expect(wrapper.text()).toContain('请求受控 Route')
    expect(wrapper.text()).toContain('提交候选结果')

    await wrapper.get('#export-artifact').setValue(pdfArtifactId)
    expect(wrapper.get('#export-target').attributes('placeholder')).toBe('D:\\Reports\\research.pdf')

    await wrapper.get('#export-artifact').setValue(markdownArtifactId)
    await wrapper.get('#export-target').setValue('D:\\Reports\\research.md')
    await wrapper.get('.export-form button').trigger('click')
    await flushPromises()
    expect(prepareArtifactExport).toHaveBeenCalledWith(
      preview.delivery_id,
      'D:\\Reports\\research.md',
      expect.stringMatching(/^prepare-/),
      markdownArtifactId,
    )
    expect(commitArtifactExport).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('确认写入此路径')

    await wrapper.get('.confirm-write').trigger('click')
    await flushPromises()
    expect(commitArtifactExport).toHaveBeenCalledWith(
      preview.export_id, preview.confirmation_digest, expect.stringMatching(/^commit-/),
    )
    expect(wrapper.text()).toContain('不可变导出回执')
  })

  it('observes every server-owned safe step without advancing from the browser', async () => {
    const snapshots: Array<[TaskWorkbench['stage'], TaskWorkbench['actions'][number]['action']]> = [
      ['researching', 'run_research'],
      ['awaiting_verification', 'verify_claims'],
      ['building_artifact', 'build_artifact'],
      ['verifying_browser', 'verify_browser'],
      ['ready_to_deliver', 'finalize_delivery'],
    ]
    const snapshot = (stage: TaskWorkbench['stage'], action: TaskWorkbench['actions'][number]['action']) => ({
      ...workbench,
      stage,
      projection_digest: `${stage}-${action}`.padEnd(64, '0'),
      delivery: null,
      actions: [
        { action, enabled: true, reason_code: 'AVAILABLE', explanation: '自动推进', effect_class: 'read_only' },
        { action: 'stop_execution', enabled: true, reason_code: 'AVAILABLE', explanation: '停止', effect_class: 'execution_control' },
      ],
    } as TaskWorkbench)
    vi.mocked(createConversationTurn).mockResolvedValue(snapshot(...snapshots[0]))
    for (const [stage, action] of snapshots.slice(1)) {
      vi.mocked(getTaskWorkbench).mockResolvedValueOnce(snapshot(stage, action))
    }
    vi.mocked(getTaskWorkbench).mockResolvedValueOnce(workbench)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('自动完成研究交付')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(getTaskWorkbench).toHaveBeenCalledTimes(5)
    expect(advanceTaskWorkbench).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('已完成')
    expect(wrapper.findAll('.run-card button')).toHaveLength(0)
  })

  it('keeps polling a claimed interpretation and only renders minimized planner proof', async () => {
    const unmatchedRoute = {
      schema_version: 'deskpilot.turn-route.v1' as const,
      task_id: preview.task_id,
      conversation_id: `cnv_${'1'.repeat(64)}`,
      user_message_id: `msg_${'2'.repeat(64)}`,
      decision: 'unsupported' as const,
      route_id: null,
      route_version: null,
      route_manifest_digest: null,
      turn_planning_adjudication_id: null,
      turn_plan_binding_id: null,
      turn_planning_provenance_digest: null,
      candidate_digest: '3'.repeat(64),
      parameter_digest: '4'.repeat(64),
      resolved_from_task_id: null,
      resolution_rule: null,
      resolution_digest: null,
      reason_code: 'NO_ROUTE_MATCHED',
      status: 'not_applicable' as const,
      result_digest: null,
      error_code: null,
      revision: 1,
      created_at: '2026-08-18T00:00:00Z',
      updated_at: '2026-08-18T00:00:00Z',
    }
    const planning = {
      schema_version: 'deskpilot.turn-planning-workbench-read.v1',
      run: {
        schema_version: 'deskpilot.turn-planner-run-workbench-summary.v1',
        status: 'dispatching', request_digest: '7'.repeat(64), run_digest: '8'.repeat(64),
        offer_count: 15, offer_set_digest: '6'.repeat(64), response_digest: null,
        failure: null, revision: 2,
      },
      adjudication: null,
      binding: null,
      revision: 2,
      planning_digest: '9'.repeat(64),
    }
    const interpreting = {
      ...workbench,
      stage: 'interpreting',
      route: unmatchedRoute,
      turn_planning: planning,
      actions: [
        { action: 'interpret_turn', enabled: false, reason_code: 'ACTION_CLAIMED', explanation: '后台解释中', effect_class: 'read_only' },
        { action: 'stop_execution', enabled: true, reason_code: 'AVAILABLE', explanation: '停止', effect_class: 'execution_control' },
      ],
      delivery: null,
      projection_digest: 'a'.repeat(64),
    } as unknown as TaskWorkbench
    const settled = {
      ...interpreting,
      stage: 'unsupported',
      actions: [],
      turn_planning: {
        ...planning,
        run: {
          ...planning.run,
          status: 'succeeded',
          response_digest: 'b'.repeat(64),
          run_digest: 'c'.repeat(64),
          revision: 3,
        },
        adjudication: {
          schema_version: 'deskpilot.turn-planner-adjudication-workbench-summary.v1',
          outcome: 'unsupported', reason_code: 'PLANNER_UNSUPPORTED',
          selected_offer_count: 0,
          adjudication_digest: 'd'.repeat(64),
        },
        binding: {
          schema_version: 'deskpilot.turn-plan-binding-workbench-summary.v1',
          status: 'not_applicable', reason_code: 'PLANNER_UNSUPPORTED',
          binding_digest: 'e'.repeat(64),
        },
        revision: 3,
        planning_digest: 'f'.repeat(64),
      },
      projection_digest: '0'.repeat(64),
    } as unknown as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(interpreting)
    vi.mocked(getTaskWorkbench).mockResolvedValueOnce(settled)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('替我处理这份材料')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(getTaskWorkbench).toHaveBeenCalledOnce()
    expect(advanceTaskWorkbench).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Turn Planner Proof')
    expect(wrapper.text()).toContain('服务器裁决')
    expect(wrapper.text()).toContain('PLANNER_UNSUPPORTED')
    expect(wrapper.text()).toContain('不会自动重放')
    expect(JSON.stringify(settled.turn_planning)).not.toContain('response_manifest')
    expect(JSON.stringify(settled.turn_planning)).not.toContain('offers')
  })

  it('shows a sanitized planned task-loop proof without private inputs', async () => {
    const planned = {
      ...workbench,
      stage: 'planned',
      actions: [],
      executions: { runs: [] },
      delivery: null,
      task_loop: {
        schema_version: 'deskpilot.task-loop-workbench.v1',
        loop_id: `tlp_${'1'.repeat(64)}`,
        phase: 'plan', status: 'planned', revision: 2, event_count: 2, step_count: 2,
        source_turn_plan_binding_digest: '2'.repeat(64),
        draft_record_digest: '3'.repeat(64),
        expected_plan_manifest_digest: '4'.repeat(64),
        progress_digest: '5'.repeat(64), failure: null, recoverable: false,
        updated_at: '2026-08-18T00:00:02Z', projection_digest: '6'.repeat(64),
      },
      projection_digest: '7'.repeat(64),
    } as unknown as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(planned)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('先查询 alpha 再统计 alpha')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Task Loop Proof')
    expect(wrapper.text()).toContain('2 步 model_planner Draft')
    expect(wrapper.text()).toContain('不会再次调用模型')
    expect(JSON.stringify(planned.task_loop)).not.toContain('parameters')
    expect(JSON.stringify(planned.task_loop)).not.toContain('offer')
    expect(advanceTaskWorkbench).not.toHaveBeenCalled()
  })

  it('shows restart-safe task-loop execution progress without authority inputs', async () => {
    const executing = {
      ...workbench,
      stage: 'executing',
      actions: [],
      task_loop: {
        schema_version: 'deskpilot.task-loop-execution-workbench.v1',
        task_id: preview.task_id,
        phase: 'execute', loop_status: 'planned', execution_status: 'active',
        loop_revision: 2, loop_event_count: 2,
        execution_revision: 1, execution_event_count: 1,
        node_count: 4, pending_count: 3, ready_count: 1, active_count: 0,
        awaiting_verification_count: 0, verified_count: 0, waiting_user_count: 0,
        failed_count: 0, cancelled_count: 0, candidate_count: 0,
        verified_result_count: 0, nodes: [], recoverable: true,
        created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:02Z',
        projection_digest: '8'.repeat(64),
      },
      projection_digest: '9'.repeat(64),
    } as unknown as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(executing)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('先查询 alpha 再统计 alpha')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Task Loop Proof')
    expect(wrapper.text()).toContain('通用循环正在按一个持久命令逐步推进')
    expect(wrapper.text()).toContain('0 / 4 已验证')
    expect(wrapper.text()).toContain('可从持久证明恢复')
    expect(JSON.stringify(executing.task_loop)).not.toContain('parameters')
    expect(JSON.stringify(executing.task_loop)).not.toContain('offer')
  })

  it('shows clarification without inventing an executable run', async () => {
    const clarification = {
      ...workbench,
      stage: 'needs_clarification',
      route: {
        schema_version: 'deskpilot.turn-route.v1', task_id: preview.task_id,
        conversation_id: `cnv_${'1'.repeat(64)}`, user_message_id: `msg_${'2'.repeat(64)}`,
        decision: 'needs_clarification', route_id: null, route_version: null,
        route_manifest_digest: null, turn_planning_adjudication_id: null,
        turn_plan_binding_id: null, turn_planning_provenance_digest: null,
        candidate_digest: '3'.repeat(64), parameter_digest: '4'.repeat(64),
        resolved_from_task_id: null, resolution_rule: null, resolution_digest: null,
        reason_code: 'AMBIGUOUS_GOAL', status: 'not_applicable', result_digest: null,
        error_code: null, revision: 1, created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:00Z',
      },
      actions: [], executions: { runs: [] }, research: null, verification: null,
      workspace: null, browser: null, delivery: null, knowledge: null, mcp: null,
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(clarification)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('帮我处理一下')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(advanceTaskWorkbench).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('需要澄清')
    expect(wrapper.text()).toContain('等待你补充')
    expect(wrapper.find('.run-card ol').exists()).toBe(false)
  })

  it('requires explicit MCP enablement before continuing the routed turn', async () => {
    const route = {
      schema_version: 'deskpilot.turn-route.v1' as const, task_id: preview.task_id,
      conversation_id: `cnv_${'1'.repeat(64)}`, user_message_id: `msg_${'2'.repeat(64)}`,
      decision: 'routed' as const, route_id: 'mcp_text_metrics' as const, route_version: '1' as const,
      route_manifest_digest: '3'.repeat(64), turn_planning_adjudication_id: null,
      turn_plan_binding_id: null, turn_planning_provenance_digest: null,
      candidate_digest: '4'.repeat(64), parameter_digest: '5'.repeat(64),
      resolved_from_task_id: null, resolution_rule: null, resolution_digest: null,
      reason_code: 'ROUTE_MATCHED', status: 'needs_user_action' as const, result_digest: null,
      error_code: null, revision: 1, created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:00Z',
    }
    const waiting = {
      ...workbench, stage: 'needs_user_action', route, delivery: null, research: null,
      verification: null, workspace: null, browser: null, knowledge: null, mcp: null,
      actions: [{ action: 'execute_route', enabled: false, reason_code: 'MCP_SERVER_DISABLED', explanation: '等待启用', effect_class: 'read_only' }],
    } as TaskWorkbench
    const ready = {
      ...waiting, stage: 'executing', route: { ...route, status: 'ready' },
      actions: [{ action: 'execute_route', enabled: true, reason_code: 'AVAILABLE', explanation: '执行只读工具', effect_class: 'read_only' }],
    } as TaskWorkbench
    const done = {
      ...ready, stage: 'delivered', route: { ...route, status: 'succeeded', result_digest: '6'.repeat(64) },
      actions: [], mcp: {
        server_id: 'deskpilot.readonly-text', tool_name: 'text_metrics', protocol_version: '2025-06-18',
        structured_content: { character_count: 15, line_count: 1 }, request_digest: '7'.repeat(64),
        result_digest: '6'.repeat(64), audit_event_id: `mcp_${'8'.repeat(64)}`,
      },
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(waiting)
    vi.mocked(getTaskWorkbench).mockResolvedValue(ready)
    vi.mocked(advanceTaskWorkbench).mockResolvedValue(done)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('统计字符数：DeskPilot Agent')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()
    expect(advanceTaskWorkbench).not.toHaveBeenCalled()

    await wrapper.get('.route-action button').trigger('click')
    await flushPromises()
    expect(setMcpServerEnabled).toHaveBeenCalledWith('deskpilot.readonly-text', true)
    expect(advanceTaskWorkbench).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('character_count')
    expect(wrapper.text()).toContain('15')
  })

  it('keeps a workspace replacement in-stream until the bound preview is confirmed', async () => {
    const confirmation = 'c'.repeat(64)
    const route = {
      schema_version: 'deskpilot.turn-route.v1' as const, task_id: preview.task_id,
      conversation_id: `cnv_${'1'.repeat(64)}`, user_message_id: `msg_${'2'.repeat(64)}`,
      decision: 'routed' as const, route_id: 'workspace_file_replace' as const,
      route_version: '1' as const, route_manifest_digest: '3'.repeat(64),
      turn_planning_adjudication_id: null, turn_plan_binding_id: null,
      turn_planning_provenance_digest: null,
      candidate_digest: '4'.repeat(64), parameter_digest: '5'.repeat(64),
      resolved_from_task_id: null, resolution_rule: null, resolution_digest: null,
      reason_code: 'WORKSPACE_FILE_REPLACE_MATCHED', status: 'needs_user_action' as const,
      result_digest: confirmation, error_code: null, revision: 2,
      created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:00Z',
    }
    const editPreview = {
      schema_version: 'deskpilot.workspace-edit-preview.v1' as const,
      task_id: preview.task_id, relative_path: 'README.md',
      expected_version_digest: '6'.repeat(64), proposed_content_digest: '7'.repeat(64),
      replacement_count: 1 as const, byte_count: 19, old_text: 'old', new_text: 'new',
      confirmation_digest: confirmation,
    }
    const waiting = {
      ...workbench, stage: 'needs_user_action', route, delivery: null, research: null,
      verification: null, workspace: null, browser: null, knowledge: null, mcp: null,
      workspace_file: null, workspace_edit: editPreview,
      actions: [
        { action: 'commit_workspace_edit', enabled: true, reason_code: 'AVAILABLE', explanation: '确认替换', effect_class: 'user_path_write' },
        { action: 'stop_execution', enabled: true, reason_code: 'AVAILABLE', explanation: '停止', effect_class: 'execution_control' },
      ],
    } as TaskWorkbench
    const receipt = {
      schema_version: 'deskpilot.workspace-edit-receipt.v1' as const,
      task_id: preview.task_id, relative_path: 'README.md', confirmation_digest: confirmation,
      previous_version_digest: '6'.repeat(64), version_digest: '8'.repeat(64),
      content_digest: '9'.repeat(64), backup_relative_path: '.README.md.backup',
      byte_count: 19, committed_at: '2026-08-18T00:00:02Z', receipt_digest: 'a'.repeat(64),
    }
    const done = {
      ...waiting, stage: 'delivered', route: { ...route, status: 'succeeded', result_digest: receipt.receipt_digest },
      actions: [], workspace_edit: receipt,
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(waiting)
    vi.mocked(commitWorkspaceEdit).mockResolvedValue(done)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('在工作区文件 README.md 中把 "old" 替换为 "new"')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(advanceTaskWorkbench).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('替换前')
    expect(wrapper.text()).toContain('old')
    expect(wrapper.text()).toContain('确认替换并保留备份')

    await wrapper.get('.workspace-approval button').trigger('click')
    await flushPromises()
    expect(commitWorkspaceEdit).toHaveBeenCalledWith(preview.task_id, confirmation)
    expect(wrapper.text()).toContain('提交完成')
    expect(wrapper.text()).toContain('.README.md.backup')
  })

  it('shows an Agent proposal as unprivileged until patch-and-test confirmation', async () => {
    const confirmation = 'c'.repeat(64)
    const route = {
      schema_version: 'deskpilot.turn-route.v1' as const, task_id: preview.task_id,
      conversation_id: `cnv_${'1'.repeat(64)}`, user_message_id: `msg_${'2'.repeat(64)}`,
      decision: 'routed' as const, route_id: 'workspace_agent_patch_test' as const,
      route_version: '1' as const, route_manifest_digest: '3'.repeat(64),
      turn_planning_adjudication_id: null, turn_plan_binding_id: null,
      turn_planning_provenance_digest: null,
      candidate_digest: '4'.repeat(64), parameter_digest: '5'.repeat(64),
      resolved_from_task_id: null, resolution_rule: null, resolution_digest: null,
      reason_code: 'WORKSPACE_AGENT_PATCH_TEST_MATCHED', status: 'needs_user_action' as const,
      result_digest: confirmation, error_code: null, revision: 2,
      created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:00Z',
    }
    const patchPreview = {
      schema_version: 'deskpilot.workspace-patch-preview.v1' as const,
      task_id: preview.task_id,
      changes: [{
        index: 1, relative_path: 'backend/sample.py', old_text: 'VALUE = 1',
        new_text: 'VALUE = 2', expected_version_digest: '6'.repeat(64),
        original_content_digest: '7'.repeat(64), proposed_content_digest: '8'.repeat(64),
        byte_count: 10, change_digest: '9'.repeat(64),
      }],
      staging_workspace_ref: 'workspace-patches/agent-proof',
      manifest_digest: 'a'.repeat(64), total_byte_count: 10,
      confirmation_digest: confirmation,
    }
    const waiting = {
      ...workbench, stage: 'needs_user_action', route, delivery: null, research: null,
      verification: null, workspace: null, browser: null, knowledge: null, mcp: null,
      workspace_file: null, workspace_edit: null, workspace_patch: patchPreview,
      actions: [
        { action: 'commit_workspace_patch', enabled: true, reason_code: 'AVAILABLE', explanation: '确认补丁并测试', effect_class: 'user_path_write' },
        { action: 'stop_execution', enabled: true, reason_code: 'AVAILABLE', explanation: '停止', effect_class: 'execution_control' },
      ],
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(waiting)
    vi.mocked(commitWorkspacePatch).mockResolvedValue(waiting)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('修复并测试工作区：…')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Agent 补丁 → 固定测试')
    expect(wrapper.text()).toContain('提交 Agent 的单文件建议并运行固定测试')
    expect(wrapper.text()).toContain('建议本身没有写权限')
    expect(wrapper.text()).toContain('VALUE = 2')
    await wrapper.get('.workspace-patch-approval .approval-actions button:last-child').trigger('click')
    await flushPromises()
    expect(commitWorkspacePatch).toHaveBeenCalledWith(preview.task_id, confirmation)
  })

  it('shows every staged file diff and commits a patch with one confirmation', async () => {
    const confirmation = 'd'.repeat(64)
    const route = {
      schema_version: 'deskpilot.turn-route.v1' as const, task_id: preview.task_id,
      conversation_id: `cnv_${'1'.repeat(64)}`, user_message_id: `msg_${'2'.repeat(64)}`,
      decision: 'routed' as const, route_id: 'workspace_patch_bundle' as const,
      route_version: '1' as const, route_manifest_digest: '3'.repeat(64),
      turn_planning_adjudication_id: null, turn_plan_binding_id: null,
      turn_planning_provenance_digest: null,
      candidate_digest: '4'.repeat(64), parameter_digest: '5'.repeat(64),
      resolved_from_task_id: null, resolution_rule: null, resolution_digest: null,
      reason_code: 'WORKSPACE_PATCH_BUNDLE_MATCHED', status: 'needs_user_action' as const,
      result_digest: confirmation, error_code: null, revision: 2,
      created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:00Z',
    }
    const changes = [
      { index: 1, relative_path: 'a.md', old_text: 'old-a', new_text: 'new-a' },
      { index: 2, relative_path: 'b.py', old_text: 'old-b', new_text: 'new-b' },
    ].map((item) => ({
      ...item, expected_version_digest: '6'.repeat(64), original_content_digest: '7'.repeat(64),
      proposed_content_digest: '8'.repeat(64), byte_count: 12,
      change_digest: String(item.index).repeat(64),
    }))
    const patchPreview = {
      schema_version: 'deskpilot.workspace-patch-preview.v1' as const,
      task_id: preview.task_id, changes, staging_workspace_ref: 'workspace-patches/proof',
      manifest_digest: '9'.repeat(64), total_byte_count: 24,
      confirmation_digest: confirmation,
    }
    const waiting = {
      ...workbench, stage: 'needs_user_action', route, delivery: null, research: null,
      verification: null, workspace: null, browser: null, knowledge: null, mcp: null,
      workspace_file: null, workspace_edit: null, workspace_patch: patchPreview,
      actions: [
        { action: 'commit_workspace_patch', enabled: true, reason_code: 'AVAILABLE', explanation: '确认补丁', effect_class: 'user_path_write' },
        { action: 'stop_execution', enabled: true, reason_code: 'AVAILABLE', explanation: '停止', effect_class: 'execution_control' },
      ],
    } as TaskWorkbench
    const changeReceipts = changes.map((item) => ({
      schema_version: 'deskpilot.workspace-edit-receipt.v1' as const,
      task_id: preview.task_id, relative_path: item.relative_path,
      confirmation_digest: confirmation, previous_version_digest: '6'.repeat(64),
      version_digest: 'a'.repeat(64), content_digest: 'b'.repeat(64),
      backup_relative_path: `.${item.relative_path}.backup`, byte_count: item.byte_count,
      committed_at: '2026-08-18T00:00:02Z', receipt_digest: item.change_digest,
    }))
    const done = {
      ...waiting, stage: 'delivered', route: { ...route, status: 'succeeded', result_digest: 'e'.repeat(64) },
      actions: [], workspace_patch: {
        schema_version: 'deskpilot.workspace-patch-receipt.v1', task_id: preview.task_id,
        status: 'committed', confirmation_digest: confirmation, change_receipts: changeReceipts,
        failed_path: null, error_code: null, committed_at: '2026-08-18T00:00:02Z',
        receipt_digest: 'e'.repeat(64),
      },
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(waiting)
    vi.mocked(commitWorkspacePatch).mockResolvedValue(done)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('批量修改工作区文件：…')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('a.md')
    expect(wrapper.text()).toContain('old-a')
    expect(wrapper.text()).toContain('b.py')
    expect(wrapper.text()).toContain('改一下')
    expect(wrapper.text()).toContain('拒绝')

    await wrapper.get('.workspace-patch-approval .approval-actions button:last-child').trigger('click')
    await flushPromises()
    expect(commitWorkspacePatch).toHaveBeenCalledWith(preview.task_id, confirmation)
    expect(wrapper.text()).toContain('已写入 2 个文件')
    expect(wrapper.text()).toContain('.a.md.backup')

    wrapper.unmount()
    const partial = {
      ...waiting, stage: 'blocked', route: {
        ...route, status: 'failed', result_digest: 'f'.repeat(64),
        error_code: 'WORKSPACE_PATCH_PARTIAL',
      },
      actions: [], workspace_patch: {
        schema_version: 'deskpilot.workspace-patch-receipt.v1', task_id: preview.task_id,
        status: 'partial', confirmation_digest: confirmation,
        change_receipts: changeReceipts.slice(0, 1), failed_path: 'b.py',
        error_code: 'WORKSPACE_FILE_CONFLICT', committed_at: '2026-08-18T00:00:02Z',
        receipt_digest: 'f'.repeat(64),
      },
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(partial)
    const partialWrapper = mount(ResearchArtifactWorkbench)
    await partialWrapper.get('#agent-prompt').setValue('批量修改工作区文件：…')
    await partialWrapper.get('.agent-composer').trigger('submit')
    await flushPromises()
    expect(partialWrapper.text()).toContain('部分完成')
    expect(partialWrapper.text()).toContain('b.py')
    expect(partialWrapper.text()).toContain('.a.md.backup')
  })

  it('keeps a non-overwriting create preview in the conversation until confirmation', async () => {
    const confirmation = 'f'.repeat(64)
    const route = {
      schema_version: 'deskpilot.turn-route.v1' as const, task_id: preview.task_id,
      conversation_id: `cnv_${'1'.repeat(64)}`, user_message_id: `msg_${'2'.repeat(64)}`,
      decision: 'routed' as const, route_id: 'workspace_file_create' as const,
      route_version: '1' as const, route_manifest_digest: '3'.repeat(64),
      turn_planning_adjudication_id: null, turn_plan_binding_id: null,
      turn_planning_provenance_digest: null,
      candidate_digest: '4'.repeat(64), parameter_digest: '5'.repeat(64),
      resolved_from_task_id: null, resolution_rule: null, resolution_digest: null,
      reason_code: 'WORKSPACE_FILE_CREATE_MATCHED', status: 'needs_user_action' as const,
      result_digest: confirmation, error_code: null, revision: 2,
      created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:00Z',
    }
    const createPreview = {
      schema_version: 'deskpilot.workspace-path-operation-preview.v1' as const,
      task_id: preview.task_id, operation: 'create' as const, source_path: null,
      target_path: 'notes/todo.md', expected_source_version_digest: null,
      expected_target_parent_version_digest: '6'.repeat(64),
      proposed_content_digest: '7'.repeat(64), byte_count: 12,
      content: 'first\nsecond', confirmation_digest: confirmation,
    }
    const waiting = {
      ...workbench, stage: 'needs_user_action', route, delivery: null, research: null,
      verification: null, workspace: null, browser: null, knowledge: null, mcp: null,
      workspace_file: null, workspace_edit: null, workspace_patch: null,
      workspace_path_operation: createPreview,
      actions: [
        { action: 'commit_workspace_path_operation', enabled: true, reason_code: 'AVAILABLE', explanation: '确认创建', effect_class: 'user_path_write' },
        { action: 'stop_execution', enabled: true, reason_code: 'AVAILABLE', explanation: '停止', effect_class: 'execution_control' },
      ],
    } as TaskWorkbench
    const receipt = {
      schema_version: 'deskpilot.workspace-path-operation-receipt.v1' as const,
      task_id: preview.task_id, operation: 'create' as const, source_path: null,
      target_path: 'notes/todo.md', confirmation_digest: confirmation,
      version_digest: '8'.repeat(64), content_digest: '7'.repeat(64), byte_count: 12,
      committed_at: '2026-08-18T00:00:02Z', receipt_digest: '9'.repeat(64),
    }
    const done = {
      ...waiting, stage: 'delivered', route: {
        ...route, status: 'succeeded', result_digest: receipt.receipt_digest,
      },
      actions: [], workspace_path_operation: receipt,
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(waiting)
    vi.mocked(commitWorkspacePathOperation).mockResolvedValue(done)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('新建工作区文件："notes/todo.md" 内容："first\nsecond"')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(advanceTaskWorkbench).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('notes/todo.md（当前不存在）')
    expect(wrapper.text()).toContain('不建目录、不覆盖同名文件')
    expect(wrapper.text()).toContain('改一下')

    await wrapper.get('.workspace-path-operation-approval .approval-actions button:last-child').trigger('click')
    await flushPromises()
    expect(commitWorkspacePathOperation).toHaveBeenCalledWith(preview.task_id, confirmation)
    expect(wrapper.text()).toContain('文件已创建')
    expect(wrapper.text()).toContain('提交回执')
  })

  it('renders a dynamic Patch approval node as a fresh user-scoped grant', async () => {
    const confirmation = 'a'.repeat(64)
    const patchPreview = {
      schema_version: 'deskpilot.workspace-patch-preview.v1' as const,
      task_id: preview.task_id,
      changes: [{
        index: 0, relative_path: 'backend/sample.py',
        expected_version_digest: '1'.repeat(64), original_content_digest: '2'.repeat(64),
        proposed_content_digest: '3'.repeat(64), byte_count: 20,
        old_text: 'VALUE = 1', new_text: 'VALUE = 2', change_digest: '4'.repeat(64),
      }],
      staging_workspace_ref: '.deskpilot-workspace-patches/staged',
      manifest_digest: '5'.repeat(64), total_byte_count: 20,
      confirmation_digest: confirmation,
    }
    const baseRun = workbench.executions.runs[0]!
    const dynamic = {
      ...workbench,
      stage: 'needs_user_action',
      route: {
        schema_version: 'deskpilot.turn-route.v1', task_id: preview.task_id,
        conversation_id: `cnv_${'1'.repeat(64)}`, user_message_id: `msg_${'2'.repeat(64)}`,
        decision: 'routed', route_id: 'workspace_dynamic_patch_test', route_version: '1',
        route_manifest_digest: '6'.repeat(64), turn_planning_adjudication_id: null,
        turn_plan_binding_id: null, turn_planning_provenance_digest: null,
        candidate_digest: '7'.repeat(64),
        parameter_digest: '8'.repeat(64), resolved_from_task_id: null,
        resolution_rule: null, resolution_digest: null,
        reason_code: 'WORKSPACE_DYNAMIC_PATCH_TEST_MATCHED', status: 'needs_user_action',
        result_digest: confirmation, error_code: null, revision: 3,
        created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:01Z',
      },
      actions: [{
        action: 'commit_workspace_patch', enabled: true, reason_code: 'AVAILABLE',
        explanation: '批准当前节点', effect_class: 'user_path_write',
      }],
      workspace_patch: patchPreview,
      executions: { runs: [{
        ...baseRun, status: 'paused',
        task_graphs: [{
          graph_id: `atg_${'1'.repeat(64)}`, binding_id: `tgb_${'2'.repeat(64)}`,
          parent_invocation_id: `inv_${'3'.repeat(64)}`,
          parent_node_id: `pnd_${'4'.repeat(64)}`, decision_id: `agd_${'5'.repeat(64)}`,
          status: 'running', node_count: 1, max_depth: 1,
          graph_digest: '6'.repeat(64), output_local_key: 'patch_approval',
          output_node_id: `pnd_${'7'.repeat(64)}`, observation_id: null,
          nodes: [{
            local_key: 'patch_approval', node_id: `pnd_${'7'.repeat(64)}`,
            binding_id: `hbn_${'8'.repeat(64)}`, status: 'waiting_child', depends_on: [],
            target_agent: { agent_id: 'builtin.workspace_patch_planner', version: '1.0.0' },
            capability: { capability_id: 'workspace.patch.propose.v1', version: '1.0.0', digest: '9'.repeat(64) },
            capability_input: {
              schema_version: 'deskpilot.agent-task-graph-capability-input.v3',
              source_key: 'route_patch_test_spec', source_ref: 'turn-route://task/patch',
              read_kind: 'patch_test', path: 'backend', test_path: 'tests/test_sample.py',
              target_path: 'backend/sample.py', test_kind: 'python',
              objective: '修复测试', route_parameter_digest: 'a'.repeat(64),
              input_digest: 'b'.repeat(64),
            },
            import_sources: [], imported_result_refs: [],
            budget_allocation: { model_calls: 2, tool_calls: 1, input_tokens: 24000,
              output_tokens: 3000, wall_seconds: 90, retries: 0,
              cost_micros: 100000, handoffs: 0 },
            child_invocation_id: `inv_${'9'.repeat(64)}`, child_result_id: null,
            result_ref: null, test_result: null, approval: patchPreview, patch_result: null,
          }],
          created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:01Z',
        }],
      }] },
    } as unknown as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(dynamic)
    vi.mocked(commitWorkspacePatch).mockResolvedValue(dynamic)

    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue('多 Agent 修复并测试工作区：…')
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Patch/Approval')
    expect(wrapper.text()).toContain('PATCH TARGET backend/sample.py')
    expect(wrapper.text()).toContain('APPROVAL WAITING USER')
    expect(wrapper.text()).toContain('ONE PATCH ONLY')
    await wrapper.get('.workspace-patch-approval .approval-actions button:last-child').trigger('click')
    await flushPromises()
    expect(commitWorkspacePatch).toHaveBeenCalledWith(preview.task_id, confirmation)

    wrapper.unmount()
    const decisionDigest = 'd'.repeat(64)
    const dynamicRun = dynamic.executions.runs[0]!
    const dynamicGraph = dynamicRun.task_graphs![0]!
    const blocked = {
      ...dynamic,
      stage: 'blocked',
      route: {
        ...dynamic.route, status: 'failed', result_digest: decisionDigest,
        error_code: 'AGENT_GRAPH_TEST_CONDITION_NOT_MET', revision: 5,
      },
      actions: [{
        action: 'replan_failed_execution', enabled: true, reason_code: 'AVAILABLE',
        explanation: '生成新的修复计划代', effect_class: 'execution_control',
      }],
      repair_loop: {
        schema_version: 'deskpilot.agent-repair-loop-status.v1', task_id: preview.task_id,
        current_plan_generation: 1, maximum_plan_generations: 3, remaining_replans: 2,
        budget_limit: { model_calls: 30, tool_calls: 12, input_tokens: 276000,
          output_tokens: 30000, wall_seconds: 1350, retries: 0,
          cost_micros: 1500000, handoffs: 12 },
        budget_allocated: { model_calls: 8, tool_calls: 3, input_tokens: 76000,
          output_tokens: 9000, wall_seconds: 360, retries: 0,
          cost_micros: 400000, handoffs: 4 },
        budget_remaining: { model_calls: 22, tool_calls: 9, input_tokens: 200000,
          output_tokens: 21000, wall_seconds: 990, retries: 0,
          cost_micros: 1100000, handoffs: 8 },
        next_plan_allocation: { model_calls: 2, tool_calls: 0, input_tokens: 12000,
          output_tokens: 2000, wall_seconds: 90, retries: 0,
          cost_micros: 100000, handoffs: 4 },
        next_replan_available: true, reason_code: 'AVAILABLE', status_digest: 'a'.repeat(64),
      },
      workspace_patch: null,
      executions: { runs: [{
        ...dynamicRun, status: 'failed', task_graphs: [{
          ...dynamicGraph, status: 'failed', nodes: [
            ...dynamicGraph.nodes,
            {
              ...dynamicGraph.nodes[0], local_key: 'directory_output',
              node_id: `pnd_${'e'.repeat(64)}`, status: 'cancelled',
              condition_decisions: [{
                schema_version: 'deskpilot.agent-task-graph-condition-decision.v1',
                graph_id: dynamicGraph.graph_id,
                source_local_key: 'patch_approval',
                source_node_id: dynamicGraph.nodes[0]!.node_id,
                target_local_key: 'directory_output',
                target_node_id: `pnd_${'e'.repeat(64)}`,
                predicate: 'test_passed', actual_status: 'test_failed',
                result_ref_digest: 'f'.repeat(64), matched: false,
                decision_digest: decisionDigest,
              }],
            },
          ],
        }],
      }] },
    } as unknown as TaskWorkbench
    const replanned = {
      ...blocked,
      stage: 'executing',
      route: { ...blocked.route, status: 'ready', result_digest: null, error_code: null },
      actions: [],
      planning: { ...blocked.planning, active_plan_generation: 2 },
    } as unknown as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(blocked)
    vi.mocked(replanTaskWorkbench).mockResolvedValue(replanned)

    const blockedWrapper = mount(ResearchArtifactWorkbench)
    await blockedWrapper.get('#agent-prompt').setValue('测试失败后继续修复')
    await blockedWrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(blockedWrapper.text()).toContain('TEST CONDITION BLOCKED')
    expect(blockedWrapper.text()).toContain('测试未通过，是否生成第 2/3 代修复计划？')
    expect(blockedWrapper.text()).toContain('剩余 2 次')
    expect(blockedWrapper.text()).toContain('也可直接输入“继续修复”')
    expect(blockedWrapper.text()).toContain('ddddddddd…ddddd')
    expect(replanTaskWorkbench).not.toHaveBeenCalled()

    await blockedWrapper.get('.condition-replan-action button').trigger('click')
    await flushPromises()
    expect(replanTaskWorkbench).toHaveBeenCalledWith(preview.task_id)
    expect(blockedWrapper.find('.condition-replan-action').exists()).toBe(false)

    blockedWrapper.unmount()
    const capped = {
      ...blocked,
      planning: { ...blocked.planning, active_plan_generation: 3 },
      actions: [{
        action: 'replan_failed_execution', enabled: false,
        reason_code: 'FAILURE_NOT_REPLAN_ELIGIBLE_OR_LIMIT_REACHED',
        explanation: '换代上限已达到', effect_class: 'execution_control',
      }],
      repair_loop: {
        ...blocked.repair_loop,
        current_plan_generation: 3, remaining_replans: 0,
        budget_allocated: { model_calls: 24, tool_calls: 9, input_tokens: 228000,
          output_tokens: 27000, wall_seconds: 1080, retries: 0,
          cost_micros: 1200000, handoffs: 12 },
        budget_remaining: { model_calls: 6, tool_calls: 3, input_tokens: 48000,
          output_tokens: 3000, wall_seconds: 270, retries: 0,
          cost_micros: 300000, handoffs: 0 },
        next_replan_available: false,
        reason_code: 'GENERATION_LIMIT_REACHED',
        status_digest: 'b'.repeat(64),
      },
    } as unknown as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(capped)
    const cappedWrapper = mount(ResearchArtifactWorkbench)
    await cappedWrapper.get('#agent-prompt').setValue('三代均失败')
    await cappedWrapper.get('.agent-composer').trigger('submit')
    await flushPromises()
    expect(cappedWrapper.text()).toContain('TEST CONDITION BLOCKED · LIMIT REACHED')
    expect(cappedWrapper.text()).toContain('当前任务已到 Plan 3/3')
    expect(cappedWrapper.text()).toContain('发送一条完整的新任务指令')
  })

  it('shows directory entries and isolated check issues as evidence', async () => {
    const routeBase = {
      schema_version: 'deskpilot.turn-route.v1' as const, task_id: preview.task_id,
      conversation_id: `cnv_${'1'.repeat(64)}`, user_message_id: `msg_${'2'.repeat(64)}`,
      decision: 'routed' as const, route_version: '1' as const,
      route_manifest_digest: '3'.repeat(64), turn_planning_adjudication_id: null,
      turn_plan_binding_id: null, turn_planning_provenance_digest: null,
      candidate_digest: '4'.repeat(64),
      parameter_digest: '5'.repeat(64), resolved_from_task_id: preview.task_id,
      resolution_rule: 'workspace_file_path', resolution_digest: '8'.repeat(64),
      status: 'succeeded' as const,
      result_digest: '6'.repeat(64), error_code: null, revision: 2,
      created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:01Z',
    }
    const importedDirectoryRef = {
      schema_version: 'deskpilot.agent-task-graph-result-ref.v1' as const,
      graph_id: `atg_${'9'.repeat(64)}`,
      producer_local_key: 'source_directory',
      producer_node_id: `pnd_${'9'.repeat(64)}`,
      producer_invocation_id: `inv_${'9'.repeat(64)}`,
      producer_result_id: `res_${'9'.repeat(64)}`,
      capability: { capability_id: 'workspace.directory.read.v1', version: '1.0.0', digest: '9'.repeat(64) },
      result_kind: 'directory' as const,
      agent_result_digest: '1'.repeat(64),
      workspace_result_digest: '2'.repeat(64),
      result_ref_digest: '3'.repeat(64),
    }
    const importSourceKey = `replan_result_${'4'.repeat(32)}`
    const directory = {
      ...workbench, actions: [], route: {
        ...routeBase, route_id: 'workspace_directory_list' as const,
        reason_code: 'WORKSPACE_DIRECTORY_LIST_MATCHED',
      },
      workspace_directory: {
        schema_version: 'deskpilot.workspace-directory-read.v1' as const,
        relative_path: 'src', truncated: false, result_digest: '6'.repeat(64),
        entries: [{ name: 'valid.py', relative_path: 'src/valid.py', kind: 'file' as const,
          byte_count: 12, version_digest: '7'.repeat(64) }],
      },
      replans: { replans: [{
        schema_version: 'deskpilot.agent-replan.v2' as const,
        replan_id: `rpl_${'1'.repeat(64)}`, task_id: preview.task_id,
        source_run_id: `run_${'1'.repeat(64)}`, source_plan_generation: 1,
        source_plan_digest: '1'.repeat(64), target_run_id: `run_${'2'.repeat(64)}`,
        target_plan_generation: 2, target_plan_digest: '2'.repeat(64),
        contract_version: 1, contract_digest: '3'.repeat(64),
        failure_snapshot: {
          schema_version: 'deskpilot.agent-replan-failure-snapshot.v1' as const,
          task_id: preview.task_id, source_run_id: `run_${'1'.repeat(64)}`,
          source_plan_generation: 1, source_plan_digest: '1'.repeat(64),
          contract_version: 1, contract_digest: '3'.repeat(64),
          route_id: 'workspace_directory_analyze' as const,
          route_parameter_digest: '4'.repeat(64), route_revision: 3,
          stable_error_code: 'AGENT_ROUTE_BINDING_REJECTED' as const,
          failed_node_ids: [`pnd_${'8'.repeat(64)}`],
          failed_invocation_ids: [`inv_${'8'.repeat(64)}`],
          failed_model_turn_ids: [`amt_${'8'.repeat(64)}`], snapshot_digest: '5'.repeat(64),
        },
        repair_advice: {
          schema_version: 'deskpilot.agent-replan-repair-advice.v1' as const,
          failure_snapshot_digest: '5'.repeat(64),
          stable_error_code: 'AGENT_ROUTE_BINDING_REJECTED' as const,
          strategy_code: 'reuse_verified_evidence_and_rebind_route' as const,
          objective: 'Reuse verified evidence without granting capabilities.',
          granted_capability_ids: [],
          result_sources: [{
            schema_version: 'deskpilot.agent-replan-result-source.v1' as const,
            source_key: importSourceKey, source_run_id: `run_${'1'.repeat(64)}`,
            source_plan_generation: 1, source_plan_digest: '1'.repeat(64),
            source_graph_digest: '6'.repeat(64), result_ref: importedDirectoryRef,
            source_digest: '7'.repeat(64),
          }],
          advice_digest: '8'.repeat(64),
        },
        status: 'activated' as const, created_at: '2026-08-18T00:00:00Z',
        replan_digest: '9'.repeat(64),
      }] },
      executions: { runs: [{
        ...workbench.executions.runs[0],
        model_turns: [{
          turn_id: `amt_${'a'.repeat(64)}`, turn_no: 1, status: 'succeeded',
          decision_kind: 'propose_task_graph' as const, decision_digest: 'b'.repeat(64),
          binding_id: `tgb_${'c'.repeat(64)}`, observation_digest: 'd'.repeat(64),
        }],
        nodes: [
          {
            node_id: `pnd_${'1'.repeat(64)}`, local_key: 'workspace_directory_list',
            status: 'verified', revision: 5, attempt_count: 1, claim_owner_id: null,
            claim_fencing_token: 2, claim_expires_at: null,
            bound_agent: { agent_id: 'builtin.workspace_coordinator' },
            handoff_parent_node_id: null,
            budget: { model_calls: 2, tool_calls: 0, input_tokens: 12000,
              output_tokens: 1000, wall_seconds: 60, retries: 0,
              cost_micros: 100000, handoffs: 4 },
            runtime_enabled: true,
          },
          {
            node_id: `pnd_${'2'.repeat(64)}`, local_key: `dynamic_${'2'.repeat(56)}`,
            status: 'verified', revision: 4, attempt_count: 1, claim_owner_id: null,
            claim_fencing_token: 1, claim_expires_at: null,
            bound_agent: { agent_id: 'builtin.workspace_reader' },
            depends_on: [],
            handoff_parent_node_id: `pnd_${'1'.repeat(64)}`,
            budget: { model_calls: 2, tool_calls: 1, input_tokens: 20000,
              output_tokens: 2000, wall_seconds: 60, retries: 0,
              cost_micros: 100000, handoffs: 0 },
            runtime_enabled: true,
          },
        ],
        delegations: [],
        task_graphs: [{
          schema_version: 'deskpilot.agent-task-graph.v7' as const,
          graph_id: `atg_${'3'.repeat(64)}`,
          binding_id: `tgb_${'4'.repeat(64)}`,
          parent_invocation_id: `inv_${'4'.repeat(64)}`,
          parent_node_id: `pnd_${'1'.repeat(64)}`,
          decision_id: `agd_${'8'.repeat(64)}`,
          status: 'consumed' as const,
          node_count: 2,
          max_depth: 2,
          graph_digest: 'a'.repeat(64),
          output_local_key: 'directory_reader',
          output_node_id: `pnd_${'2'.repeat(64)}`,
          observation_id: `obs_${'7'.repeat(64)}`,
          nodes: [{
            local_key: 'directory_reader',
            node_id: `pnd_${'2'.repeat(64)}`,
            binding_id: `hbn_${'9'.repeat(64)}`,
            status: 'consumed' as const,
            depends_on: ['python_test'],
            target_agent: { agent_id: 'builtin.workspace_reader', version: '1.2.0' },
            capability: { capability_id: 'workspace.directory.read.v1', version: '1.0.0', digest: 'b'.repeat(64) },
            capability_input: {
              schema_version: 'deskpilot.agent-task-graph-capability-input.v1' as const,
              source_key: 'route_directory_path' as const,
              source_ref: 'turn-route://task/parameters/path',
              read_kind: 'directory' as const,
              path: 'src',
              test_path: null,
              target_path: null,
              test_kind: null,
              objective: null,
              route_parameter_digest: 'f'.repeat(64),
              input_digest: '0'.repeat(64),
            },
            conditions: [{
              schema_version: 'deskpilot.agent-task-graph-condition.v1' as const,
              source_local_key: 'python_test',
              source_node_id: `pnd_${'3'.repeat(64)}`,
              predicate: 'test_passed' as const,
              condition_digest: 'a'.repeat(64),
            }],
            condition_decisions: [{
              schema_version: 'deskpilot.agent-task-graph-condition-decision.v1' as const,
              graph_id: `atg_${'3'.repeat(64)}`,
              source_local_key: 'python_test',
              source_node_id: `pnd_${'3'.repeat(64)}`,
              target_local_key: 'directory_reader',
              target_node_id: `pnd_${'2'.repeat(64)}`,
              predicate: 'test_passed' as const,
              actual_status: 'passed' as const,
              result_ref_digest: '6'.repeat(64),
              matched: true,
              decision_digest: 'b'.repeat(64),
            }],
            import_sources: [importSourceKey],
            imported_result_refs: [importedDirectoryRef],
            budget_allocation: { model_calls: 2, tool_calls: 1, input_tokens: 20000,
              output_tokens: 2000, wall_seconds: 90, retries: 0,
              cost_micros: 100000, handoffs: 0 },
            child_invocation_id: `inv_${'5'.repeat(64)}`,
            child_result_id: `res_${'6'.repeat(64)}`,
            result_ref: {
              schema_version: 'deskpilot.agent-task-graph-result-ref.v1' as const,
              graph_id: `atg_${'3'.repeat(64)}`,
              producer_local_key: 'directory_reader',
              producer_node_id: `pnd_${'2'.repeat(64)}`,
              producer_invocation_id: `inv_${'5'.repeat(64)}`,
              producer_result_id: `res_${'6'.repeat(64)}`,
              capability: { capability_id: 'workspace.directory.read.v1', version: '1.0.0', digest: 'b'.repeat(64) },
              result_kind: 'directory' as const,
              agent_result_digest: 'c'.repeat(64),
              workspace_result_digest: 'd'.repeat(64),
              result_ref_digest: 'e'.repeat(64),
            },
            test_result: null,
            approval: null,
            patch_result: null,
          }, {
            local_key: 'python_test',
            node_id: `pnd_${'3'.repeat(64)}`,
            binding_id: `hbn_${'8'.repeat(64)}`,
            status: 'consumed' as const,
            depends_on: [],
            target_agent: { agent_id: 'builtin.workspace_tester', version: '1.0.0' },
            capability: { capability_id: 'workspace.python.test.v1', version: '1.0.0', digest: '1'.repeat(64) },
            capability_input: {
              schema_version: 'deskpilot.agent-task-graph-capability-input.v2' as const,
              source_key: 'route_python_test_spec' as const,
              source_ref: 'turn-route://task/parameters/python_project_path+python_test_path',
              read_kind: 'python_test' as const,
              path: 'backend',
              test_path: 'tests/test_sample.py',
              target_path: null,
              test_kind: null,
              objective: null,
              route_parameter_digest: '2'.repeat(64),
              input_digest: '3'.repeat(64),
            },
            budget_allocation: { model_calls: 2, tool_calls: 1, input_tokens: 20000,
              output_tokens: 2000, wall_seconds: 90, retries: 0,
              cost_micros: 100000, handoffs: 0 },
            child_invocation_id: `inv_${'3'.repeat(64)}`,
            child_result_id: `res_${'3'.repeat(64)}`,
            result_ref: {
              schema_version: 'deskpilot.agent-task-graph-result-ref.v1' as const,
              graph_id: `atg_${'3'.repeat(64)}`,
              producer_local_key: 'python_test',
              producer_node_id: `pnd_${'3'.repeat(64)}`,
              producer_invocation_id: `inv_${'3'.repeat(64)}`,
              producer_result_id: `res_${'3'.repeat(64)}`,
              capability: { capability_id: 'workspace.python.test.v1', version: '1.0.0', digest: '1'.repeat(64) },
              result_kind: 'python_test' as const,
              agent_result_digest: '4'.repeat(64),
              workspace_result_digest: '5'.repeat(64),
              result_ref_digest: '6'.repeat(64),
            },
            test_result: {
              schema_version: 'deskpilot.workspace-python-test.v1' as const,
              profile: 'pytest-file' as const,
              project_path: 'backend', test_path: 'tests/test_sample.py',
              snapshot_digest: '7'.repeat(64), runtime_digest: '8'.repeat(64),
              status: 'passed' as const, exit_code: 0, passed_count: 3,
              failed_count: 0, skipped_count: 0, error_count: 0, duration_ms: 120,
              output: '3 passed', output_truncated: false,
              isolation_mode: 'windows_appcontainer' as const,
              network_access: false as const, process_limit: 1 as const,
              result_digest: '9'.repeat(64),
            },
            approval: null,
            patch_result: null,
          }],
          created_at: '2026-08-18T00:00:00Z',
          updated_at: '2026-08-18T00:00:01Z',
        }],
      }] },
      workspace_check: null,
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(directory)
    const directoryWrapper = mount(ResearchArtifactWorkbench)
    await directoryWrapper.get('#agent-prompt').setValue('列出工作区目录：src')
    await directoryWrapper.get('.agent-composer').trigger('submit')
    await flushPromises()
    expect(directoryWrapper.text()).toContain('src/valid.py')
    expect(directoryWrapper.text()).toContain('完整')
    expect(directoryWrapper.text()).toContain('对话补全证明')
    expect(directoryWrapper.text()).toContain('SERVER-ADJUDICATED')
    expect(directoryWrapper.text()).toContain('builtin.workspace_coordinator')
    expect(directoryWrapper.text()).toContain('builtin.workspace_reader')
    expect(directoryWrapper.text()).toContain('DYNAMIC DAG · SERVER-ADJUDICATED')
    expect(directoryWrapper.text()).toContain('GRAPH consumed · NODES 2 · DEPTH 2')
    expect(directoryWrapper.text()).toContain('SERVER-CONDITIONAL')
    expect(directoryWrapper.text()).toContain('CONDITION python_test.TEST_PASSED · PASSED')
    expect(directoryWrapper.text()).toContain('DEPENDS ROOT')
    expect(directoryWrapper.text()).toContain('INPUT route_directory_path · src')
    expect(directoryWrapper.text()).toContain('REUSED VERIFIED replan_result_')
    expect(directoryWrapper.text()).toContain('REPAIR reuse_verified_evidence_and_rebind_route')
    expect(directoryWrapper.text()).toContain('IMPORTS 1 · GRANTS 0')
    expect(directoryWrapper.text()).toContain('builtin.workspace_tester')
    expect(directoryWrapper.text()).toContain('tests/test_sample.py')
    expect(directoryWrapper.text()).toContain('FIXED TEST passed · 3 passed / 0 failed')
    directoryWrapper.unmount()

    const checked = {
      ...workbench, actions: [], route: {
        ...routeBase, route_id: 'workspace_snapshot_check' as const,
        reason_code: 'WORKSPACE_SNAPSHOT_CHECK_MATCHED',
      },
      workspace_directory: null,
      workspace_check: {
        schema_version: 'deskpilot.workspace-check.v1' as const,
        profile: 'python-syntax' as const, relative_path: 'src',
        snapshot_digest: '8'.repeat(64), status: 'failed' as const,
        checked_file_count: 2, isolation_mode: 'windows_appcontainer' as const,
        network_access: false as const, output_truncated: false,
        result_digest: '6'.repeat(64), issues: [{
          relative_path: 'src/broken.py', line: 2, column: 3,
          code: 'PYTHON_SYNTAX_INVALID' as const, message: 'invalid syntax',
        }],
      },
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(checked)
    const checkWrapper = mount(ResearchArtifactWorkbench)
    await checkWrapper.get('#agent-prompt').setValue('运行工作区检查：python-syntax src')
    await checkWrapper.get('.agent-composer').trigger('submit')
    await flushPromises()
    expect(checkWrapper.text()).toContain('src/broken.py:2:3')
    expect(checkWrapper.text()).toContain('AppContainer · 断网')
  })

  it('shows fixed pytest output and isolation proof in the evidence panel', async () => {
    const tested = {
      ...workbench,
      actions: [],
      route: {
        schema_version: 'deskpilot.turn-route.v1' as const,
        task_id: preview.task_id,
        conversation_id: `cnv_${'1'.repeat(64)}`,
        user_message_id: `msg_${'2'.repeat(64)}`,
        decision: 'routed' as const,
        route_id: 'workspace_python_test' as const,
        route_version: '1' as const,
        route_manifest_digest: '3'.repeat(64),
        turn_planning_adjudication_id: null,
        turn_plan_binding_id: null,
        turn_planning_provenance_digest: null,
        candidate_digest: '4'.repeat(64),
        parameter_digest: '5'.repeat(64),
        resolved_from_task_id: null,
        resolution_rule: null,
        resolution_digest: null,
        reason_code: 'WORKSPACE_PYTHON_TEST_MATCHED',
        status: 'succeeded' as const,
        result_digest: '6'.repeat(64),
        error_code: null,
        revision: 2,
        created_at: '2026-08-18T00:00:00Z',
        updated_at: '2026-08-18T00:00:01Z',
      },
      workspace_python_test: {
        schema_version: 'deskpilot.workspace-python-test.v1' as const,
        profile: 'pytest-file' as const,
        project_path: 'backend',
        test_path: 'tests/test_workspace_file_runtime.py',
        snapshot_digest: '7'.repeat(64),
        runtime_digest: '8'.repeat(64),
        status: 'passed' as const,
        exit_code: 0,
        passed_count: 3,
        failed_count: 0,
        skipped_count: 0,
        error_count: 0,
        duration_ms: 812,
        output: '3 passed in 0.81s',
        output_truncated: false,
        isolation_mode: 'windows_appcontainer' as const,
        network_access: false as const,
        process_limit: 1 as const,
        result_digest: '6'.repeat(64),
      },
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(tested)
    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue(
      '运行项目测试：backend tests/test_workspace_file_runtime.py',
    )
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('3 passed in 0.81s')
    expect(wrapper.text()).toContain('AppContainer · 断网 · 1 process')
    expect(wrapper.text()).toContain('tests/test_workspace_file_runtime.py')
  })

  it('shows fixed node:test output and isolation proof in the evidence panel', async () => {
    const tested = {
      ...workbench,
      actions: [],
      route: {
        schema_version: 'deskpilot.turn-route.v1' as const,
        task_id: preview.task_id,
        conversation_id: `cnv_${'1'.repeat(64)}`,
        user_message_id: `msg_${'2'.repeat(64)}`,
        decision: 'routed' as const,
        route_id: 'workspace_node_test' as const,
        route_version: '1' as const,
        route_manifest_digest: '3'.repeat(64),
        turn_planning_adjudication_id: null,
        turn_plan_binding_id: null,
        turn_planning_provenance_digest: null,
        candidate_digest: '4'.repeat(64),
        parameter_digest: '5'.repeat(64),
        resolved_from_task_id: null,
        resolution_rule: null,
        resolution_digest: null,
        reason_code: 'WORKSPACE_NODE_TEST_MATCHED',
        status: 'succeeded' as const,
        result_digest: '6'.repeat(64),
        error_code: null,
        revision: 2,
        created_at: '2026-08-19T00:00:00Z',
        updated_at: '2026-08-19T00:00:01Z',
      },
      workspace_node_test: {
        schema_version: 'deskpilot.workspace-node-test.v1' as const,
        profile: 'node-test-file' as const,
        project_path: 'frontend',
        test_path: 'tests/sample.test.js',
        snapshot_digest: '7'.repeat(64),
        runtime_digest: '8'.repeat(64),
        status: 'passed' as const,
        exit_code: 0,
        passed_count: 2,
        failed_count: 0,
        skipped_count: 0,
        error_count: 0,
        duration_ms: 122,
        output: 'ℹ tests 2\nℹ pass 2\nℹ fail 0',
        output_truncated: false,
        isolation_mode: 'windows_appcontainer' as const,
        network_access: false as const,
        process_limit: 1 as const,
        result_digest: '6'.repeat(64),
      },
    } as TaskWorkbench
    vi.mocked(createConversationTurn).mockResolvedValue(tested)
    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('#agent-prompt').setValue(
      '运行 Node 测试：frontend tests/sample.test.js',
    )
    await wrapper.get('.agent-composer').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('ℹ pass 2')
    expect(wrapper.text()).toContain('AppContainer · 断网 · 1 process')
    expect(wrapper.text()).toContain('tests/sample.test.js')
  })
})
