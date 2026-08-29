<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { getProviderProbePreparation, prepareProviderProbe } from '../api'
import type {
  ProviderProbeCostControlMode,
  ProviderProbeFamily,
  ProviderProbePreparationCommand,
  ProviderProbePreparationManifest,
  ProviderProbePreparationProfile,
  ProviderProbePreparationResult,
} from '../types'

interface ProbeForm {
  baseUrl: string
  costControlMode: ProviderProbeCostControlMode
  exactModelConfirmed: boolean
  credentialPresenceConfirmed: boolean
  baseUrlKeyPairConfirmed: boolean
  providerHardLimitEnforcing: boolean
  dedicatedProbeCredentialConfirmed: boolean
  applicationBudgetEnvelopeConfirmed: boolean
  prepaidBalanceAvailableConfirmed: boolean
  billingAlertConfirmed: boolean
  billingDelayAcknowledged: boolean
  freeQuotaStopEnabled: boolean
  pricingSourceConfirmed: boolean
}

const manifest = ref<ProviderProbePreparationManifest | null>(null)
const loading = ref(true)
const preparing = ref(false)
const loadError = ref<string | null>(null)
const preparationError = ref<string | null>(null)
const attempted = ref(false)
const selectedFamily = ref<ProviderProbeFamily>('openai')
const results = reactive<Partial<Record<ProviderProbeFamily, ProviderProbePreparationResult>>>({})

function emptyForm(costControlMode: ProviderProbeCostControlMode): ProbeForm {
  return {
    baseUrl: '',
    costControlMode,
    exactModelConfirmed: false,
    credentialPresenceConfirmed: false,
    baseUrlKeyPairConfirmed: false,
    providerHardLimitEnforcing: false,
    dedicatedProbeCredentialConfirmed: false,
    applicationBudgetEnvelopeConfirmed: false,
    prepaidBalanceAvailableConfirmed: false,
    billingAlertConfirmed: false,
    billingDelayAcknowledged: false,
    freeQuotaStopEnabled: false,
    pricingSourceConfirmed: false,
  }
}

const forms = reactive<Record<ProviderProbeFamily, ProbeForm>>({
  openai: emptyForm('openai_application_envelope'),
  deepseek: emptyForm('deepseek_prepaid_balance'),
  bailian: emptyForm('bailian_billing_alert'),
})

const profile = computed<ProviderProbePreparationProfile | null>(() =>
  manifest.value?.profiles.find((item) => item.provider_family === selectedFamily.value) ?? null,
)
const form = computed(() => forms[selectedFamily.value])
const result = computed(() => results[selectedFamily.value] ?? null)

const requiredConfirmations = computed(() => {
  const current = form.value
  const items = [
    { checked: current.exactModelConfirmed, label: '模型名与本次冻结值完全一致' },
    { checked: current.credentialPresenceConfirmed, label: 'API Key 已安全保存到当前 Windows 用户的凭据管理器' },
    { checked: current.baseUrlKeyPairConfirmed, label: '连接地址与该 API Key 属于同一账户或工作空间' },
    { checked: current.dedicatedProbeCredentialConfirmed, label: '使用专用的小额验证凭据' },
    { checked: current.applicationBudgetEnvelopeConfirmed, label: '接受项目内的请求数和费用上限' },
    { checked: current.pricingSourceConfirmed, label: '今日已核对官方计费来源' },
  ]
  if (current.costControlMode === 'openai_project_hard_limit') {
    items.push({ checked: current.providerHardLimitEnforcing, label: 'OpenAI 项目硬限额已生效' })
  }
  if (selectedFamily.value === 'deepseek') {
    items.push({ checked: current.prepaidBalanceAvailableConfirmed, label: 'DeepSeek 预付费余额已于今日核对' })
  }
  if (selectedFamily.value === 'bailian') {
    items.push({ checked: current.billingAlertConfirmed, label: '百炼账单告警已配置' })
    items.push({ checked: current.billingDelayAcknowledged, label: '已知晓百炼账单可能延迟' })
  }
  return items
})

