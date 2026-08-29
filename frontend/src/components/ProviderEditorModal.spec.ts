import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ProviderEditorModal from './ProviderEditorModal.vue'

const apiMocks = vi.hoisted(() => ({
  getManagedCredentialStatus: vi.fn(),
  storeManagedCredential: vi.fn(),
  deleteManagedCredential: vi.fn(),
}))

vi.mock('../api', () => apiMocks)

type EditorWrapper = VueWrapper<InstanceType<typeof ProviderEditorModal>>

const availableStatus = {
  schema_version: 'deskpilot.managed-credential-status.v1' as const,
  backend: 'windows_credential_manager' as const,
  identifier: 'OPENAI_RESPONSES',
  state: 'available' as const,
  writable: true as const,
  deleted: false,
}

async function mountOpen(
  props: Partial<InstanceType<typeof ProviderEditorModal>['$props']> = {},
): Promise<EditorWrapper> {
  const wrapper = mount(ProviderEditorModal, {
    attachTo: document.body,
    props: {
      open: false,
      mode: 'create',
      provider: null,
      submitting: false,
      serverError: null,
      ...props,
    },
  })
  await wrapper.setProps({ open: true })
  await nextTick()
  return wrapper
}

beforeEach(() => {
  apiMocks.getManagedCredentialStatus.mockReset()
  apiMocks.storeManagedCredential.mockReset()
  apiMocks.deleteManagedCredential.mockReset()
  apiMocks.getManagedCredentialStatus.mockResolvedValue(availableStatus)
  apiMocks.storeManagedCredential.mockResolvedValue(availableStatus)
  apiMocks.deleteManagedCredential.mockResolvedValue({
    ...availableStatus,
    state: 'missing',
    deleted: true,
  })
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ProviderEditorModal', () => {
  it('从 OpenAI 预设把 API Key 先写入安全后端，再提交不含密钥的 Responses 配置', async () => {
    const wrapper = await mountOpen()

    expect(wrapper.get('[data-testid="provider-model"]').element).toHaveProperty(
      'value',
      'gpt-5.6-luna',
    )
    expect(wrapper.get('[data-testid="provider-base-url"]').element).toHaveProperty(
      'value',
      'https://api.openai.com/v1',
    )
    const secret = wrapper.get('[data-testid="credential-secret"]')
    expect(secret.attributes('type')).toBe('password')
    expect(secret.attributes('autocomplete')).toBe('new-password')

    await secret.setValue('ui-test-secret-never-in-provider-config')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(apiMocks.storeManagedCredential).toHaveBeenCalledWith(
      'OPENAI_RESPONSES',
      'ui-test-secret-never-in-provider-config',
    )
    const emitted = wrapper.emitted('save')?.[0]?.[0]
    expect(emitted).toMatchObject({
      kind: 'openai_compatible_responses',
      enabled: false,
      provider_id: 'openai-responses',
      model: 'gpt-5.6-luna',
      base_url: 'https://api.openai.com/v1',
      credential_ref: {
        backend: 'windows_credential_manager',
        identifier: 'OPENAI_RESPONSES',
      },
    })
    expect(JSON.stringify(emitted)).not.toContain('ui-test-secret')
    expect((secret.element as HTMLInputElement).value).toBe('')
  })

  it('提供 DeepSeek、百炼和 Fake 预设，并要求百炼填写 Workspace URL', async () => {
    const wrapper = await mountOpen()

    await wrapper.get('[data-testid="provider-preset"]').setValue('deepseek')
    expect((wrapper.get('[data-testid="provider-model"]').element as HTMLInputElement).value).toBe(
      'deepseek-v4-flash',
    )
    expect((wrapper.get('[data-testid="credential-identifier"]').element as HTMLInputElement).value).toBe(
      'DEEPSEEK',
    )

    await wrapper.get('[data-testid="provider-preset"]').setValue('bailian')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('百炼北京业务空间专属')
    expect(wrapper.emitted('save')).toBeUndefined()

    await wrapper.get('[data-testid="provider-preset"]').setValue('fake')
    expect(wrapper.find('[data-testid="credential-secret"]').exists()).toBe(false)
    await wrapper.get('[data-testid="fake-delay"]').setValue('1.5')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('save')?.[0]?.[0]).toEqual({
      kind: 'fake',
      enabled: false,
      provider_id: 'fake-local',
      display_name: 'DeskPilot Fake Model',
      model: 'deskpilot-fake-v1',
      delay_seconds: 1.5,
    })
  })

  it('只展示凭据状态，并通过两次点击显式删除', async () => {
    const wrapper = await mountOpen()
    const statusButton = wrapper.findAll('button').find((button) => button.text() === '检查保存状态')
    const deleteButton = wrapper.findAll('button').find((button) => button.text() === '删除已保存 Key')

    await statusButton?.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已安全保存')
    expect(wrapper.text()).not.toContain('ui-test-secret')

    await deleteButton?.trigger('click')
    expect(apiMocks.deleteManagedCredential).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('再次点击确认删除')
    const confirmation = wrapper.findAll('button').find(
      (button) => button.text() === '再次点击确认删除',
    )
    await confirmation?.trigger('click')
    await flushPromises()
    expect(apiMocks.deleteManagedCredential).toHaveBeenCalledWith('OPENAI_RESPONSES')
    expect(wrapper.text()).toContain('尚未保存')
  })

  it('提交期间禁用关闭与再次提交', async () => {
    const wrapper = await mountOpen({ submitting: true })
    const dialog = wrapper.get('dialog')
    const closeButton = wrapper.get('button[aria-label="关闭对话框"]')
    const submitButton = wrapper.get('button[type="submit"]')

    expect(dialog.attributes('open')).toBeDefined()
    expect(closeButton.attributes('disabled')).toBeDefined()
    expect(submitButton.attributes('disabled')).toBeDefined()
    expect(submitButton.text()).toBe('安全保存中…')

    await closeButton.trigger('click')
    expect(wrapper.emitted('close')).toBeUndefined()
  })
})
