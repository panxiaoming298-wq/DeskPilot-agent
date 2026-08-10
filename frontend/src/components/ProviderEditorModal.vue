<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type {
  CredentialReference,
  ModelLocation,
  ProviderCatalogEntry,
  ProviderConfig,
} from '../types'

const props = defineProps<{
  open: boolean
  mode: 'create' | 'edit'
  provider: ProviderCatalogEntry | null
  submitting: boolean
  serverError: string | null
}>()

const emit = defineEmits<{
  close: []
  save: [config: ProviderConfig]
}>()

interface EditorForm {
  kind: 'fake' | 'openai_compatible_chat'
  enabled: boolean
  providerId: string
  displayName: string
  model: string
  delaySeconds: number
  baseUrl: string
  location: ModelLocation
  useCredential: boolean
  credentialBackend: CredentialReference['backend']
  credentialIdentifier: string
  allowPrivateNetwork: boolean
  supportsStreaming: boolean
  supportsStructuredOutput: boolean
  supportsStrictJsonSchema: boolean
  maxContextTokens: number
  maxTokensField: 'max_tokens' | 'max_completion_tokens'
  maxResponseMegabytes: number
  healthTimeoutSeconds: number
}

const dialog = ref<HTMLDialogElement | null>(null)
const form = ref<EditorForm>(emptyForm())
const localError = ref<string | null>(null)

const isEdit = computed(() => props.mode === 'edit')
const isDefault = computed(() => props.provider?.is_default ?? false)
const title = computed(() => (isEdit.value ? '重新配置 Provider' : '添加模型服务'))
const submitLabel = computed(() => {
  if (props.submitting) return '保存中…'
  return isEdit.value ? '保存完整配置' : '创建 Provider'
})

watch(
  () => props.open,
  async (open) => {
    if (open) {
      resetForm()
      await nextTick()
      if (dialog.value && !dialog.value.open) dialog.value.showModal()
    } else {
      if (dialog.value?.open) dialog.value.close()
      form.value = emptyForm()
      localError.value = null
    }
  },
)

watch(
  () => form.value.location,
  (location) => {
    if (location === 'cloud') {
      form.value.useCredential = true
      form.value.allowPrivateNetwork = false
    }
  },
)

function emptyForm(): EditorForm {
  return {
    kind: 'openai_compatible_chat',
    enabled: false,
    providerId: '',
    displayName: '',
    model: '',
    delaySeconds: 0,
    baseUrl: '',
    location: 'local',
    useCredential: false,
    credentialBackend: 'windows_credential_manager',
    credentialIdentifier: '',
    allowPrivateNetwork: false,
    supportsStreaming: true,
    supportsStructuredOutput: true,
    supportsStrictJsonSchema: false,
    maxContextTokens: 32_768,
    maxTokensField: 'max_tokens',
    maxResponseMegabytes: 4,
    healthTimeoutSeconds: 5,
  }
}

function resetForm(): void {
  localError.value = null
  const provider = props.provider
  if (!provider) {
    form.value = emptyForm()
    return
  }

  const descriptor = provider.descriptor
  form.value = {
    ...emptyForm(),
    kind: descriptor.protocol === 'fake' ? 'fake' : 'openai_compatible_chat',
    enabled: provider.enabled,
    providerId: descriptor.provider_id,
    displayName: descriptor.display_name,
    model: descriptor.model,
    location: descriptor.location,
    useCredential: descriptor.location === 'cloud',
    supportsStreaming: descriptor.capabilities.streaming,
    supportsStructuredOutput: descriptor.capabilities.structured_output,
    supportsStrictJsonSchema: descriptor.capabilities.strict_json_schema,
    maxContextTokens: descriptor.capabilities.max_context_tokens,
  }
}

function close(): void {
  if (!props.submitting) emit('close')
}

function submit(): void {
  localError.value = validateForm()
  if (localError.value) return

  const enabled = isDefault.value ? true : form.value.enabled
  if (form.value.kind === 'fake') {
    emit('save', {
      kind: 'fake',
      enabled,
      provider_id: form.value.providerId.trim(),
      display_name: form.value.displayName.trim(),
      model: form.value.model.trim(),
      delay_seconds: form.value.delaySeconds,
    })
    return
  }

  const credentialRef = buildCredentialReference()
  emit('save', {
    kind: 'openai_compatible_chat',
    enabled,
    provider_id: form.value.providerId.trim(),
    display_name: form.value.displayName.trim(),
    model: form.value.model.trim(),
    base_url: form.value.baseUrl.trim(),
    location: form.value.location,
    credential_ref: credentialRef,
    allow_private_network: form.value.location === 'local' && form.value.allowPrivateNetwork,
    supports_streaming: form.value.supportsStreaming,
    supports_structured_output: form.value.supportsStructuredOutput,
    supports_strict_json_schema: form.value.supportsStrictJsonSchema,
    max_context_tokens: form.value.maxContextTokens,
    max_tokens_field: form.value.maxTokensField,
    max_response_bytes: form.value.maxResponseMegabytes * 1024 * 1024,
    health_timeout_seconds: form.value.healthTimeoutSeconds,
  })
}

