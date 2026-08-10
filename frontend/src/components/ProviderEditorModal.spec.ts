import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it } from 'vitest'
import ProviderEditorModal from './ProviderEditorModal.vue'

type EditorWrapper = VueWrapper<InstanceType<typeof ProviderEditorModal>>

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

async function fillIdentity(
  wrapper: EditorWrapper,
  values: { providerId: string; displayName: string; model: string },
): Promise<void> {
  await wrapper.get('input[placeholder="local-ollama"]').setValue(values.providerId)
  await wrapper.get('input[placeholder="本地 Qwen"]').setValue(values.displayName)
  await wrapper.get('input[placeholder="qwen3:8b"]').setValue(values.model)
}

describe('ProviderEditorModal', () => {
  it('切换为 Fake 后只提交 Fake 配置 payload', async () => {
    const wrapper = await mountOpen()

    await wrapper.get('input[type="radio"][value="fake"]').setValue()
    await fillIdentity(wrapper, {
      providerId: ' fake-local ',
      displayName: ' Fake Local ',
      model: ' deterministic-v1 ',
    })
    await wrapper.get('input[type="number"]').setValue('1.5')

    expect(wrapper.find('input[placeholder="http://127.0.0.1:11434/v1"]').exists()).toBe(false)
    expect(wrapper.find('.credential-panel').exists()).toBe(false)

    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('save')).toEqual([[
      {
        kind: 'fake',
        enabled: false,
        provider_id: 'fake-local',
        display_name: 'Fake Local',
        model: 'deterministic-v1',
        delay_seconds: 1.5,
      },
    ]])
  })

  it('云端 OpenAI-compatible 只提交凭据引用，不接收 API Key', async () => {
    const wrapper = await mountOpen()

    expect(wrapper.text()).toContain('绝不通过页面提交 API Key')
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    expect(
      wrapper.findAll('input').some((input) =>
        (input.attributes('name') ?? '').toLowerCase().includes('key'),
      ),
    ).toBe(false)

    await fillIdentity(wrapper, {
      providerId: 'cloud-chat',
      displayName: 'Cloud Chat',
      model: 'gpt-compatible',
    })
    await wrapper.get('input[placeholder="http://127.0.0.1:11434/v1"]').setValue('https://api.example.com/v1')

    const location = wrapper.findAll('select').find((select) =>
      select.find('option[value="cloud"]').exists(),
    )
    expect(location).toBeDefined()
    await location!.setValue('cloud')
    await nextTick()

    const credentialBackend = wrapper.findAll('select').find((select) =>
      select.find('option[value="environment"]').exists(),
    )
    expect(credentialBackend).toBeDefined()
    await credentialBackend!.setValue('environment')
    await wrapper.get('input[placeholder="CLOUD_CHAT"]').setValue('DESKPILOT_CREDENTIAL_CLOUD_CHAT')

    const credentialSwitch = wrapper.find('.credential-panel input[type="checkbox"]')
    expect(credentialSwitch.attributes('disabled')).toBeDefined()

    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('save')).toEqual([[
      {
        kind: 'openai_compatible_chat',
        enabled: false,
        provider_id: 'cloud-chat',
        display_name: 'Cloud Chat',
        model: 'gpt-compatible',
        base_url: 'https://api.example.com/v1',
        location: 'cloud',
        credential_ref: {
          backend: 'environment',
          identifier: 'DESKPILOT_CREDENTIAL_CLOUD_CHAT',
        },
        allow_private_network: false,
        supports_streaming: true,
        supports_structured_output: true,
        supports_strict_json_schema: false,
        max_context_tokens: 32_768,
        max_tokens_field: 'max_tokens',
        max_response_bytes: 4 * 1024 * 1024,
        health_timeout_seconds: 5,
      },
    ]])
  })

  it('dialog 跟随 open 开关，提交期间禁用关闭与再次提交', async () => {
    const wrapper = await mountOpen({ submitting: true })
    const dialog = wrapper.get('dialog')
    const closeButton = wrapper.get('button[aria-label="关闭对话框"]')
    const submitButton = wrapper.get('button[type="submit"]')

    expect(dialog.attributes('open')).toBeDefined()
    expect(closeButton.attributes('disabled')).toBeDefined()
    expect(submitButton.attributes('disabled')).toBeDefined()
    expect(submitButton.text()).toBe('保存中…')

    await closeButton.trigger('click')
    expect(wrapper.emitted('close')).toBeUndefined()

    await wrapper.setProps({ submitting: false })
    await closeButton.trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)

    await wrapper.setProps({ open: false })
    await nextTick()
    expect(dialog.attributes('open')).toBeUndefined()
  })
})