const missingConfirmations = computed(() => {
  const missing = requiredConfirmations.value
    .filter((item) => !item.checked)
    .map((item) => item.label)
  if (!form.value.baseUrl.trim()) missing.unshift('填写连接地址')
  return missing
})

const violationLabels: Record<string, string> = {
  POLICY_DIGEST_MISMATCH: '本地策略版本已变更',
  BINDING_NOT_CURRENT: '准备材料已过期',
  PRICING_CONFIRMATION_STALE: '计费确认已过期',
  EXACT_MODEL_NOT_CONFIRMED: '模型名未确认',
  CREDENTIAL_PRESENCE_NOT_CONFIRMED: '凭据存在性未确认',
  BASE_URL_KEY_PAIR_NOT_CONFIRMED: '连接地址与凭据关系未确认',
  MODEL_NOT_ALLOWED: '模型名不在冻结策略内',
  BASE_URL_NOT_ALLOWED: '连接地址不符合当前冻结策略',
  CREDENTIAL_REFERENCE_NOT_ALLOWED: '凭据引用名不符合策略',
  BUDGET_POLICY_MISMATCH: '费用或请求上限与策略不一致',
  PUBLIC_PROVIDER_CONFIG_INVALID: '公开 Provider 配置无效',
  COST_CONTROL_MODE_NOT_ALLOWED: '费用控制方式不在允许范围内',
  DEDICATED_PROBE_CREDENTIAL_NOT_CONFIRMED: '专用验证凭据未确认',
  APPLICATION_BUDGET_ENVELOPE_NOT_CONFIRMED: '应用内费用上限未确认',
  PROVIDER_HARD_LIMIT_NOT_ENFORCING: 'Provider 硬限额未生效',
  PREPAID_BALANCE_NOT_CURRENT: '预付费余额未实时确认',
  BILLING_ALERT_EVIDENCE_MISMATCH: '账单告警确认不完整',
  BILLING_DELAY_EVIDENCE_MISMATCH: '账单延迟确认不完整',
}

onMounted(() => void loadManifest())

async function loadManifest(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const loaded = await getProviderProbePreparation()
    manifest.value = loaded
    for (const item of loaded.profiles) {
      forms[item.provider_family].baseUrl = item.suggested_base_url ?? ''
      forms[item.provider_family].costControlMode = item.allowed_cost_control_modes.at(-1)!
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '无法读取离线准备策略。'
  } finally {
    loading.value = false
  }
}

function selectFamily(family: ProviderProbeFamily): void {
  selectedFamily.value = family
  attempted.value = false
  preparationError.value = null
}

function buildCommand(currentProfile: ProviderProbePreparationProfile): ProviderProbePreparationCommand {
  const current = form.value
  return {
    provider_family: currentProfile.provider_family,
    provider_id: currentProfile.provider_id,
    exact_model: currentProfile.exact_model,
    base_url: current.baseUrl.trim(),
    credential_identifier: currentProfile.credential_identifier,
    cost_control_mode: current.costControlMode,
    exact_model_confirmed: current.exactModelConfirmed,
    credential_presence_confirmed: current.credentialPresenceConfirmed,
    base_url_key_pair_confirmed: current.baseUrlKeyPairConfirmed,
    provider_hard_limit_enforcing: current.providerHardLimitEnforcing,
    dedicated_probe_credential_confirmed: current.dedicatedProbeCredentialConfirmed,
    application_budget_envelope_confirmed: current.applicationBudgetEnvelopeConfirmed,
    prepaid_balance_available_confirmed: current.prepaidBalanceAvailableConfirmed,
    billing_alert_confirmed: current.billingAlertConfirmed,
    billing_delay_acknowledged: current.billingDelayAcknowledged,
    free_quota_stop_enabled: current.freeQuotaStopEnabled,
    pricing_source_confirmed: true,
  }
}

