import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  commitArtifactExport,
  createResearchWorkbenchTask,
  getTaskWorkbench,
  prepareArtifactExport,
} from '../api'
import type { ArtifactExport, TaskWorkbench } from '../types'
import ResearchArtifactWorkbench from './ResearchArtifactWorkbench.vue'

vi.mock('../api', () => ({
  ApiProblemError: class extends Error {},
  cancelWorkbenchExecution: vi.fn(),
  commitArtifactExport: vi.fn(),
  createResearchWorkbenchTask: vi.fn(),
  getTaskWorkbench: vi.fn(),
  prepareArtifactExport: vi.fn(),
  runWorkbenchStep: vi.fn(),
}))

const preview: ArtifactExport = {
  export_id: `xpt_${'1'.repeat(64)}`, delivery_id: `dlv_${'2'.repeat(64)}`,
  task_id: `tsk_${'3'.repeat(64)}`, artifact_id: `art_${'4'.repeat(64)}`,
  revision_id: `rev_${'5'.repeat(64)}`, target_path: 'D:\\Reports\\research.html',
  conflict_policy: 'fail_if_exists', status: 'prepared', source_digest: '6'.repeat(64),
  request_digest: '7'.repeat(64), confirmation_digest: '8'.repeat(64), receipt_digest: null,
  byte_count: 1024, error_code: null, requested_at: '2026-08-18T00:00:00Z', committed_at: null,
}

const workbench = {
  schema_version: 'deskpilot.task-workbench.v1',
  task: { task_id: preview.task_id, conversation_id: null, goal: '生成可核验研究页', status: 'running', mode: 'standard', privacy_mode: 'balanced', constraints: [], last_event_seq: 0, event_stream: '/events', created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:00Z' },
  stage: 'delivered',
  actions: [
    { action: 'prepare_export', enabled: true, reason_code: 'AVAILABLE', explanation: '预览写入', effect_class: 'user_path_write' },
    { action: 'stop_execution', enabled: false, reason_code: 'EXECUTION_NOT_ACTIVE', explanation: '停止', effect_class: 'execution_control' },
  ],
  conversation: [{ message_id: 'msg_1', role: 'user', content: '生成可核验研究页', created_at: '2026-08-18T00:00:00Z' }],
  planning: {}, contract: {}, plans: { plans: [] },
  executions: { runs: [{ run_id: `run_${'9'.repeat(64)}`, task_id: preview.task_id, status: 'succeeded', revision: 5, created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:01Z', invocations: [], nodes: [
    { node_id: 'node-1', local_key: 'research', status: 'verified', revision: 2, attempt_count: 1, claim_owner_id: null, claim_fencing_token: 1, claim_expires_at: null, bound_agent: null, runtime_enabled: true },
    { node_id: 'node-2', local_key: 'build_html', status: 'verified', revision: 2, attempt_count: 1, claim_owner_id: null, claim_fencing_token: 1, claim_expires_at: null, bound_agent: null, runtime_enabled: true },
    { node_id: 'node-3', local_key: 'browser_verify', status: 'verified', revision: 2, attempt_count: 1, claim_owner_id: null, claim_fencing_token: 1, claim_expires_at: null, bound_agent: null, runtime_enabled: true },
    { node_id: 'node-4', local_key: 'final_acceptance', status: 'verified', revision: 2, attempt_count: 1, claim_owner_id: null, claim_fencing_token: 1, claim_expires_at: null, bound_agent: null, runtime_enabled: true },
  ] }] },
  research: { research_session_id: 'rs_1', status: 'verified', search_calls: [], page_snapshots: [], claims: [{ claim_id: 'clm_1', statement: '这是一条已核验事实。', citation_ids: ['cit_1'], status: 'awaiting_verification' }], citations: [] },
  verification: { verification_run_id: 'vrf_1', status: 'completed', outcome: 'verified', verdicts: [{ claim_id: 'clm_1', outcome: 'verified', reason_code: 'SUPPORTED', citation_ids: ['cit_1'] }] },
  workspace: { workspace_id: 'wsp_1', status: 'delivered', artifacts: [{ artifact_id: preview.artifact_id, relative_path: 'index.html', active_revision: { revision_id: preview.revision_id, content_digest: preview.source_digest, byte_count: 1024, patch_receipt_id: 'ptr_1' } }] },
  browser: { browser_run_id: 'brr_1', status: 'passed', engine: 'chromium', viewport_width: 1280, viewport_height: 720, title: '研究页', heading_count: 2, link_count: 1, external_request_count: 0, console_error_count: 0, page_error_count: 0, issue_codes: [], screenshot_digest: '0'.repeat(64) },
  delivery: { delivery_id: preview.delivery_id, revision_id: preview.revision_id, verified_claim_ids: ['clm_1'], citation_ids: ['cit_1'], limitation_codes: [], manifest_digest: '1'.repeat(64) },
  exports: [], projection_digest: '2'.repeat(64),
} as unknown as TaskWorkbench

describe('ResearchArtifactWorkbench', () => {
  beforeEach(() => {
    vi.mocked(createResearchWorkbenchTask).mockResolvedValue(workbench)
    vi.mocked(prepareArtifactExport).mockResolvedValue(preview)
    vi.mocked(commitArtifactExport).mockResolvedValue({ ...preview, status: 'committed', receipt_digest: 'a'.repeat(64), committed_at: '2026-08-18T00:00:02Z' })
    vi.mocked(getTaskWorkbench).mockResolvedValue({ ...workbench, stage: 'exported' })
  })

  it('shows server proof and requires a separate export confirmation', async () => {
    const wrapper = mount(ResearchArtifactWorkbench)
    await wrapper.get('.goal-form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('这是一条已核验事实。')
    expect(wrapper.text()).toContain('外部请求0')
    expect(wrapper.text()).toContain('PatchReceipt')

    await wrapper.get('#export-target').setValue(preview.target_path)
    await wrapper.get('.export-form button').trigger('click')
    await flushPromises()
    expect(prepareArtifactExport).toHaveBeenCalledOnce()
    expect(commitArtifactExport).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('确认写入此路径')

    await wrapper.get('.confirm-write').trigger('click')
    await flushPromises()
    expect(commitArtifactExport).toHaveBeenCalledWith(
      preview.export_id, preview.confirmation_digest, expect.stringMatching(/^commit-/),
    )
    expect(wrapper.text()).toContain('不可变导出回执')
  })
})
