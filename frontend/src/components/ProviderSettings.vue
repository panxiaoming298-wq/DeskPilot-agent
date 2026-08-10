<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useProviderManagement } from '../composables/useProviderManagement'
import type {
  ModelProviderRoutingSnapshot,
  ModelRole,
  ProviderCatalogEntry,
  ProviderConfig,
  ProviderConfigAuditEvent,
} from '../types'
import ProviderEditorModal from './ProviderEditorModal.vue'

const {
  catalog,
  routing,
  etag,
  providers,
  enabledCount,
  localCount,
  auditEvents,
  loading,
  auditLoading,
  routingLoading,
  activeMutation,
  healthChecking,
  loadError,
  operationError,
  conflictMessage,
  successMessage,
  initialize,
  refreshCatalog,
  refreshAudit,
  refreshRouting,
  createProvider,
  updateProvider,
  setProviderEnabled,
  makeProviderDefault,
  deleteProvider,
  probeHealth,
  dismissMessages,
} = useProviderManagement()

const editorOpen = ref(false)
const editorMode = ref<'create' | 'edit'>('create')
const selectedProvider = ref<ProviderCatalogEntry | null>(null)
const pendingDelete = ref<string | null>(null)

const cloudCount = computed(() => providers.value.length - localCount.value)
const healthyCount = computed(
  () => providers.value.filter((entry) => entry.cached_health?.status === 'ready').length,
)
const openCircuitCount = computed(
  () => routing.value?.providers.filter((provider) => provider.circuit_state !== 'closed').length ?? 0,
)
const totalRetryCount = computed(
  () => routing.value?.providers.reduce((total, provider) => total + provider.retry_count, 0) ?? 0,
)

onMounted(() => void initialize())

async function refreshControlPlane(): Promise<void> {
  await Promise.all([refreshCatalog(), refreshRouting()])
}

function openCreate(): void {
  dismissMessages()
  editorMode.value = 'create'
  selectedProvider.value = null
  editorOpen.value = true
}

function openEdit(provider: ProviderCatalogEntry): void {
  dismissMessages()
  editorMode.value = 'edit'
  selectedProvider.value = provider
  editorOpen.value = true
}

function closeEditor(): void {
  editorOpen.value = false
  selectedProvider.value = null
}

async function saveProvider(config: ProviderConfig): Promise<void> {
  const success = editorMode.value === 'create'
    ? await createProvider(config)
    : await updateProvider(config.provider_id, config)
  if (success) closeEditor()
}

async function confirmDelete(providerId: string): Promise<void> {
  const success = await deleteProvider(providerId)
  if (success) pendingDelete.value = null
}

function protocolLabel(protocol: ProviderCatalogEntry['descriptor']['protocol']): string {
  const labels: Record<ProviderCatalogEntry['descriptor']['protocol'], string> = {
    fake: 'Fake',
    openai_compatible_chat: 'OpenAI Chat',
    openai_responses: 'OpenAI Responses',
    ollama: 'Ollama',
  }
  return labels[protocol]
}

function healthLabel(provider: ProviderCatalogEntry): string {
  if (!provider.enabled) return '已禁用'
  const status = provider.cached_health?.status
  if (status === 'ready') return '运行正常'
  if (status === 'degraded') return '性能下降'
  if (status === 'unavailable') return '不可用'
  return '尚未检查'
}

function healthTime(provider: ProviderCatalogEntry): string {
  if (!provider.cached_health) return '点击检查后生成短期缓存'
  const time = formatDate(provider.cached_health.checked_at)
  const latency = provider.cached_health.latency_ms
  return latency === null ? time : `${time} · ${latency} ms`
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(value))
}

function actionLabel(action: ProviderConfigAuditEvent['action']): string {
  const labels: Record<ProviderConfigAuditEvent['action'], string> = {
    created: '创建配置',
    updated: '重新配置',
    enabled: '启用',
    disabled: '禁用',
    default_changed: '设为默认',
    deleted: '删除配置',
  }
  return labels[action]
}

function fieldLabel(field: string): string {
  const labels: Record<string, string> = {
    provider_id: 'Provider ID',
    display_name: '显示名称',
    model: '模型',
    base_url: '连接地址',
    credential_ref: '凭据引用',
    enabled: '启用状态',
    default_provider_id: '默认 Provider',
    capabilities: '能力声明',
  }
  return labels[field] ?? field
}