async function runPreflight(): Promise<void> {
  attempted.value = true
  preparationError.value = null
  if (!profile.value || missingConfirmations.value.length) return
  preparing.value = true
  try {
    results[selectedFamily.value] = await prepareProviderProbe(buildCommand(profile.value))
  } catch (error) {
    preparationError.value = error instanceof Error ? error.message : '无法完成无网检查。'
  } finally {
    preparing.value = false
  }
}

function exportBinding(): void {
  if (!result.value) return
  const payload = JSON.stringify(result.value.binding, null, 2)
  const url = URL.createObjectURL(new Blob([payload], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `deskpilot-${selectedFamily.value}-probe-binding.json`
  link.click()
  URL.revokeObjectURL(url)
}

function formatMoney(microunits: number, currency: 'USD' | 'CNY'): string {
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(microunits / 1_000_000)
}

function costModeLabel(mode: ProviderProbeCostControlMode): string {
  return {
    openai_project_hard_limit: 'OpenAI 项目硬限额',
    openai_application_envelope: '应用内小额预算上限',
    deepseek_prepaid_balance: 'DeepSeek 预付费余额',
    bailian_billing_alert: '百炼账单告警',
  }[mode]
}

function violationLabel(code: string): string {
  return violationLabels[code] ?? code
}
</script>

<template>
  <section class="probe-preparation" aria-labelledby="probe-preparation-title">
    <header class="probe-heading">
      <div>
        <span class="probe-kicker">116C-A · 离线准备</span>
        <h2 id="probe-preparation-title">Provider 验证准备</h2>
        <p>生成 24 小时有效的公开配置 binding，只做本机无网检查。不读取密钥，不发起模型请求。</p>
      </div>
      <div class="boundary" aria-label="当前边界">
        <span>网络关闭</span>
        <span>真实 capture 关闭</span>
        <span>Production Admission 关闭</span>
      </div>
    </header>

    <div v-if="loading" class="probe-state" aria-live="polite">正在读取冻结策略…</div>
    <div v-else-if="loadError" class="probe-state error-state" role="alert">
      <strong>离线策略暂不可用</strong>
      <span>{{ loadError }}</span>
      <button type="button" @click="loadManifest">重试</button>
    </div>

    <template v-else-if="manifest && profile">
      <div class="provider-tabs" role="tablist" aria-label="选择 Provider">
        <button
          v-for="item in manifest.profiles"
          :key="item.provider_family"
          type="button"
          role="tab"
          :aria-selected="selectedFamily === item.provider_family"
          :class="{ active: selectedFamily === item.provider_family }"
          @click="selectFamily(item.provider_family)"
        >
          <strong>{{ item.display_name }}</strong>
          <small>{{ item.exact_model }}</small>
        </button>
      </div>

      <div class="probe-workspace">
        <div class="probe-config">
          <dl class="frozen-facts">
            <div><dt>冻结模型</dt><dd><code>{{ profile.exact_model }}</code></dd></div>
            <div><dt>Windows 凭据引用</dt><dd><code>{{ profile.credential_identifier }}</code></dd></div>
            <div><dt>计划请求</dt><dd>{{ manifest.planned_requests_per_provider }} 次，0 次自动重试</dd></div>
            <div><dt>本次预算包络</dt><dd>{{ formatMoney(profile.planned_budget_envelope_microunits, profile.currency) }}</dd></div>
          </dl>

          <label class="field-label">
            <span>连接地址</span>
            <input v-model="form.baseUrl" type="url" :readonly="!profile.base_url_editable" autocomplete="off">
            <small v-if="profile.base_url_editable">填写百炼北京地域 Workspace 的 Responses 兼容地址，不支持 Coding Plan 地址。</small>
            <small v-else>该地址由冻结策略固定。</small>
          </label>

          <fieldset v-if="profile.allowed_cost_control_modes.length > 1" class="cost-modes">
            <legend>费用控制</legend>
            <label v-for="mode in profile.allowed_cost_control_modes" :key="mode">
              <input v-model="form.costControlMode" type="radio" :value="mode">
              <span>{{ costModeLabel(mode) }}</span>
            </label>
          </fieldset>
          <div v-else class="fixed-control"><span>费用控制</span><strong>{{ costModeLabel(form.costControlMode) }}</strong></div>

          <fieldset class="confirmation-list">
            <legend>运行前人工确认</legend>
            <label><input v-model="form.exactModelConfirmed" type="checkbox"><span>模型名与上方冻结值完全一致</span></label>
            <label><input v-model="form.credentialPresenceConfirmed" type="checkbox"><span>API Key 已通过 Provider 编辑器保存到 Windows 凭据管理器</span></label>
            <label><input v-model="form.baseUrlKeyPairConfirmed" type="checkbox"><span>连接地址与 API Key 属于同一账户或工作空间</span></label>
            <label><input v-model="form.dedicatedProbeCredentialConfirmed" type="checkbox"><span>使用专用的小额验证凭据</span></label>
            <label><input v-model="form.applicationBudgetEnvelopeConfirmed" type="checkbox"><span>接受 {{ profile.maximum_requests }} 次上限和 {{ formatMoney(profile.maximum_total_microunits, profile.currency) }} 最大总费用</span></label>
            <label><input v-model="form.pricingSourceConfirmed" type="checkbox"><span>今日已核对官方计费来源</span></label>
            <label v-if="form.costControlMode === 'openai_project_hard_limit'"><input v-model="form.providerHardLimitEnforcing" type="checkbox"><span>OpenAI 项目硬限额已生效</span></label>
            <label v-if="profile.prepaid_balance_check_required"><input v-model="form.prepaidBalanceAvailableConfirmed" type="checkbox"><span>DeepSeek 预付费余额已于今日核对</span></label>
            <label v-if="profile.billing_alert_required"><input v-model="form.billingAlertConfirmed" type="checkbox"><span>百炼账单告警已配置</span></label>
            <label v-if="profile.billing_delay_acknowledgement_required"><input v-model="form.billingDelayAcknowledged" type="checkbox"><span>已知晓百炼账单数据可能延迟</span></label>
            <label v-if="profile.free_quota_stop_recommended" class="optional-confirmation"><input v-model="form.freeQuotaStopEnabled" type="checkbox"><span>如控制台可用，免费额度用完即停（建议）</span></label>
          </fieldset>
        </div>

        <aside class="preflight-panel" aria-live="polite">
          <div class="preflight-title">
            <span>无网检查</span>
            <code>{{ manifest.policy_digest.slice(0, 12) }}</code>
          </div>

          <div v-if="result" class="readiness-result" :data-ready="result.readiness.ready">
            <strong>{{ result.readiness.ready ? '准备材料已通过' : '准备材料被阻止' }}</strong>
            <p v-if="result.readiness.ready">仅表示公开配置与费用边界一致，不代表模型可访问或准许联网。</p>
            <ul v-else><li v-for="code in result.readiness.violations" :key="code">{{ violationLabel(code) }}</li></ul>
            <dl>
              <div><dt>凭据已读取</dt><dd>否</dd></div>
              <div><dt>网络已访问</dt><dd>否</dd></div>
              <div><dt>实时许可已创建</dt><dd>否</dd></div>
              <div><dt>Binding 有效至</dt><dd>{{ new Date(result.binding.valid_until).toLocaleString('zh-CN') }}</dd></div>
            </dl>
          </div>
          <div v-else class="preflight-empty">
            <strong>尚未生成准备材料</strong>
            <p>完成左侧确认后，检查固定模型、地址、凭据引用和费用上限。</p>
          </div>

          <div v-if="attempted && missingConfirmations.length" class="blocker-list" role="alert">
            <strong>还有 {{ missingConfirmations.length }} 项未完成</strong>
            <ul><li v-for="item in missingConfirmations" :key="item">{{ item }}</li></ul>
          </div>
          <p v-if="preparationError" class="preparation-error" role="alert">{{ preparationError }}</p>

          <div class="preflight-actions">
            <button class="run-button" type="button" :disabled="preparing" @click="runPreflight">
              {{ preparing ? '检查中…' : '运行无网检查' }}
            </button>
            <button type="button" :disabled="!result" @click="exportBinding">导出 binding JSON</button>
          </div>
          <small class="boundary-note">此处没有“运行真实模型”入口。下一阶段仍需要单独授权。</small>
        </aside>
      </div>
    </template>
  </section>
</template>

<style scoped>
/* finesse · component=provider-probe-preparation · register=product-workflow · states=loading+error+empty+ready+blocked */
.probe-preparation {
  margin-top: 12px;
  padding: 18px;
  border: 1px solid var(--line);
  border-radius: 10px;
  color: var(--ink);
  background: var(--surface);
}

.probe-heading,
.preflight-title,
.preflight-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.probe-heading h2 { margin: 4px 0 7px; font-size: 17px; }
.probe-heading p { max-width: 720px; margin: 0; color: var(--muted-ink); font-size: 12px; line-height: 1.65; }
.probe-kicker { color: var(--quiet-ink); font-size: 9px; letter-spacing: 0.04em; }
.boundary { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 5px; }
.boundary span { padding: 5px 7px; border: 1px solid var(--line); border-radius: 5px; color: var(--quiet-ink); background: var(--task-card); font-size: 9px; }

.provider-tabs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  margin-top: 16px;
  padding: 5px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--page);
}

