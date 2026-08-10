<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type { Reconciliation, ReconciliationEvidenceKind } from '../types'

const props = withDefaults(
  defineProps<{
    reconciliation: Reconciliation | null
    loading: boolean
    refreshing: boolean
    compensating: boolean
    message: string | null
    error: string | null
  }>(),
  {
    reconciliation: null,
    loading: false,
    refreshing: false,
    compensating: false,
    message: null,
    error: null,
  },
)

const emit = defineEmits<{
  refresh: []
  compensate: []
}>()

const confirmCompensation = ref(false)
const latestEvidence = computed(() => props.reconciliation?.receipt_evidence[0] ?? null)
const receipt = computed(() => latestEvidence.value?.commit_receipt ?? null)
const committedEvidence = computed(() =>
  props.reconciliation?.receipt_evidence.find(
    (item) => item.kind === 'commit_receipt' && item.commit_receipt !== null,
  ) ?? null,
)
const evidenceTone = computed<ReconciliationEvidenceKind | 'loading'>(() =>
  latestEvidence.value?.kind ?? 'loading',
)
const evidenceLabel = computed(() => {
  const labels: Record<ReconciliationEvidenceKind, string> = {
    commit_receipt: '已发现提交回执',
    no_receipt: '当前未发现回执',
    query_failed: '查询失败',
  }
  return latestEvidence.value ? labels[latestEvidence.value.kind] : '等待 Runner 证据'
})

function formattedTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date)
}

function shortDigest(value: string): string {
  return value.length <= 24 ? value : `${value.slice(0, 13)}…${value.slice(-8)}`
}

function requestCompensation(): void {
  confirmCompensation.value = false
  emit('compensate')
}

watch(
  () => props.reconciliation?.reconciliation_id ?? null,
  () => {
    confirmCompensation.value = false
  },
)
</script>

