<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  deleteManagedCredential,
  getManagedCredentialStatus,
  storeManagedCredential,
} from '../api'
import type {
  ManagedCredentialStatus,
  ModelLocation,
  ProviderCatalogEntry,
  ProviderConfig,
} from '../types'

type ProviderPreset = 'openai' | 'deepseek' | 'bailian' | 'custom' | 'fake'
type CompatibleKind = 'openai_compatible_chat' | 'openai_compatible_responses'

interface EditorForm {
  preset: ProviderPreset
  kind: 'fake' | CompatibleKind
  enabled: boolean
  providerId: string
  displayName: string
  model: string
  delaySeconds: number
  endpointAddress: string
  location: ModelLocation
  useCredential: boolean
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

const dialog = ref<HTMLDialogElement | null>(null)
const form = ref<EditorForm>(emptyForm())
const apiKey = ref('')
const revealApiKey = ref(false)
const localError = ref<string | null>(null)
const credentialStatus = ref<ManagedCredentialStatus | null>(null)
const credentialBusy = ref(false)
const credentialDeleteArmed = ref(false)

const isEdit = computed(() => props.mode === 'edit')
const isDefault = computed(() => props.provider?.is_default ?? false)
const busy = computed(() => props.submitting || credentialBusy.value)
const isCloudProvider = computed(() => form.value.kind !== 'fake' && form.value.location === 'cloud')
const title = computed(() => (isEdit.value ? '重新配置 Provider' : '连接模型服务'))
const submitLabel = computed(() => {
  if (busy.value) return '安全保存中…'
  return isEdit.value ? '保存完整配置' : '添加 Provider'
})
const credentialStatusLabel = computed(() => {
  if (!credentialStatus.value) return '尚未检查'
  return {
    available: '已安全保存',
    missing: '尚未保存',
    invalid: '需要重新保存',
  }[credentialStatus.value.state]
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
      clearSensitiveState()
      form.value = emptyForm()
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

watch(
  () => form.value.credentialIdentifier,
  () => {
    credentialStatus.value = null
    credentialDeleteArmed.value = false
  },
)

function emptyForm(): EditorForm {
  return {
    preset: 'openai',
    kind: 'openai_compatible_responses',
    enabled: false,
    providerId: 'openai-responses',
    displayName: 'OpenAI',
    model: 'gpt-5.6-luna',
    delaySeconds: 0,
    endpointAddress: 'https://api.openai.com/v1',
    location: 'cloud',
    useCredential: true,
    credentialIdentifier: 'OPENAI_RESPONSES',
    allowPrivateNetwork: false,
    supportsStreaming: true,
    supportsStructuredOutput: true,
    supportsStrictJsonSchema: true,
    maxContextTokens: 128_000,
    maxTokensField: 'max_tokens',
    maxResponseMegabytes: 4,
    healthTimeoutSeconds: 5,
  }
}

function resetForm(): void {
  clearSensitiveState()
  const provider = props.provider
  if (!provider) {
    form.value = emptyForm()
    return
  }
  const descriptor = provider.descriptor
  const preset = inferPreset(descriptor.provider_id, descriptor.protocol)
  const replacement = preset === 'custom' ? presetForm('custom') : presetForm(preset)
  form.value = {
    ...replacement,
    preset,
    kind: descriptor.protocol === 'fake'
      ? 'fake'
      : descriptor.protocol === 'openai_responses'
        ? 'openai_compatible_responses'
        : 'openai_compatible_chat',
    enabled: provider.enabled,
    providerId: descriptor.provider_id,
    displayName: descriptor.display_name,
    model: descriptor.model,
    location: descriptor.location,
    useCredential: descriptor.location === 'cloud' || replacement.useCredential,
    supportsStreaming: descriptor.capabilities.streaming,
    supportsStructuredOutput: descriptor.capabilities.structured_output,
    supportsStrictJsonSchema: descriptor.capabilities.strict_json_schema,
    maxContextTokens: descriptor.capabilities.max_context_tokens,
  }
}

function inferPreset(providerId: string, protocol: ProviderCatalogEntry['descriptor']['protocol']): ProviderPreset {
  if (protocol === 'fake') return 'fake'
  if (providerId.startsWith('openai')) return 'openai'
  if (providerId.startsWith('deepseek')) return 'deepseek'
  if (providerId.startsWith('bailian')) return 'bailian'
  return 'custom'
}

function presetForm(preset: ProviderPreset): EditorForm {
  const base = emptyForm()
  if (preset === 'openai') return base
  if (preset === 'deepseek') {
    return {
      ...base,
      preset,
      providerId: 'deepseek-responses',
      displayName: 'DeepSeek',
      model: 'deepseek-v4-flash',
      endpointAddress: 'https://api.deepseek.com',
      credentialIdentifier: 'DEEPSEEK',
    }
  }
  if (preset === 'bailian') {
    return {
      ...base,
      preset,
      providerId: 'bailian-responses',
      displayName: '阿里云百炼',
      model: 'qwen3.8-max',
      endpointAddress: '',
      credentialIdentifier: 'BAILIAN',
    }
  }
  if (preset === 'fake') {
    return {
      ...base,
      preset,
      kind: 'fake',
      providerId: 'fake-local',
      displayName: 'DeskPilot Fake Model',
      model: 'deskpilot-fake-v1',
      location: 'local',
      useCredential: false,
      supportsStrictJsonSchema: false,
      maxContextTokens: 32_768,
    }
  }
  return {
    ...base,
    preset,
    kind: 'openai_compatible_chat',
    providerId: '',
    displayName: '',
    model: '',
    endpointAddress: '',
    location: 'local',
    useCredential: false,
    credentialIdentifier: '',
    supportsStrictJsonSchema: false,
    maxContextTokens: 32_768,
  }
}

function applyPreset(): void {
  const enabled = form.value.enabled
  form.value = { ...presetForm(form.value.preset), enabled }
  clearSensitiveState()
}

function clearSensitiveState(): void {
  apiKey.value = ''
  revealApiKey.value = false
  localError.value = null
  credentialStatus.value = null
  credentialDeleteArmed.value = false
}

function close(): void {
  if (busy.value) return
  clearSensitiveState()
  emit('close')
}

async function submit(): Promise<void> {
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
  if (
    form.value.useCredential
    && (enabled || apiKey.value.length > 0)
    && !(await prepareCredential(enabled))
  ) return
  emit('save', buildCompatibleConfig(enabled))
}

async function prepareCredential(enabled: boolean): Promise<boolean> {
  const identifier = form.value.credentialIdentifier.trim()
  credentialBusy.value = true
  localError.value = null
  try {
    if (apiKey.value) {
      credentialStatus.value = await storeManagedCredential(identifier, apiKey.value)
      apiKey.value = ''
      revealApiKey.value = false
      return true
    }
    credentialStatus.value = await getManagedCredentialStatus(identifier)
    if (enabled && credentialStatus.value.state !== 'available') {
      localError.value = '启用云端 Provider 前，请先输入并安全保存 API Key。'
      return false
    }
    return true
  } catch (error) {
    localError.value = error instanceof Error ? error.message : 'API Key 未能安全保存。'
    return false
  } finally {
    credentialBusy.value = false
  }
}

function buildCompatibleConfig(enabled: boolean): ProviderConfig {
  const normalizedBaseUrl = normalizeValue(form.value.endpointAddress)
  const common = {
    enabled,
    provider_id: form.value.providerId.trim(),
    display_name: form.value.displayName.trim(),
    model: form.value.model.trim(),
    base_url: normalizedBaseUrl,
    location: form.value.location,
    credential_ref: form.value.useCredential
      ? {
          backend: 'windows_credential_manager' as const,
          identifier: form.value.credentialIdentifier.trim(),
        }
      : null,
    allow_private_network: form.value.location === 'local' && form.value.allowPrivateNetwork,
    supports_streaming: form.value.supportsStreaming,
    supports_structured_output: form.value.supportsStructuredOutput,
    supports_strict_json_schema: form.value.supportsStrictJsonSchema,
    max_context_tokens: form.value.maxContextTokens,
    max_response_bytes: form.value.maxResponseMegabytes * 1024 * 1024,
    health_timeout_seconds: form.value.healthTimeoutSeconds,
  }
  if (form.value.kind === 'openai_compatible_responses') {
    return { kind: 'openai_compatible_responses', ...common }
  }
  return {
    kind: 'openai_compatible_chat',
    ...common,
    max_tokens_field: form.value.maxTokensField,
  }
}

function normalizeValue(value: string): string {
  return value.trim()
}

async function refreshCredentialStatus(): Promise<void> {
  const identifier = form.value.credentialIdentifier.trim()
  if (!/^[A-Z][A-Z0-9_]{0,95}$/.test(identifier)) {
    localError.value = '请先填写有效的凭据标识符。'
    return
  }
  credentialBusy.value = true
  localError.value = null
  try {
    credentialStatus.value = await getManagedCredentialStatus(identifier)
  } catch (error) {
    localError.value = error instanceof Error ? error.message : '无法读取 API Key 状态。'
  } finally {
    credentialBusy.value = false
  }
}

async function removeCredential(): Promise<void> {
  if (props.provider?.enabled) return
  if (!credentialDeleteArmed.value) {
    credentialDeleteArmed.value = true
    return
  }
  credentialBusy.value = true
  localError.value = null
  try {
    credentialStatus.value = await deleteManagedCredential(form.value.credentialIdentifier.trim())
    credentialDeleteArmed.value = false
  } catch (error) {
    localError.value = error instanceof Error ? error.message : '无法删除 API Key。'
  } finally {
    credentialBusy.value = false
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
    return form.value.delaySeconds < 0 || form.value.delaySeconds > 60
      ? 'Fake 延迟必须在 0～60 秒之间。'
      : null
  }
  let endpoint: URL
  const endpointInput = form.value.endpointAddress
  const urlConstructor = URL
  try {
    endpoint = new urlConstructor(normalizeValue(endpointInput))
  } catch {
    return form.value.preset === 'bailian'
      ? '请填写百炼北京业务空间专属的完整 Base URL。'
      : 'Base URL 必须是完整的 HTTP(S) 地址。'
  }
  if (!['http:', 'https:'].includes(endpoint.protocol)) return 'Base URL 只允许 HTTP 或 HTTPS。'
  if (endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
    return 'Base URL 不能包含账号、密码、查询参数或片段。'
  }
  if (form.value.location === 'cloud' && endpoint.protocol !== 'https:') {
    return '云端 Provider 必须使用 HTTPS。'
  }
  if (isCloudProvider.value && !form.value.useCredential) return '云端 Provider 必须引用一个凭据。'
  if (form.value.useCredential && !/^[A-Z][A-Z0-9_]{0,95}$/.test(form.value.credentialIdentifier.trim())) {
    return '凭据标识符只能使用大写字母、数字和下划线。'
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
          <span class="eyebrow">模型连接</span>
          <h2>{{ title }}</h2>
          <p>选择常用服务，或配置任意 OpenAI-compatible endpoint。</p>
        </div>
        <button class="icon-button" type="button" aria-label="关闭对话框" :disabled="busy" @click="close">×</button>
      </header>

      <div v-if="isEdit" class="security-note">
        <strong>安全完整替换</strong>
        <p>现有 endpoint 和凭据引用不会从后端回传。预设会恢复官方配置；自定义服务需要重新填写。API Key 留空时不会覆盖已保存值。</p>
      </div>

      <label class="field-control preset-control">
        <span>服务预设</span>
        <select v-model="form.preset" :disabled="isEdit" data-testid="provider-preset" @change="applyPreset">
          <option value="openai">OpenAI</option>
          <option value="deepseek">DeepSeek</option>
          <option value="bailian">阿里云百炼</option>
          <option value="custom">自定义兼容服务</option>
          <option value="fake">Fake（离线）</option>
        </select>
        <small v-if="form.preset === 'bailian'">本版本固定北京业务空间；Workspace Base URL 需从百炼控制台复制。</small>
        <small v-else-if="form.preset !== 'custom' && form.preset !== 'fake'">模型与 endpoint 来自项目内冻结策略，仍可按账户实际情况修改。</small>
      </label>

      <label v-if="form.preset === 'custom'" class="field-control compact-field">
        <span>兼容协议</span>
        <select v-model="form.kind" data-testid="provider-protocol">
          <option value="openai_compatible_chat">Chat Completions</option>
          <option value="openai_compatible_responses">Responses</option>
        </select>
      </label>

      <div class="form-grid two-columns">
        <label class="field-control">
          <span>Provider ID</span>
          <input v-model.trim="form.providerId" data-testid="provider-id" :disabled="isEdit" required maxlength="64" placeholder="local-ollama">
          <small>稳定标识，创建后不可修改</small>
        </label>
        <label class="field-control">
          <span>显示名称</span>
          <input v-model="form.displayName" data-testid="provider-name" required maxlength="100" placeholder="本地 Qwen">
        </label>
      </div>

      <label class="field-control">
        <span>模型名称</span>
        <input v-model="form.model" data-testid="provider-model" required maxlength="200" placeholder="qwen3:8b">
      </label>

      <label v-if="form.kind === 'fake'" class="field-control compact-field">
        <span>模拟延迟（秒）</span>
        <input v-model.number="form.delaySeconds" data-testid="fake-delay" type="number" min="0" max="60" step="0.1" required>
      </label>

      <template v-else>
        <div class="form-grid endpoint-grid">
          <label class="field-control">
            <span>运行位置</span>
            <select v-model="form.location" data-testid="provider-location">
              <option value="local">本地</option>
              <option value="cloud">云端</option>
            </select>
          </label>
          <label class="field-control endpoint-field">
            <span>Base URL</span>
            <input
              v-model="form.endpointAddress"
              data-testid="provider-base-url"
              type="url"
              required
              :placeholder="form.preset === 'bailian' ? 'https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1' : 'http://127.0.0.1:11434/v1'"
            >
          </label>
        </div>

        <label v-if="form.location === 'local'" class="switch-row">
          <input v-model="form.allowPrivateNetwork" type="checkbox">
          <span><strong>允许私有网段地址</strong><small>访问非回环内网 IP 时必须显式开启。</small></span>
        </label>

        <section class="credential-panel">
          <div class="credential-panel-heading">
            <div>
              <strong>API Key</strong>
              <p>仅发送到本机后端，并写入当前 Windows 用户的凭据管理器；不会进入 Provider 配置或数据库。</p>
            </div>
            <span class="credential-status" :data-state="credentialStatus?.state ?? 'unknown'">{{ credentialStatusLabel }}</span>
          </div>

          <label v-if="form.location === 'local'" class="switch-row">
            <input v-model="form.useCredential" type="checkbox">
            <span><strong>本地服务需要凭据</strong><small>无鉴权的本地服务可以关闭。</small></span>
          </label>

          <template v-if="form.useCredential">
            <div class="form-grid two-columns credential-fields">
              <label class="field-control">
                <span>安全存储标识</span>
                <input v-model="form.credentialIdentifier" data-testid="credential-identifier" required maxlength="96" placeholder="CLOUD_CHAT" autocomplete="off">
                <small>只保存引用名称，不保存到日志</small>
              </label>
              <label class="field-control">
                <span>API Key</span>
                <span class="secret-input-wrap">
                  <input
                    v-model="apiKey"
                    data-testid="credential-secret"
                    :type="revealApiKey ? 'text' : 'password'"
                    name="provider-api-key"
                    maxlength="2560"
                    autocomplete="new-password"
                    :spellcheck="false"
                    placeholder="留空则保留已保存的 Key"
                  >
                  <button type="button" :aria-label="revealApiKey ? '隐藏 API Key' : '显示 API Key'" @click="revealApiKey = !revealApiKey">
                    {{ revealApiKey ? '隐藏' : '显示' }}
                  </button>
                </span>
                <small>页面刷新或关闭后会清空输入框</small>
              </label>
            </div>
            <div class="credential-actions">
              <button class="inline-button" type="button" :disabled="credentialBusy" @click="refreshCredentialStatus">检查保存状态</button>
              <button
                class="inline-button danger-link"
                type="button"
                :disabled="credentialBusy || provider?.enabled"
                @click="removeCredential"
              >
                {{ credentialDeleteArmed ? '再次点击确认删除' : '删除已保存 Key' }}
              </button>
              <small v-if="provider?.enabled">先禁用 Provider，才能删除其 API Key。</small>
            </div>
          </template>
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
            <label v-if="form.kind === 'openai_compatible_chat'" class="field-control">
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
        <button class="text-button" type="button" :disabled="busy" @click="close">取消</button>
        <button class="primary-button" type="submit" :disabled="busy">{{ submitLabel }}</button>
      </footer>
    </form>
  </dialog>
</template>