.provider-tabs button {
  min-height: 48px;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--muted-ink);
  background: transparent;
  text-align: left;
}

.provider-tabs button:hover { background: var(--surface-raised); }
.provider-tabs button.active { border-color: var(--line-strong); color: var(--ink); background: var(--surface-hot); }
.provider-tabs strong,
.provider-tabs small { display: block; }
.provider-tabs strong { font-size: 12px; }
.provider-tabs small { margin-top: 3px; color: var(--quiet-ink); font-family: var(--font-mono); font-size: 9px; }

.probe-workspace { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(290px, 0.65fr); gap: 12px; margin-top: 12px; }
.probe-config,
.preflight-panel { min-width: 0; padding: 15px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface); }
.frozen-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; margin: 0 0 15px; overflow: hidden; border: 1px solid var(--line); border-radius: 7px; background: var(--line); }
.frozen-facts div { min-width: 0; padding: 10px; background: var(--surface-raised); }
.frozen-facts dt { color: var(--quiet-ink); font-size: 9px; }
.frozen-facts dd { margin: 5px 0 0; color: var(--ink); font-size: 11px; }
.frozen-facts code { overflow-wrap: anywhere; color: var(--accent); font-family: var(--font-mono); }

.field-label { display: grid; gap: 6px; color: var(--muted-ink); font-size: 11px; }
.field-label input { width: 100%; min-height: 44px; padding: 9px 11px; border: 1px solid var(--line-strong); border-radius: 7px; color: var(--ink); background: var(--page); }
.field-label input[readonly] { color: var(--quiet-ink); background: var(--task-card); }
.field-label small { color: var(--quiet-ink); font-size: 9px; line-height: 1.55; }