<template>
  <section
    class="reconciliation-card"
    aria-labelledby="reconciliation-card-title"
    :aria-busy="loading || refreshing"
    :data-evidence="evidenceTone"
  >
    <div class="reconciliation-heading">
      <div>
        <span class="eyebrow">UNKNOWN RECONCILIATION</span>
        <h3 id="reconciliation-card-title">Runner 提交证据</h3>
      </div>
      <span class="reconciliation-status">{{ evidenceLabel }}</span>
    </div>

    <div v-if="loading && !reconciliation" class="reconciliation-loading" role="status">
      正在定位 unknown 调用并查询 Runner 持久化日志…
    </div>

    <template v-else-if="reconciliation">
      <div class="unknown-invariant">
        <strong>原始调用保持 unknown</strong>
        <p>
          自动证据不会改写工具账本，也不会自动创建新 attempt；最终结果仍需人工裁决。
        </p>
      </div>

      <dl class="reconciliation-facts">
        <div>
          <dt>工具</dt>
          <dd><code>{{ reconciliation.tool_name }}@{{ reconciliation.tool_version }}</code></dd>
        </div>
        <div>
          <dt>调用错误</dt>
          <dd><code>{{ reconciliation.call_error_code ?? '未记录' }}</code></dd>
        </div>
        <div>
          <dt>对账状态</dt>
          <dd>{{ reconciliation.status === 'pending' ? '等待人工裁决' : '已人工裁决' }}</dd>
        </div>
        <div>
          <dt>证据快照</dt>
          <dd>{{ reconciliation.receipt_evidence.length }} 条</dd>
        </div>
      </dl>

      <div v-if="latestEvidence" class="evidence-result" :data-kind="latestEvidence.kind">
        <template v-if="latestEvidence.kind === 'commit_receipt' && receipt">
          <strong>Runner 日志确认外部提交已越过提交边界。</strong>
          <p>这是“已提交”的正向证据；建议人工核对后裁决为 confirmed_succeeded。</p>
          <dl class="receipt-facts">
            <div><dt>Receipt</dt><dd><code :title="receipt.receipt_id">{{ shortDigest(receipt.receipt_id) }}</code></dd></div>
            <div><dt>Runner</dt><dd><code>{{ latestEvidence.queried_runner_id ?? '未记录' }}</code></dd></div>
            <div><dt>提交开始</dt><dd>{{ formattedTime(receipt.commit_started_at) }}</dd></div>
            <div><dt>日志落盘</dt><dd>{{ formattedTime(receipt.receipt_recorded_at) }}</dd></div>
          </dl>
          <div class="resource-version-grid">
            <div>
              <span>源文件</span>
              <code>{{ shortDigest(receipt.resource_versions_before.source ?? 'unknown') }}</code>
              <b>→</b>
              <code>{{ shortDigest(receipt.resource_versions_after.source ?? 'unknown') }}</code>
            </div>
            <div>
              <span>目标文件</span>
              <code>{{ shortDigest(receipt.resource_versions_before.destination ?? 'unknown') }}</code>
              <b>→</b>
              <code>{{ shortDigest(receipt.resource_versions_after.destination ?? 'unknown') }}</code>
            </div>
          </div>
        </template>
        <template v-else-if="latestEvidence.kind === 'no_receipt'">
          <strong>当前 Runner 日志中没有 committed receipt。</strong>
          <p>没有回执不等于“未生效”，因此不能据此安全重试或创建新 attempt。</p>
        </template>
        <template v-else>
          <strong>未能读取 Runner 提交日志。</strong>
          <p>错误码 <code>{{ latestEvidence.error_code ?? 'RUNNER_COMMIT_RECEIPT_QUERY_FAILED' }}</code>；该结果不能证明成功或无副作用。</p>
        </template>
        <small>
          观察于 {{ formattedTime(latestEvidence.observed_at) }}
          · {{ latestEvidence.queried_runner_id ?? 'Runner 不可用' }}
        </small>
      </div>

      <div v-else class="evidence-empty">
        尚未保存 Runner 查询证据。页面会自动采集一次，也可手动刷新。
      </div>

      <div v-if="committedEvidence" class="compensation-panel">
        <template v-if="reconciliation.compensation_task_id">
          <strong>反向任务已创建</strong>
          <p>
            任务 <code>{{ reconciliation.compensation_task_id }}</code> 已与该提交回执绑定，
            不会再创建第二个直接补偿。
          </p>
        </template>
        <template v-else-if="reconciliation.can_create_compensation">
          <strong>可创建显式反向任务</strong>
          <p>
            服务端将仅从原审批与 committed receipt 派生反向路径；
            创建不会立即移动文件，仍需在新审批卡中确认。
          </p>
          <div v-if="confirmCompensation" class="compensation-confirm" role="alert">
            <span>确认创建一个全新反向任务？精确路径将在下一张审批卡展示。</span>
            <button
              class="inline-button"
              data-testid="confirm-reconciliation-compensation"
              type="button"
              :disabled="loading || refreshing || compensating"
              @click="requestCompensation"
            >
              {{ compensating ? '正在创建…' : '确认创建' }}
            </button>
            <button
              class="inline-button secondary-button"
              type="button"
              :disabled="compensating"
              @click="confirmCompensation = false"
            >
              取消
            </button>
          </div>
          <button
            v-else
            class="inline-button compensation-button"
            data-testid="create-reconciliation-compensation"
            type="button"
            :disabled="loading || refreshing || compensating"
            @click="confirmCompensation = true"
          >
            创建反向任务
          </button>
        </template>
      </div>

      <button
        class="inline-button refresh-evidence-button"
        data-testid="refresh-reconciliation-evidence"
        type="button"
        :disabled="loading || refreshing || compensating"
        @click="emit('refresh')"
      >
        {{ refreshing ? '正在查询…' : '重新查询 Runner 日志' }}
      </button>
    </template>

    <div class="reconciliation-feedback" aria-live="polite" aria-atomic="true">
      <p v-if="message" class="success-message" role="status">{{ message }}</p>
      <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    </div>
  </section>
</template>

<style scoped>
.reconciliation-card {
  display: grid;
  gap: 0.875rem;
  margin-top: 1.125rem;
  padding: 1rem;
  border: 1px solid rgb(109 142 226 / 26%);
  border-radius: 0.875rem;
  background: linear-gradient(145deg, rgb(37 57 112 / 20%), rgb(18 24 40 / 86%));
}

.reconciliation-card[data-evidence="commit_receipt"] {
  border-color: rgb(42 211 158 / 28%);
  background: linear-gradient(145deg, rgb(24 92 72 / 18%), rgb(18 24 40 / 86%));
}

