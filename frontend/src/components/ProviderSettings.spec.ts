import { flushPromises, mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProviderCatalogEntry, ProviderCatalogSnapshot } from '../types'
import { useProviderManagement } from '../composables/useProviderManagement'
import ProviderSettings from './ProviderSettings.vue'

vi.mock('../composables/useProviderManagement', () => ({
  useProviderManagement: vi.fn(),
}))

function providerEntry(): ProviderCatalogEntry {
  return {
    descriptor: {
      provider_id: 'local-fake',
      display_name: 'Local Fake',
      model: 'deterministic-v1',
      protocol: 'fake',
      location: 'local',
      capabilities: {
        streaming: true,
        structured_output: true,
        strict_json_schema: false,
        tool_calling: 'none',
        parallel_tool_calls: false,
        vision: false,
        embeddings: false,
        max_context_tokens: 32_768,
      },
    },
    enabled: true,
    is_default: false,
    cached_health: {
      provider_id: 'local-fake',
      status: 'ready',
      checked_at: '2026-08-09T08:00:00Z',
      latency_ms: 12,
      cache_status: 'fresh',
      expires_at: '2026-08-09T08:01:00Z',
    },
  }
}

function managementMock() {
  const snapshot: ProviderCatalogSnapshot = {
    catalog_version: 7,
    imported_at: '2026-08-09T08:00:00Z',
    default_provider_id: 'another-provider',
    providers: [providerEntry()],
  }
  const catalog = ref<ProviderCatalogSnapshot | null>(snapshot)
  const providers = computed(() => catalog.value?.providers ?? [])

  return {
    catalog,
    routing: ref(null),
    etag: ref('"provider-catalog-v7"'),
    providers,
    enabledCount: computed(() => providers.value.filter((entry) => entry.enabled).length),
    localCount: computed(() =>
      providers.value.filter((entry) => entry.descriptor.location === 'local').length,
    ),
    auditEvents: ref([]),
    loading: ref(false),
    auditLoading: ref(false),
    routingLoading: ref(false),
    activeMutation: ref<string | null>(null),
    healthChecking: ref<string | null>(null),
    loadError: ref<string | null>(null),
    operationError: ref<string | null>(null),
    conflictMessage: ref<string | null>(null),
    successMessage: ref<string | null>(null),
    initialize: vi.fn().mockResolvedValue(undefined),
    refreshCatalog: vi.fn().mockResolvedValue(undefined),
    refreshAudit: vi.fn().mockResolvedValue(undefined),
    refreshRouting: vi.fn().mockResolvedValue(undefined),
    createProvider: vi.fn().mockResolvedValue(true),
    updateProvider: vi.fn().mockResolvedValue(true),
    setProviderEnabled: vi.fn().mockResolvedValue(true),
    makeProviderDefault: vi.fn().mockResolvedValue(true),
    deleteProvider: vi.fn().mockResolvedValue(true),
    probeHealth: vi.fn().mockResolvedValue(undefined),
    dismissMessages: vi.fn(),
  }
}

describe('ProviderSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('展示 catalog 卡片并连接健康检查、默认、启停与删除确认流程', async () => {
    const management = managementMock()
    vi.mocked(useProviderManagement).mockReturnValue(
      management as unknown as ReturnType<typeof useProviderManagement>,
    )

    const wrapper = mount(ProviderSettings, {
      global: {
        stubs: {
          ProviderEditorModal: true,
        },
      },
    })
    await flushPromises()

    expect(management.initialize).toHaveBeenCalledOnce()
    const card = wrapper.get('.provider-card')
    expect(card.text()).toContain('Local Fake')
    expect(card.text()).toContain('local-fake')
    expect(card.text()).toContain('deterministic-v1')
    expect(card.text()).toContain('运行正常')
    expect(card.text()).toContain('12 ms')

    const button = (label: string) =>
      card.findAll('button').find((candidate) => candidate.text() === label)!

    await button('健康检查').trigger('click')
    expect(management.probeHealth).toHaveBeenCalledWith('local-fake')

    await button('设为默认').trigger('click')
    expect(management.makeProviderDefault).toHaveBeenCalledWith('local-fake')

    await button('禁用').trigger('click')
    expect(management.setProviderEnabled).toHaveBeenCalledWith('local-fake', false)

    await button('删除').trigger('click')
    expect(management.deleteProvider).not.toHaveBeenCalled()
    const confirmation = card.get('[role="alertdialog"]')
    expect(confirmation.text()).toContain('删除 Local Fake？')

    await confirmation.get('.danger-button').trigger('click')
    await flushPromises()
    expect(management.deleteProvider).toHaveBeenCalledWith('local-fake')
    expect(card.find('[role="alertdialog"]').exists()).toBe(false)
  })
})
