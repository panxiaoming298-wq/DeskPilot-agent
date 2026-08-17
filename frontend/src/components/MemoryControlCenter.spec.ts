import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { LongTermMemoryPage } from '../types'
import MemoryControlCenter from './MemoryControlCenter.vue'

const api = vi.hoisted(() => ({
  getLongTermMemory: vi.fn(),
  createLongTermMemory: vi.fn(),
  confirmMemoryProposal: vi.fn(),
  rejectMemoryProposal: vi.fn(),
  editLongTermMemory: vi.fn(),
  deleteLongTermMemory: vi.fn(),
  resolveMemoryConflict: vi.fn(),
  exportLongTermMemory: vi.fn(),
}))

vi.mock('../api', () => api)

const active = {
  memory_id: `mem_${'1'.repeat(64)}`,
  proposal_id: `mpr_${'2'.repeat(64)}`,
  key: 'response.language',
  version: 1,
  kind: 'preference' as const,
  value: '始终使用中文回答',
  source_type: 'user_explicit' as const,
  source_id: 'local-user',
  source_digest: '3'.repeat(64),
  created_by: 'user' as const,
  scope: 'user' as const,
  classification: 'internal' as const,
  confidence: 1,
  status: 'active' as const,
  value_digest: '4'.repeat(64),
  item_digest: '5'.repeat(64),
  supersedes_memory_id: null,
  created_at: '2026-08-17T00:00:00Z',
  expires_at: null,
  deleted_at: null,
}

const pending = {
  proposal_id: `mpr_${'6'.repeat(64)}`,
  key: 'profile.timezone',
  kind: 'user_confirmed_fact' as const,
  value: 'Asia/Shanghai',
  source_type: 'agent_result' as const,
  source_id: `res_${'7'.repeat(64)}`,
  source_digest: '8'.repeat(64),
  created_by: 'agent' as const,
  scope: 'user' as const,
  classification: 'internal' as const,
  confidence: 0.7,
  status: 'pending_confirmation' as const,
  value_digest: '9'.repeat(64),
  proposal_digest: 'a'.repeat(64),
  created_at: '2026-08-17T00:00:00Z',
  expires_at: null,
  decided_at: null,
}

function page(overrides: Partial<LongTermMemoryPage> = {}): LongTermMemoryPage {
  return {
    items: [active],
    proposals: [pending],
    conflicts: [],
    usage: [{
      usage_id: `mus_${'b'.repeat(64)}`,
      memory_id: active.memory_id,
      memory_version: 1,
      task_id: `tsk_${'c'.repeat(32)}`,
      invocation_id: `inv_${'d'.repeat(64)}`,
      context_manifest_id: `cmf_${'e'.repeat(64)}`,
      agent_id: 'builtin.web_researcher',
      provider_id: 'fake-local',
      provider_location: 'local',
      purpose: 'model_context',
      supplied_at: '2026-08-17T01:00:00Z',
      policy_reference: 'deskpilot.long-term-memory-policy.v1',
      deleted_after_use: false,
    }],
    ...overrides,
  }
}

describe('MemoryControlCenter', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getLongTermMemory.mockResolvedValue(page())
    api.confirmMemoryProposal.mockResolvedValue(page({ proposals: [] }))
    api.rejectMemoryProposal.mockResolvedValue(page({ proposals: [] }))
    api.createLongTermMemory.mockResolvedValue(page())
    api.editLongTermMemory.mockResolvedValue(page())
    api.deleteLongTermMemory.mockResolvedValue(page({ items: [{ ...active, value: null, status: 'deleted', deleted_at: '2026-08-17T02:00:00Z' }] }))
    api.resolveMemoryConflict.mockResolvedValue(page())
  })

  it('shows provenance and requires an explicit decision for a pending proposal', async () => {
    const wrapper = mount(MemoryControlCenter)
    await flushPromises()

    expect(wrapper.text()).toContain('builtin.web_researcher')
    expect(wrapper.text()).toContain('fake-local · local')
    await wrapper.findAll('.ledger-row').find((row) => row.text().includes('profile.timezone'))!.trigger('click')
    expect(wrapper.text()).toContain('agent_result')
    await wrapper.get('.decision-block .accent-button').trigger('click')
    await flushPromises()
    expect(api.confirmMemoryProposal).toHaveBeenCalledWith(pending.proposal_id)
  })

  it('creates a typed memory and uses a two-step destructive confirmation', async () => {
    const wrapper = mount(MemoryControlCenter)
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text() === '新建记忆')!.trigger('click')
    await wrapper.get('input[name="memory-key"]').setValue('writing.tone')
    await wrapper.get('textarea[name="memory-value"]').setValue('保持简洁')
    await wrapper.get('.memory-create').trigger('submit')
    await flushPromises()
    expect(api.createLongTermMemory).toHaveBeenCalledWith(expect.objectContaining({
      key: 'writing.tone', kind: 'preference', value: '保持简洁',
    }))

    await wrapper.findAll('button').find((button) => button.text() === '删除记忆')!.trigger('click')
    expect(api.deleteLongTermMemory).not.toHaveBeenCalled()
    await wrapper.findAll('button').find((button) => button.text() === '再次点击，确认删除')!.trigger('click')
    await flushPromises()
    expect(api.deleteLongTermMemory).toHaveBeenCalledWith(active.memory_id)
  })
})