.reconciliation-card[data-evidence="query_failed"] {
  border-color: rgb(239 101 101 / 25%);
}

.reconciliation-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.reconciliation-heading h3 {
  margin-top: 0.3rem;
  color: #dce3f2;
  font-size: 0.875rem;
}

.reconciliation-status {
  flex: 0 0 auto;
  padding: 0.3rem 0.5rem;
  border: 1px solid rgb(109 142 226 / 25%);
  border-radius: 999px;
  color: #a9bcf0;
  font-size: 0.625rem;
}

.unknown-invariant,
.evidence-result,
.evidence-empty,
.compensation-panel {
  padding: 0.75rem;
  border-radius: 0.625rem;
  background: rgb(7 11 20 / 48%);
}

.unknown-invariant strong,
.evidence-result strong,
.compensation-panel strong {
  color: #d6deed;
  font-size: 0.6875rem;
}

.unknown-invariant p,
.evidence-result p,
.compensation-panel p {
  margin: 0.35rem 0 0;
  color: #8794ac;
  font-size: 0.625rem;
  line-height: 1.55;
}

.reconciliation-facts,
.receipt-facts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.5rem;
  margin: 0;
}

.reconciliation-facts div,
.receipt-facts div {
  min-width: 0;
  padding: 0.625rem;
  border-radius: 0.5625rem;
  background: rgb(8 13 24 / 50%);
}

.reconciliation-facts dt,
.reconciliation-facts dd,
.receipt-facts dt,
.receipt-facts dd {
  margin: 0;
}

.reconciliation-facts dt,
.receipt-facts dt {
  color: #626f88;
  font-size: 0.5625rem;
}

.reconciliation-facts dd,
.receipt-facts dd {
  margin-top: 0.25rem;
  overflow-wrap: anywhere;
  color: #aeb8cb;
  font-size: 0.625rem;
}

.evidence-result[data-kind="commit_receipt"] strong { color: #8edbc0; }
.evidence-result[data-kind="no_receipt"] strong { color: #e4c17c; }
.evidence-result[data-kind="query_failed"] strong { color: #eaa0a2; }
.receipt-facts { margin-top: 0.7rem; }
.evidence-result > small { display: block; margin-top: 0.65rem; color: #5f6d86; font-size: 0.5625rem; }
.evidence-empty, .reconciliation-loading { color: #738097; font-size: 0.625rem; line-height: 1.55; }

.resource-version-grid { display: grid; gap: 0.4rem; margin-top: 0.6rem; }
.resource-version-grid div { display: grid; grid-template-columns: 4.5rem minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 0.35rem; padding: 0.5rem; border: 1px solid rgb(126 145 186 / 10%); border-radius: 0.5rem; }
.resource-version-grid span { color: #73819a; font-size: 0.5625rem; }
.resource-version-grid code { overflow: hidden; color: #91a3ca; font-size: 0.5625rem; text-overflow: ellipsis; white-space: nowrap; }
.resource-version-grid b { color: #53617b; font-size: 0.625rem; }

.refresh-evidence-button { justify-self: end; }
.compensation-panel { border: 1px solid rgb(42 211 158 / 16%); }
.compensation-panel code { overflow-wrap: anywhere; color: #9fb3dd; }
.compensation-button { margin-top: 0.65rem; }
.compensation-confirm { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin-top: 0.65rem; padding: 0.625rem; border-radius: 0.5rem; background: rgb(8 13 24 / 58%); }
.compensation-confirm span { flex: 1 1 15rem; color: #aeb8cb; font-size: 0.625rem; line-height: 1.5; }
.reconciliation-feedback p { margin: 0; font-size: 0.625rem; line-height: 1.55; }
.reconciliation-feedback:empty { display: none; }
.success-message { color: #86efac; }
.error-message { color: #fca5a5; }

@media (max-width: 560px) {
  .reconciliation-facts,
  .receipt-facts { grid-template-columns: 1fr; }
  .reconciliation-heading { align-items: flex-start; flex-direction: column; }
  .resource-version-grid div { grid-template-columns: 1fr; }
  .resource-version-grid b { transform: rotate(90deg); }
  .refresh-evidence-button { width: 100%; }
}
</style>
