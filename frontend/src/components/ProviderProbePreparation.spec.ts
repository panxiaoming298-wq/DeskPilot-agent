import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getProviderProbePreparation, prepareProviderProbe } from '../api'
import type {
  ProviderProbePreparationManifest,
  ProviderProbePreparationResult,
  ProviderProbePreparationProfile,
} from '../types'
import ProviderProbePreparation from './ProviderProbePreparation.vue'

vi.mock('../api', () => ({
  getProviderProbePreparation: vi.fn(),
  prepareProviderProbe: vi.fn(),
}))

function profile(
  overrides: Partial<ProviderProbePreparationProfile> = {},
): ProviderProbePreparationProfile {
  return {
    provider_family: 'openai',
    provider_id: 'openai-responses',
    display_name: 'OpenAI',
    exact_model: 'gpt-5.6-luna',
    suggested_base_url: 'https://api.openai.com/v1',
    base_url_editable: false,
    credential_identifier: 'OPENAI_RESPONSES',
    currency: 'USD',
    maximum_total_microunits: 5_000_000,
    maximum_per_request_microunits: 250_000,
    maximum_requests: 16,
    planned_budget_envelope_microunits: 1_000_000,
    allowed_cost_control_modes: [
      'openai_project_hard_limit',
      'openai_application_envelope',
    ],
    prepaid_balance_check_required: false,
    billing_alert_required: false,
    billing_delay_acknowledgement_required: false,
    free_quota_stop_recommended: false,
    ...overrides,
  }
}

const manifest: ProviderProbePreparationManifest = {
  schema_version: 'deskpilot.provider-probe-preparation-manifest.v1',
  policy_id: 'phase115-three-provider-public-probe',
  policy_digest: `sha256:${'a'.repeat(64)}`,
  data_class: 'public_synthetic',
  planned_requests_per_provider: 4,
  planned_aggregate_requests: 12,
  profiles: [
    profile(),
    profile({
      provider_family: 'deepseek',
      provider_id: 'deepseek-responses',
      display_name: 'DeepSeek',
      exact_model: 'deepseek-v4-flash',
      suggested_base_url: 'https://api.deepseek.com',
      credential_identifier: 'DEEPSEEK',
      maximum_total_microunits: 2_000_000,
      maximum_per_request_microunits: 100_000,
      planned_budget_envelope_microunits: 400_000,
      maximum_requests: 10,
      allowed_cost_control_modes: ['deepseek_prepaid_balance'],
      prepaid_balance_check_required: true,
    }),
    profile({
      provider_family: 'bailian',
      provider_id: 'bailian-responses',
      display_name: '阿里云百炼',
      exact_model: 'qwen3.8-max',
      suggested_base_url: null,
      base_url_editable: true,
      credential_identifier: 'BAILIAN',
      currency: 'CNY',
      maximum_total_microunits: 20_000_000,
      maximum_per_request_microunits: 2_000_000,
      planned_budget_envelope_microunits: 8_000_000,
      maximum_requests: 10,
      allowed_cost_control_modes: ['bailian_billing_alert'],
      billing_alert_required: true,
      billing_delay_acknowledgement_required: true,
      free_quota_stop_recommended: true,
    }),
  ],
  network_access: false,
  credentials_resolved: false,
  real_model_capture: false,
  production_admission: false,
  cloud_activation: false,
  full_116c_b: false,
}

