import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { callMcpTool, getMcpAudit, listMcpServers, setMcpServerEnabled } from '../api'
import McpConnections from './McpConnections.vue'

vi.mock('../api', () => ({
  callMcpTool: vi.fn(),
  getMcpAudit: vi.fn(),
  listMcpServers: vi.fn(),
  setMcpServerEnabled: vi.fn(),
}))

const disabledServer = {
  server_id: 'deskpilot.readonly-text',
  title: 'DeskPilot 只读文本 Server',
  transport: 'stdio' as const,
  protocol_version: '2025-11-25',
  command_preview: ['python', '-I', '<bundled>/readonly_text_server.py'],
  enabled: false,
  revision: 0,
  network_access: false,
  filesystem_roots: [],
  client_capabilities: [],
  tools: [{
    name: 'deskpilot.text.metrics',
    title: '本地文本指标',
    description: '只读文本指标',
    risk_floor: 'R0' as const,
    input_schema: {},
    output_schema: {},
    schema_digest: 'a'.repeat(64),
  }],
  bundle_digest: 'e'.repeat(64),
  manifest_digest: 'b'.repeat(64),
  updated_at: null,
}
const enabledServer = { ...disabledServer, enabled: true, revision: 1 }

describe('McpConnections', () => {
  beforeEach(() => {
    vi.mocked(listMcpServers)
      .mockResolvedValueOnce([disabledServer])
      .mockResolvedValue([enabledServer])
    vi.mocked(getMcpAudit).mockResolvedValue({ events: [], next_after_sequence: 0 })
    vi.mocked(setMcpServerEnabled).mockResolvedValue({
      server: enabledServer,
      audit_event_id: 'mca_enable',
    })
    vi.mocked(callMcpTool).mockResolvedValue({
      server_id: enabledServer.server_id,
      tool_name: 'deskpilot.text.metrics',
      protocol_version: '2025-11-25',
      structured_content: { character_count: 5, line_count: 1, word_count: 1 },
      request_digest: 'c'.repeat(64),
      result_digest: 'd'.repeat(64),
      audit_event_id: 'mca_call',
    })
  })

  it('requires explicit enablement before invoking the fixed tool', async () => {
    const wrapper = mount(McpConnections)
    await flushPromises()
    expect(wrapper.text()).toContain('默认禁用')
    expect(wrapper.text()).toContain('不提供 roots / sampling / elicitation')
    expect(wrapper.findAll('button')[1].attributes('disabled')).toBeDefined()

    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(setMcpServerEnabled).toHaveBeenCalledWith(enabledServer.server_id, true)
    expect(wrapper.text()).toContain('Server 已显式启用')

    await wrapper.get('textarea').setValue('hello')
    await wrapper.findAll('button')[1].trigger('click')
    await flushPromises()
    expect(callMcpTool).toHaveBeenCalledWith(
      enabledServer.server_id,
      'deskpilot.text.metrics',
      { text: 'hello' },
    )
    expect(wrapper.text()).toContain('character_count')
  })
})