fieldset { min-width: 0; margin: 14px 0 0; padding: 0; border: 0; }
legend,
.fixed-control > span { margin-bottom: 7px; color: var(--quiet-ink); font-size: 9px; }
.cost-modes { display: flex; flex-wrap: wrap; gap: 7px; }
.cost-modes legend { width: 100%; }
.cost-modes label,
.fixed-control { display: flex; align-items: center; gap: 8px; min-height: 40px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-ink); background: var(--surface-raised); font-size: 10px; }
.fixed-control { align-items: flex-start; flex-direction: column; margin-top: 14px; gap: 2px; }
.fixed-control > span { margin: 0; }
.fixed-control strong { font-size: 11px; }

.confirmation-list { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.confirmation-list legend { grid-column: 1 / -1; }
.confirmation-list label { display: flex; align-items: flex-start; gap: 8px; min-height: 44px; padding: 9px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-ink); background: var(--surface-raised); font-size: 10px; line-height: 1.45; }
.confirmation-list label:has(input:checked) { border-color: var(--line-hot); color: var(--accent-bright); background: var(--accent-soft); }
.confirmation-list input,
.cost-modes input { flex: 0 0 auto; width: 16px; height: 16px; margin: 1px 0 0; accent-color: var(--accent); }
.confirmation-list .optional-confirmation { color: var(--quiet-ink); }

