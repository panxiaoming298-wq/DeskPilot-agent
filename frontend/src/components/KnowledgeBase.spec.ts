import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { importKnowledgeSource, listKnowledgeSources, searchKnowledge } from '../api'
import KnowledgeBase from './KnowledgeBase.vue'

vi.mock('../api', () => ({
  importKnowledgeSource: vi.fn(),
  listKnowledgeSources: vi.fn(),
  searchKnowledge: vi.fn(),
}))

const source = {
  source_id: 'ksr_source',
  canonical_path: 'D:\\docs\\runbook.md',
  artifact_id: `art_${'a'.repeat(64)}`,
  source_version: 'b'.repeat(64),
  content_digest: 'a'.repeat(64),
  byte_size: 42,
  chunk_count: 1,
  manifest_digest: 'c'.repeat(64),
  imported_at: '2026-08-16T00:00:00Z',
  updated_at: '2026-08-16T00:00:00Z',
}

describe('KnowledgeBase', () => {
  beforeEach(() => {
    vi.mocked(listKnowledgeSources).mockResolvedValue([])
    vi.mocked(importKnowledgeSource).mockResolvedValue(source)
    vi.mocked(searchKnowledge).mockResolvedValue({
      query_digest: 'd'.repeat(64),
      citations: [{
        source_id: source.source_id,
        artifact_id: source.artifact_id,
        chunk_id: `kch_${'e'.repeat(64)}`,
        canonical_path: source.canonical_path,
        locator: 'L1-L2',
        snippet: '先核对来源，再执行恢复。',
        score: 3,
        text_digest: 'f'.repeat(64),
        chunk_proof_digest: '1'.repeat(64),
        retrieval_proof_digest: '2'.repeat(64),
      }],
      searched_sources: 1,
      stale_source_ids: [],
      result_digest: '3'.repeat(64),
    })
  })

  it('imports an explicit path and renders proof-bearing search results', async () => {
    const wrapper = mount(KnowledgeBase)
    await flushPromises()

    await wrapper.get('input[name="source-path"]').setValue(source.canonical_path)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(importKnowledgeSource).toHaveBeenCalledWith(source.canonical_path)
    expect(wrapper.text()).toContain('已导入 1 个可验证分块')

    await wrapper.get('input[name="knowledge-query"]').setValue('恢复')
    await wrapper.findAll('form')[1].trigger('submit')
    await flushPromises()
    expect(searchKnowledge).toHaveBeenCalledWith('恢复')
    expect(wrapper.text()).toContain('先核对来源，再执行恢复')
    expect(wrapper.text()).toContain('proof 2222222222222222')
  })
})