function routeRoleLabel(role: ModelRole): string {
  const labels: Record<ModelRole, string> = {
    intent: '意图识别',
    planner: '规划器',
    tool_agent: '工具 Agent',
    summarizer: '总结器',
    verifier: '验证器',
  }
  return labels[role]
}

function runtimeFor(providerId: string): ModelProviderRoutingSnapshot | null {
  return routing.value?.providers.find((provider) => provider.provider_id === providerId) ?? null
}

function circuitLabel(state: ModelProviderRoutingSnapshot['circuit_state']): string {
  return { closed: '闭合', open: '已熔断', half_open: '半开探测' }[state]
}

function formatCost(micros: number | null): string {
  if (micros === null) return '不限'
  return `$${(micros / 1_000_000).toFixed(6)}`
}
</script>

<template>
  <section class="provider-settings" aria-labelledby="provider-settings-title">
    <div class="settings-hero">
      <div>
        <span class="eyebrow">PROVIDER CONTROL PLANE</span>
        <h2 id="provider-settings-title">模型连接与运行状态</h2>
        <p>统一管理本地与云端模型连接，并观察角色路由、费用/重试预算、延迟 EWMA 和 Provider 熔断状态。</p>
      </div>
      <div class="settings-actions">
        <button class="text-button" type="button" :disabled="loading || routingLoading || !!activeMutation" @click="refreshControlPlane">
          {{ loading || routingLoading ? '刷新中…' : '刷新控制面' }}
        </button>
        <button class="primary-button" type="button" :disabled="!!activeMutation" @click="openCreate">添加 Provider</button>
      </div>
    </div>

    <div v-if="successMessage || conflictMessage || operationError" class="notice-stack" aria-live="polite">
      <div v-if="successMessage" class="notice success-notice"><span>{{ successMessage }}</span><button type="button" aria-label="关闭提示" @click="dismissMessages">×</button></div>
      <div v-if="conflictMessage" class="notice conflict-notice"><span>{{ conflictMessage }}</span><button type="button" aria-label="关闭提示" @click="dismissMessages">×</button></div>
      <div v-if="operationError" class="notice error-notice" role="alert"><span>{{ operationError }}</span><button type="button" aria-label="关闭提示" @click="dismissMessages">×</button></div>
    </div>

    <div class="provider-metrics" aria-label="Provider 概览">
      <article><span>总连接数</span><strong>{{ providers.length }}</strong><small>上限 32 个</small></article>
      <article><span>当前启用</span><strong>{{ enabledCount }}</strong><small>{{ providers.length - enabledCount }} 个已禁用</small></article>
      <article><span>部署位置</span><strong>{{ localCount }} / {{ cloudCount }}</strong><small>本地 / 云端</small></article>
      <article><span>健康缓存</span><strong>{{ healthyCount }}</strong><small>状态正常</small></article>
      <article><span>韧性状态</span><strong>{{ openCircuitCount }} / {{ totalRetryCount }}</strong><small>非闭合熔断 / 累计重试</small></article>
    </div>

    <div class="settings-layout">
      <section class="provider-list-panel" aria-labelledby="provider-list-title">
        <div class="panel-heading">
          <div>
            <span class="eyebrow">ACTIVE CATALOG</span>
            <h2 id="provider-list-title">Provider 目录</h2>
          </div>
          <div class="catalog-version">
            <span>配置版本</span>
            <code>v{{ catalog?.catalog_version ?? '—' }}</code>
          </div>
        </div>

        <div v-if="loading && !catalog" class="settings-loading">正在建立安全会话并读取配置…</div>
        <div v-else-if="loadError" class="settings-empty error-empty">
          <strong>目录暂时不可用</strong>
          <p>{{ loadError }}</p>
          <button class="text-button" type="button" @click="initialize">重试</button>
        </div>
        <div v-else-if="!providers.length" class="settings-empty">
          <strong>还没有 Provider</strong>
          <p>添加 Fake Provider 或 OpenAI-compatible 连接开始配置。</p>
        </div>
        <div v-else class="provider-cards">
          <article v-for="provider in providers" :key="provider.descriptor.provider_id" class="provider-card" :class="{ disabled: !provider.enabled }">
            <div class="provider-card-heading">
              <div class="provider-identity">
                <span class="provider-avatar" :data-location="provider.descriptor.location">{{ provider.descriptor.location === 'local' ? 'L' : 'C' }}</span>
                <div>
                  <div class="provider-name-line">
                    <h3>{{ provider.descriptor.display_name }}</h3>
                    <span v-if="provider.is_default" class="default-badge">默认</span>
                  </div>
                  <p><code>{{ provider.descriptor.provider_id }}</code> · {{ protocolLabel(provider.descriptor.protocol) }}</p>
                </div>
              </div>
              <span class="enabled-badge" :data-enabled="provider.enabled">{{ provider.enabled ? 'Enabled' : 'Disabled' }}</span>
            </div>

            <div class="provider-model-row">
              <div><span>模型</span><strong>{{ provider.descriptor.model }}</strong></div>
              <div><span>位置</span><strong>{{ provider.descriptor.location === 'local' ? '本地设备' : '云端服务' }}</strong></div>
              <div><span>上下文</span><strong>{{ provider.descriptor.capabilities.max_context_tokens.toLocaleString() }}</strong></div>
            </div>

            <div class="capability-list" aria-label="模型能力">
              <span v-if="provider.descriptor.capabilities.streaming">Streaming</span>
              <span v-if="provider.descriptor.capabilities.structured_output">Structured</span>
              <span v-if="provider.descriptor.capabilities.strict_json_schema">Strict schema</span>
              <span v-if="provider.descriptor.capabilities.tool_calling !== 'none'">Tool calling</span>
              <span v-if="!provider.descriptor.capabilities.streaming && !provider.descriptor.capabilities.structured_output">基础文本</span>
            </div>

            <div v-if="runtimeFor(provider.descriptor.provider_id)" class="provider-runtime-strip">
              <span :data-circuit="runtimeFor(provider.descriptor.provider_id)?.circuit_state">
                {{ circuitLabel(runtimeFor(provider.descriptor.provider_id)!.circuit_state) }}
              </span>
              <span>EWMA {{ runtimeFor(provider.descriptor.provider_id)?.latency_ewma_ms?.toFixed(1) ?? '—' }} ms</span>
              <span>重试 {{ runtimeFor(provider.descriptor.provider_id)?.retry_count ?? 0 }}</span>
              <span>费用 {{ formatCost(runtimeFor(provider.descriptor.provider_id)?.total_cost_micros ?? 0) }}</span>
            </div>

            <div class="health-row" :data-status="provider.cached_health?.status ?? (provider.enabled ? 'unknown' : 'disabled')">
              <span class="health-dot" />
              <div><strong>{{ healthLabel(provider) }}</strong><small>{{ healthTime(provider) }}</small></div>
              <button class="inline-button" type="button" :disabled="!provider.enabled || !!activeMutation || !!healthChecking" @click="probeHealth(provider.descriptor.provider_id)">
                {{ healthChecking === provider.descriptor.provider_id ? '检查中…' : '健康检查' }}
              </button>
            </div>

            <div class="provider-card-actions">
              <button class="inline-button" type="button" :disabled="!!activeMutation" @click="openEdit(provider)">重新配置</button>
              <button v-if="!provider.is_default" class="inline-button" type="button" :disabled="!provider.enabled || !!activeMutation" @click="makeProviderDefault(provider.descriptor.provider_id)">设为默认</button>
              <button v-if="!provider.is_default" class="inline-button" type="button" :disabled="!!activeMutation" @click="setProviderEnabled(provider.descriptor.provider_id, !provider.enabled)">
                {{ provider.enabled ? '禁用' : '启用' }}
              </button>
              <button v-if="!provider.is_default" class="inline-button danger-link" type="button" :disabled="!!activeMutation" @click="pendingDelete = provider.descriptor.provider_id">删除</button>
            </div>

            <div v-if="pendingDelete === provider.descriptor.provider_id" class="delete-confirm" role="alertdialog" aria-label="确认删除 Provider">
              <p><strong>删除 {{ provider.descriptor.display_name }}？</strong>关联凭据不会被删除，可供后续重新绑定。</p>
              <div>
                <button class="text-button" type="button" :disabled="!!activeMutation" @click="pendingDelete = null">取消</button>
                <button class="danger-button" type="button" :disabled="!!activeMutation" @click="confirmDelete(provider.descriptor.provider_id)">{{ activeMutation ? '处理中…' : '确认删除' }}</button>
              </div>
            </div>
          </article>
        </div>
      </section>

      <aside class="provider-side-column">
        <section class="routing-panel" aria-labelledby="routing-title">
          <div class="panel-heading compact-heading">
            <div><span class="eyebrow">ROUTING & RESILIENCE</span><h2 id="routing-title">角色调度策略</h2></div>
            <button class="icon-button" type="button" aria-label="刷新调度状态" :disabled="routingLoading" @click="refreshRouting">↻</button>
          </div>
          <div v-if="routingLoading && !routing" class="routing-empty">正在读取调度状态…</div>
          <template v-else-if="routing">
            <div class="resilience-summary">
              <div><span>最大尝试</span><strong>{{ routing.default_max_attempts }}</strong></div>
              <div><span>重试等待预算</span><strong>{{ routing.default_retry_delay_budget_seconds }}s</strong></div>
              <div><span>任务费用上限</span><strong>{{ formatCost(routing.default_task_cost_budget_micros) }}</strong></div>
              <div><span>熔断阈值</span><strong>{{ routing.circuit_failure_threshold }}</strong></div>
            </div>
            <ol class="role-route-list">
              <li v-for="route in routing.routes" :key="route.role">
                <div><strong>{{ routeRoleLabel(route.role) }}</strong><span>{{ route.strategy === 'latency_aware' ? '延迟择优' : '固定优先级' }}</span></div>
                <p>
                  <code v-for="providerId in route.provider_ids" :key="providerId">{{ providerId }}</code>
                </p>
              </li>
            </ol>
          </template>
          <div v-else class="routing-empty">调度状态暂不可用。</div>
        </section>

        <section class="credential-guide">
          <span class="eyebrow">SECRET BOUNDARY</span>
          <h2>先保存密钥，再引用</h2>
          <p>页面不会接收、显示或保存 API Key。推荐先通过本地 CLI 将密钥写入当前用户的 Windows 凭据管理器。</p>
          <pre><code>.\.venv\Scripts\python.exe -m deskpilot.credential_cli store CLOUD_CHAT</code></pre>
          <small>在 backend 目录执行；表单中的凭据标识符填写 <code>CLOUD_CHAT</code>。</small>
        </section>

        <section class="audit-panel" aria-labelledby="audit-title">
          <div class="panel-heading compact-heading">
            <div><span class="eyebrow">AUDIT TRAIL</span><h2 id="audit-title">配置时间线</h2></div>
            <button class="icon-button" type="button" aria-label="刷新审计记录" :disabled="auditLoading" @click="refreshAudit">↻</button>
          </div>
          <div v-if="auditLoading && !auditEvents.length" class="audit-empty">正在读取审计记录…</div>
          <ol v-else-if="auditEvents.length" class="audit-list">
            <li v-for="event in auditEvents" :key="event.event_id">
              <span class="audit-marker" :data-action="event.action" />
              <div>
                <div class="audit-event-heading"><strong>{{ actionLabel(event.action) }}</strong><time :datetime="event.occurred_at">{{ formatDate(event.occurred_at) }}</time></div>
                <p><code>{{ event.provider_id }}</code> · revision {{ event.config_revision }}</p>
                <div v-if="event.changed_fields.length" class="audit-fields">
                  <span v-for="field in event.changed_fields" :key="field">{{ fieldLabel(field) }}</span>
                </div>
              </div>
            </li>
          </ol>
          <div v-else class="audit-empty">还没有配置变更记录。</div>
        </section>

        <div class="etag-note"><span>当前并发条件</span><code>{{ etag ?? '等待目录加载' }}</code></div>
      </aside>
    </div>

    <ProviderEditorModal
      :open="editorOpen"
      :mode="editorMode"
      :provider="selectedProvider"
      :submitting="!!activeMutation"
      :server-error="operationError ?? conflictMessage"
      @close="closeEditor"
      @save="saveProvider"
    />
  </section>
</template>