.preflight-panel { display: flex; flex-direction: column; }
.preflight-title span { color: var(--ink); font-size: 12px; font-weight: 650; }
.preflight-title code { color: var(--quiet-ink); font-family: var(--font-mono); font-size: 9px; }
.preflight-empty,
.readiness-result { margin-top: 14px; padding: 13px; border: 1px solid var(--line); border-radius: 7px; background: var(--surface-raised); }
.preflight-empty strong,
.readiness-result > strong { font-size: 12px; }
.preflight-empty p,
.readiness-result p { margin: 7px 0 0; color: var(--quiet-ink); font-size: 10px; line-height: 1.6; }
.readiness-result[data-ready="true"] { border-color: color-mix(in srgb, var(--success) 30%, transparent); background: color-mix(in srgb, var(--success) 7%, transparent); }
.readiness-result[data-ready="false"] { border-color: color-mix(in srgb, var(--danger) 30%, transparent); background: color-mix(in srgb, var(--danger) 7%, transparent); }
.readiness-result ul,
.blocker-list ul { margin: 8px 0 0; padding-left: 17px; color: var(--danger); font-size: 10px; line-height: 1.55; }
.readiness-result dl { display: grid; gap: 6px; margin: 12px 0 0; }
.readiness-result dl div { display: flex; justify-content: space-between; gap: 8px; color: var(--quiet-ink); font-size: 9px; }
.readiness-result dd { margin: 0; color: var(--ink); text-align: right; }
.blocker-list { margin-top: 10px; padding: 11px; border: 1px solid color-mix(in srgb, var(--amber) 25%, transparent); border-radius: 7px; color: var(--amber); background: color-mix(in srgb, var(--amber) 6%, transparent); }
.blocker-list strong { font-size: 10px; }
.blocker-list ul { color: var(--amber); }
.preparation-error { margin: 10px 0 0; color: var(--danger); font-size: 10px; line-height: 1.5; }
.preflight-actions { margin-top: auto; padding-top: 14px; }
.preflight-actions button,
.probe-state button { min-height: 44px; padding: 9px 11px; border: 1px solid var(--line-strong); border-radius: 7px; color: var(--muted-ink); background: var(--surface-raised); }
.preflight-actions .run-button { color: var(--page); border-color: var(--ink); background: var(--ink); font-weight: 650; }
.preflight-actions button:disabled { cursor: not-allowed; opacity: 0.45; }
.boundary-note { margin-top: 10px; color: var(--quiet-ink); font-size: 9px; line-height: 1.55; }
.probe-state { display: grid; gap: 8px; margin-top: 14px; padding: 18px; border: 1px solid var(--line); border-radius: 8px; color: var(--muted-ink); background: var(--surface); font-size: 11px; }
.probe-state button { width: fit-content; }
.error-state { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 24%, transparent); }

button:focus-visible,
input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

@media (max-width: 900px) {
  .probe-workspace { grid-template-columns: 1fr; }
  .preflight-actions { margin-top: 0; }
}

@media (max-width: 600px) {
  .probe-preparation { padding: 13px; }
  .probe-heading { align-items: flex-start; flex-direction: column; }
  .boundary { justify-content: flex-start; }
  .provider-tabs { grid-template-columns: 1fr; }
  .frozen-facts,
  .confirmation-list { grid-template-columns: 1fr; }
  .preflight-actions { align-items: stretch; flex-direction: column; }
  .preflight-actions button { width: 100%; }
}
</style>