const readyResult: ProviderProbePreparationResult = {
  schema_version: 'deskpilot.provider-probe-preparation-result.v1',
  binding: {
    schema_version: 'deskpilot.provider-probe-operator-binding.v2',
    policy_digest: manifest.policy_digest,
    provider_family: 'openai',
    provider_id: 'openai-responses',
    exact_model: 'gpt-5.6-luna',
    base_url: 'https://api.openai.com/v1',
    credential_ref: { backend: 'windows_credential_manager', identifier: 'OPENAI_RESPONSES' },
    currency: 'USD',
    maximum_total_microunits: 5_000_000,
    maximum_per_request_microunits: 250_000,
    maximum_requests: 16,
    automatic_retries: 0,
    exact_model_confirmed: true,
    credential_presence_confirmed: true,
    base_url_key_pair_confirmed: true,
    cost_control_mode: 'openai_application_envelope',
    provider_hard_limit_enforcing: false,
    dedicated_probe_credential_confirmed: true,
    application_budget_envelope_confirmed: true,
    prepaid_balance_available_confirmed: false,
    prepaid_balance_checked_at: null,
    billing_alert_confirmed: false,
    billing_delay_acknowledged: false,
    free_quota_stop_enabled: false,
    pricing_source_checked_at: '2026-08-29T08:00:00Z',
    confirmed_by: 'reviewer_local_owner',
    confirmed_at: '2026-08-29T08:00:00Z',
    valid_until: '2026-08-30T08:00:00Z',
    binding_digest: `sha256:${'b'.repeat(64)}`,
  },
  readiness: {
    schema_version: 'deskpilot.provider-probe-readiness.v2',
    policy_digest: manifest.policy_digest,
    binding_digest: `sha256:${'b'.repeat(64)}`,
    provider_family: 'openai',
    provider_id: 'openai-responses',
    model: 'gpt-5.6-luna',
    public_config_digest: `sha256:${'c'.repeat(64)}`,
    credential_reference_digest: `sha256:${'d'.repeat(64)}`,
    planned_request_count: 4,
    maximum_requests: 16,
    currency: 'USD',
    maximum_total_microunits: 5_000_000,
    maximum_per_request_microunits: 250_000,
    planned_budget_envelope_microunits: 1_000_000,
    cost_control_mode: 'openai_application_envelope',
    provider_hard_limit_enforcing: false,
    dedicated_probe_credential_confirmed: true,
    application_budget_envelope_confirmed: true,
    ready: true,
    violations: [],
    checked_at: '2026-08-29T08:00:00Z',
    network_access: false,
    credentials_resolved: false,
    real_model_capture: false,
    production_admission: false,
    cloud_activation: false,
  },
  readiness_report_digest: `sha256:${'e'.repeat(64)}`,
  live_permit_created: false,
  network_access: false,
}

describe('ProviderProbePreparation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getProviderProbePreparation).mockResolvedValue(manifest)
    vi.mocked(prepareProviderProbe).mockResolvedValue(readyResult)
  })

  it('显示三个冻结 Provider 与明确的禁用边界', async () => {
    const wrapper = mount(ProviderProbePreparation)
    await flushPromises()

    expect(getProviderProbePreparation).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('OpenAI')
    expect(wrapper.text()).toContain('DeepSeek')
    expect(wrapper.text()).toContain('阿里云百炼')
    expect(wrapper.text()).toContain('网络关闭')
    expect(wrapper.text()).toContain('Production Admission 关闭')
    expect(wrapper.findAll('button').some((button) => button.text() === '运行真实模型')).toBe(false)
    expect(wrapper.get('input[type="url"]').attributes('readonly')).toBeDefined()
  })

  it('点击检查时立即列出未完成项，不发送部分确认', async () => {
    const wrapper = mount(ProviderProbePreparation)
    await flushPromises()

    await wrapper.get('.run-button').trigger('click')

    expect(prepareProviderProbe).not.toHaveBeenCalled()
    expect(wrapper.get('.blocker-list').text()).toContain('还有 6 项未完成')
    expect(wrapper.get('.blocker-list').text()).toContain('今日已核对官方计费来源')
  })

  it('只提交公开配置和确认，并展示无网通过结果', async () => {
    const wrapper = mount(ProviderProbePreparation)
    await flushPromises()

    for (const checkbox of wrapper.findAll<HTMLInputElement>('input[type="checkbox"]')) {
      await checkbox.setValue(true)
    }
    await wrapper.get('.run-button').trigger('click')
    await flushPromises()

    expect(prepareProviderProbe).toHaveBeenCalledWith(expect.objectContaining({
      provider_family: 'openai',
      exact_model: 'gpt-5.6-luna',
      credential_identifier: 'OPENAI_RESPONSES',
      base_url: 'https://api.openai.com/v1',
      pricing_source_confirmed: true,
    }))
    expect(JSON.stringify(vi.mocked(prepareProviderProbe).mock.calls[0])).not.toContain('api_key')
    expect(wrapper.text()).toContain('准备材料已通过')
    expect(wrapper.text()).toContain('实时许可已创建否')
  })

  it('百炼要求可编辑 Workspace 地址和账单延迟确认', async () => {
    const wrapper = mount(ProviderProbePreparation)
    await flushPromises()

    await wrapper.findAll('[role="tab"]')[2].trigger('click')

    expect(wrapper.get('input[type="url"]').attributes('readonly')).toBeUndefined()
    expect(wrapper.text()).toContain('Workspace 的 Responses 兼容地址')
    expect(wrapper.text()).toContain('已知晓百炼账单数据可能延迟')
  })
})