function buildCredentialReference(): CredentialReference | null {
  if (!form.value.useCredential) return null
  return {
    backend: form.value.credentialBackend,
    identifier: form.value.credentialIdentifier.trim(),
  }
}

function validateForm(): string | null {
  if (!/^[a-z][a-z0-9_-]{1,63}$/.test(form.value.providerId.trim())) {
    return 'Provider ID 需以小写字母开头，只能包含小写字母、数字、下划线或连字符。'
  }
  if (!form.value.displayName.trim() || !form.value.model.trim()) {
    return '显示名称和模型名称不能为空。'
  }
  if (form.value.kind === 'fake') {
    if (form.value.delaySeconds < 0 || form.value.delaySeconds > 60) {
      return 'Fake 延迟必须在 0～60 秒之间。'
    }
    return null
  }

  let endpoint: URL
  try {
    endpoint = new URL(form.value.baseUrl.trim())
  } catch {
    return 'Base URL 必须是完整的 HTTP(S) 地址。'
  }
  if (!['http:', 'https:'].includes(endpoint.protocol)) {
    return 'Base URL 只允许 HTTP 或 HTTPS。'
  }
  if (endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
    return 'Base URL 不能包含账号、密码、查询参数或片段。'
  }
  if (form.value.location === 'cloud' && endpoint.protocol !== 'https:') {
    return '云端 Provider 必须使用 HTTPS。'
  }
  if (form.value.location === 'cloud' && !form.value.useCredential) {
    return '云端 Provider 必须引用一个凭据。'
  }
  if (form.value.useCredential) {
    const identifier = form.value.credentialIdentifier.trim()
    const pattern = form.value.credentialBackend === 'environment'
      ? /^DESKPILOT_CREDENTIAL_[A-Z0-9_]{1,96}$/
      : /^[A-Z][A-Z0-9_]{0,95}$/
    if (!pattern.test(identifier)) return '凭据标识符与所选后端的命名规则不匹配。'
  }
  if (form.value.maxContextTokens < 1 || form.value.maxContextTokens > 10_000_000) {
    return '上下文窗口必须在 1～10,000,000 Token 之间。'
  }
  if (form.value.maxResponseMegabytes < 1 || form.value.maxResponseMegabytes > 64) {
    return '最大响应体必须在 1～64 MiB 之间。'
  }
  if (form.value.healthTimeoutSeconds <= 0 || form.value.healthTimeoutSeconds > 30) {
    return '健康检查超时必须大于 0 且不超过 30 秒。'
  }
  return null
}
</script>

