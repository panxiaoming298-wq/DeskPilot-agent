import { computed, onUnmounted, ref } from 'vue'
import {
  ApiProblemError,
  checkProviderHealth,
  createIdempotencyKey,
  createProvider as createProviderRequest,
  deleteProvider as deleteProviderRequest,
  getProviderAudit,
  getProviderCatalog,
  getProviderRouting,
  makeProviderDefault as makeProviderDefaultRequest,
  setProviderEnabled as setProviderEnabledRequest,
  updateProvider as updateProviderRequest,
  type ProviderMutationResponse,
} from '../api'
import type {
  ProviderCatalogSnapshot,
  ProviderConfig,
  ProviderConfigAuditEvent,
  ProviderHealthSnapshot,
  ModelGatewayRoutingSnapshot,
} from '../types'

type MutationOperation = (
  etag: string,
  idempotencyKey: string,
) => Promise<ProviderMutationResponse>

const ACTION_LABELS: Record<string, string> = {
  created: 'Provider 已创建',
  updated: 'Provider 已重新配置',
  enabled: 'Provider 已启用',
  disabled: 'Provider 已禁用',
  default_changed: '默认 Provider 已切换',
  deleted: 'Provider 已删除，关联凭据仍保留',
}

export function useProviderManagement() {
  const catalog = ref<ProviderCatalogSnapshot | null>(null)
  const routing = ref<ModelGatewayRoutingSnapshot | null>(null)
  const etag = ref<string | null>(null)
  const auditEvents = ref<ProviderConfigAuditEvent[]>([])
  const loading = ref(false)
  const auditLoading = ref(false)
  const routingLoading = ref(false)
  const activeMutation = ref<string | null>(null)
  const healthChecking = ref<string | null>(null)
  const loadError = ref<string | null>(null)
  const operationError = ref<string | null>(null)
  const conflictMessage = ref<string | null>(null)
  const successMessage = ref<string | null>(null)

  let messageTimer: number | null = null

  const providers = computed(() => catalog.value?.providers ?? [])
  const enabledCount = computed(() => providers.value.filter((entry) => entry.enabled).length)
  const localCount = computed(
    () => providers.value.filter((entry) => entry.descriptor.location === 'local').length,
  )

  async function refreshCatalog(silent = false): Promise<void> {
    if (!silent) loading.value = true
    loadError.value = null
    try {
      const response = await getProviderCatalog()
      catalog.value = response.snapshot
      etag.value = response.etag
    } catch (error) {
      loadError.value = readableError(error, '无法读取 Provider Catalog')
    } finally {
      if (!silent) loading.value = false
    }
  }

  async function refreshAudit(): Promise<void> {
    auditLoading.value = true
    try {
      const page = await getProviderAudit()
      auditEvents.value = [...page.events].reverse()
    } catch (error) {
      operationError.value = readableError(error, '无法读取 Provider 审计记录')
    } finally {
      auditLoading.value = false
    }
  }

  async function refreshRouting(): Promise<void> {
    routingLoading.value = true
    try {
      routing.value = await getProviderRouting()
    } catch (error) {
      operationError.value = readableError(error, '无法读取 Provider 调度状态')
    } finally {
      routingLoading.value = false
    }
  }

  async function initialize(): Promise<void> {
    loading.value = true
    await Promise.all([refreshCatalog(true), refreshAudit(), refreshRouting()])
    loading.value = false
  }

  async function runMutation(label: string, operation: MutationOperation): Promise<boolean> {
    if (activeMutation.value) return false
    if (!etag.value) {
      await refreshCatalog()
      if (!etag.value) return false
    }

    activeMutation.value = label
    operationError.value = null
    conflictMessage.value = null
    const requestEtag = etag.value
    const idempotencyKey = createIdempotencyKey()

    try {
      const response = await executeWithNetworkRetry(operation, requestEtag, idempotencyKey)
      etag.value = response.etag
      showSuccess(ACTION_LABELS[response.result.action] ?? 'Provider 配置已更新')
      await Promise.all([refreshCatalog(true), refreshAudit(), refreshRouting()])
      return true
    } catch (error) {
      if (error instanceof ApiProblemError && error.status === 412) {
        await refreshCatalog(true)
        conflictMessage.value = '配置已被其他操作更新，页面已载入最新版本。请确认后重新提交。'
      } else {
        operationError.value = readableError(error, `${label}失败`)
      }
      return false
    } finally {
      activeMutation.value = null
    }
  }

  function createProvider(config: ProviderConfig): Promise<boolean> {
    return runMutation('创建 Provider', (currentEtag, key) =>
      createProviderRequest(config, currentEtag, key),
    )
  }

  function updateProvider(providerId: string, config: ProviderConfig): Promise<boolean> {
    return runMutation('重新配置 Provider', (currentEtag, key) =>
      updateProviderRequest(providerId, config, currentEtag, key),
    )
  }

  function setProviderEnabled(providerId: string, enabled: boolean): Promise<boolean> {
    return runMutation(enabled ? '启用 Provider' : '禁用 Provider', (currentEtag, key) =>
      setProviderEnabledRequest(providerId, enabled, currentEtag, key),
    )
  }

  function makeProviderDefault(providerId: string): Promise<boolean> {
    return runMutation('切换默认 Provider', (currentEtag, key) =>
      makeProviderDefaultRequest(providerId, currentEtag, key),
    )
  }

  function deleteProvider(providerId: string): Promise<boolean> {
    return runMutation('删除 Provider', (currentEtag, key) =>
      deleteProviderRequest(providerId, currentEtag, key),
    )
  }

  async function probeHealth(providerId: string): Promise<void> {
    if (healthChecking.value || activeMutation.value) return
    healthChecking.value = providerId
    operationError.value = null
    try {
      const health = await checkProviderHealth(providerId)
      replaceHealth(health)
    } catch (error) {
      operationError.value = readableError(error, '健康检查失败')
    } finally {
      healthChecking.value = null
    }
  }

  function replaceHealth(health: ProviderHealthSnapshot): void {
    if (!catalog.value) return
    catalog.value = {
      ...catalog.value,
      providers: catalog.value.providers.map((entry) =>
        entry.descriptor.provider_id === health.provider_id
          ? { ...entry, cached_health: health }
          : entry,
      ),
    }
  }

  function dismissMessages(): void {
    operationError.value = null
    conflictMessage.value = null
    successMessage.value = null
  }

  function showSuccess(message: string): void {
    successMessage.value = message
    if (messageTimer !== null) window.clearTimeout(messageTimer)
    messageTimer = window.setTimeout(() => {
      successMessage.value = null
      messageTimer = null
    }, 4_000)
  }

  onUnmounted(() => {
    if (messageTimer !== null) window.clearTimeout(messageTimer)
  })

  return {
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
  }
}

function readableError(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

async function executeWithNetworkRetry(
  operation: MutationOperation,
  etag: string,
  idempotencyKey: string,
): Promise<ProviderMutationResponse> {
  try {
    return await operation(etag, idempotencyKey)
  } catch (error) {
    if (!(error instanceof TypeError)) throw error
    return operation(etag, idempotencyKey)
  }
}