<template>
  <dialog ref="dialog" class="provider-dialog" @cancel.prevent="close">
    <form class="provider-editor" @submit.prevent="submit">
      <header class="dialog-heading">
        <div>
          <span class="eyebrow">MODEL CONNECTION</span>
          <h2>{{ title }}</h2>
        </div>
        <button class="icon-button" type="button" aria-label="关闭对话框" :disabled="submitting" @click="close">×</button>
      </header>

      <div v-if="isEdit" class="security-note">
        <strong>这是一次完整替换</strong>
        <p v-if="form.kind === 'openai_compatible_chat'">出于安全原因，现有 endpoint 与凭据引用不会从 API 回传；保存前需重新填写。密钥仍只保存在环境变量或 Windows 凭据管理器中。</p>
        <p v-else>公开目录不保存 Fake 延迟，重新配置时默认使用 0 秒，请按需调整。</p>
      </div>

      <fieldset class="segmented-field" :disabled="isEdit">
        <legend>Provider 类型</legend>
        <label :class="{ selected: form.kind === 'openai_compatible_chat' }">
          <input v-model="form.kind" type="radio" value="openai_compatible_chat">
          <span><strong>OpenAI-compatible</strong><small>云端 API 或本地兼容服务</small></span>
        </label>
        <label :class="{ selected: form.kind === 'fake' }">
          <input v-model="form.kind" type="radio" value="fake">
          <span><strong>Fake</strong><small>离线开发与协议验证</small></span>
        </label>
      </fieldset>

      <div class="form-grid two-columns">
        <label class="field-control">
          <span>Provider ID</span>
          <input v-model.trim="form.providerId" :disabled="isEdit" required maxlength="64" placeholder="local-ollama">
          <small>稳定标识，创建后不可修改</small>
        </label>
        <label class="field-control">
          <span>显示名称</span>
          <input v-model="form.displayName" required maxlength="100" placeholder="本地 Qwen">
        </label>
      </div>

      <label class="field-control">
        <span>模型名称</span>
        <input v-model="form.model" required maxlength="200" placeholder="qwen3:8b">
      </label>

      <template v-if="form.kind === 'fake'">
        <label class="field-control compact-field">
          <span>模拟延迟（秒）</span>
          <input v-model.number="form.delaySeconds" type="number" min="0" max="60" step="0.1" required>
        </label>
      </template>

      <template v-else>
        <div class="form-grid endpoint-grid">
          <label class="field-control">
            <span>运行位置</span>
            <select v-model="form.location">
              <option value="local">本地</option>
              <option value="cloud">云端</option>
            </select>
          </label>
          <label class="field-control endpoint-field">
            <span>Base URL</span>
            <input v-model="form.baseUrl" type="url" required placeholder="http://127.0.0.1:11434/v1">
          </label>
        </div>

        <label v-if="form.location === 'local'" class="switch-row">
          <input v-model="form.allowPrivateNetwork" type="checkbox">
          <span><strong>允许私有网段地址</strong><small>访问非回环内网 IP 时必须显式开启；localhost 和 127.0.0.1 不需要。</small></span>
        </label>

        <section class="credential-panel">
          <label class="switch-row">
            <input v-model="form.useCredential" type="checkbox" :disabled="form.location === 'cloud'">
            <span><strong>引用凭据</strong><small>这里只保存引用，绝不通过页面提交 API Key。</small></span>
          </label>
          <div v-if="form.useCredential" class="form-grid two-columns credential-fields">
            <label class="field-control">
              <span>凭据后端</span>
              <select v-model="form.credentialBackend">
                <option value="windows_credential_manager">Windows 凭据管理器</option>
                <option value="environment">环境变量</option>
              </select>
            </label>
            <label class="field-control">
              <span>引用标识符</span>
              <input v-model="form.credentialIdentifier" required placeholder="CLOUD_CHAT">
              <small>{{ form.credentialBackend === 'environment' ? '例如 DESKPILOT_CREDENTIAL_CLOUD_CHAT' : '例如 CLOUD_CHAT' }}</small>
            </label>
          </div>
        </section>

        <details class="advanced-settings">
          <summary>高级能力与限制</summary>
          <div class="capability-editor">
            <label class="mini-switch"><input v-model="form.supportsStreaming" type="checkbox"><span>流式输出</span></label>
            <label class="mini-switch"><input v-model="form.supportsStructuredOutput" type="checkbox"><span>结构化输出</span></label>
            <label class="mini-switch"><input v-model="form.supportsStrictJsonSchema" type="checkbox"><span>严格 JSON Schema</span></label>
          </div>
          <div class="form-grid two-columns advanced-grid">
            <label class="field-control">
              <span>上下文窗口（Token）</span>
              <input v-model.number="form.maxContextTokens" type="number" min="1" max="10000000" required>
            </label>
            <label class="field-control">
              <span>输出 Token 参数</span>
              <select v-model="form.maxTokensField">
                <option value="max_tokens">max_tokens</option>
                <option value="max_completion_tokens">max_completion_tokens</option>
              </select>
            </label>
            <label class="field-control">
              <span>最大响应体（MiB）</span>
              <input v-model.number="form.maxResponseMegabytes" type="number" min="1" max="64" required>
            </label>
            <label class="field-control">
              <span>健康检查超时（秒）</span>
              <input v-model.number="form.healthTimeoutSeconds" type="number" min="0.1" max="30" step="0.1" required>
            </label>
          </div>
        </details>
      </template>

      <label class="switch-row enable-switch">
        <input v-model="form.enabled" type="checkbox" :disabled="isDefault">
        <span><strong>保存后启用</strong><small>{{ isDefault ? '默认 Provider 必须保持启用' : '启用时后端会先验证凭据和 adapter 配置' }}</small></span>
      </label>

      <p v-if="localError || serverError" class="form-error dialog-error" role="alert">
        {{ localError ?? serverError }}
      </p>

      <footer class="dialog-actions">
        <button class="text-button" type="button" :disabled="submitting" @click="close">取消</button>
        <button class="primary-button" type="submit" :disabled="submitting">{{ submitLabel }}</button>
      </footer>
    </form>
  </dialog>
</template>
