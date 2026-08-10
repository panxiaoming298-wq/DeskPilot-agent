<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type { Approval, ApprovalAction, ApprovalStatus } from '../types'

const props = withDefaults(
  defineProps<{
    approval: Approval | null
    loading: boolean
    activeAction: ApprovalAction | null
    message: string | null
    error: string | null
    disabled: boolean
  }>(),
  {
    approval: null,
    loading: false,
    activeAction: null,
    message: null,
    error: null,
    disabled: false,
  },
)

const emit = defineEmits<{
  resolve: [action: ApprovalAction, reason?: string]
}>()

const rejectionReason = ref('')

const pending = computed(() => props.approval?.status === 'pending')
const busy = computed(() => props.loading || props.activeAction !== null)
const actionsDisabled = computed(() => props.disabled || busy.value || !pending.value)

const statusLabel = computed(() => {
  const labels: Record<ApprovalStatus, string> = {
    pending: '等待决定',
    approved: '已同意',
    rejected: '已拒绝',
    expired: '已过期',
    cancelled: '已取消',
  }
  return props.approval ? labels[props.approval.status] : '正在载入'
})

const terminalMessage = computed(() => {
  const approval = props.approval
  if (!approval || approval.status === 'pending') return null
  const messages: Record<Exclude<ApprovalStatus, 'pending'>, string> = {
    approved: approval.consumed_at
      ? '本次授权已使用，工具不会因页面刷新而重复执行。'
      : '已仅为本次调用授权。',
    rejected: '本次操作已拒绝，不会执行工具。',
    expired: approval.decision === 'approved'
      ? '你曾同意本次操作，但授权在执行前已过期，工具不会执行。'
      : '审批已过期，旧预览不能再用于授权。',
    cancelled: approval.decision === 'approved'
      ? '你曾同意本次操作，但授权在执行前已取消，工具不会执行。'
      : '审批已随任务取消，不会执行工具。',
  }
  return messages[approval.status]
})

watch(
  () => props.approval?.approval_id,
  () => {
    rejectionReason.value = ''
  },
)

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

function resolve(action: ApprovalAction): void {
  if (actionsDisabled.value) return
  const reason = rejectionReason.value.trim()
  emit('resolve', action, action === 'reject' && reason ? reason : undefined)
}
</script>

<template>
  <section
    class="approval-card"
    aria-labelledby="approval-card-title"
    :aria-busy="busy"
    :data-status="approval?.status ?? 'loading'"
  >
    <div class="approval-heading">
      <div>
        <span class="eyebrow">EXPLICIT APPROVAL</span>
        <h3 id="approval-card-title">执行前审批</h3>
      </div>
      <span class="approval-status">{{ statusLabel }}</span>
    </div>

    <div v-if="loading && !approval" class="approval-loading" role="status">
      正在读取经策略引擎规范化的操作预览…
    </div>

    <template v-else-if="approval">
      <div class="approval-summary">
        <strong>{{ approval.title }}</strong>
        <p>{{ approval.purpose }}</p>
      </div>

      <dl class="approval-facts">
        <div>
          <dt>工具</dt>
          <dd><code>{{ approval.tool_name }}@{{ approval.tool_version }}</code></dd>
        </div>
        <div>
          <dt>风险级别</dt>
          <dd><strong class="risk-label">{{ approval.risk_level }}</strong></dd>
        </div>
        <div>
          <dt>是否可撤销</dt>
          <dd>{{ approval.reversible ? '可撤销' : '不可保证撤销' }}</dd>
        </div>
        <div>
          <dt>数据外发</dt>
          <dd>
            {{ approval.data_egress.enabled
              ? `会发送至 ${approval.data_egress.destination ?? '未标注目标'}`
              : '不会离开本机' }}
          </dd>
        </div>
      </dl>

      <div class="approval-section">
        <h4>最终资源范围</h4>
        <ul class="approval-resource-list">
          <li v-for="resource in approval.resource_scope" :key="`${resource.kind}:${resource.label}`">
            <strong>{{ resource.label }}</strong>
            <span>{{ resource.kind }} · {{ resource.operations.join(' / ') || '仅已声明能力' }}</span>
            <small v-if="resource.version">资源版本 {{ resource.version }}</small>
          </li>
        </ul>
        <p v-if="!approval.resource_scope.length" class="approval-empty">未声明可执行的资源范围。</p>
      </div>

      <div class="approval-section">
        <h4>能力与后果</h4>
        <div class="approval-capabilities">
          <span v-for="capability in approval.capabilities" :key="capability">{{ capability }}</span>
          <span v-if="!approval.capabilities.length">无额外能力</span>
        </div>
        <ul v-if="approval.consequences.length" class="approval-consequences">
          <li v-for="consequence in approval.consequences" :key="consequence">{{ consequence }}</li>
        </ul>
        <p v-else class="approval-empty">策略未标记额外后果。</p>
      </div>

      <div class="approval-validity">
        <span>仅本次有效</span>
        <time :datetime="approval.expires_at">有效至 {{ formattedTime(approval.expires_at) }}</time>
      </div>

      <template v-if="pending">
        <label class="approval-reason-field">
          <span>拒绝原因（可选）</span>
          <textarea
            v-model="rejectionReason"
            rows="2"
            maxlength="500"
            :disabled="actionsDisabled"
            placeholder="说明为什么不执行，便于审计"
          />
        </label>
        <div class="approval-actions">
          <button
            class="danger-button"
            data-testid="reject-approval"
            type="button"
            :disabled="actionsDisabled"
            @click="resolve('reject')"
          >
            {{ activeAction === 'reject' ? '正在拒绝…' : '拒绝' }}
          </button>
          <button
            class="primary-button"
            data-testid="approve-approval"
            type="button"
            :disabled="actionsDisabled"
            @click="resolve('approve')"
          >
            {{ activeAction === 'approve' ? '正在确认…' : '仅本次允许' }}
          </button>
        </div>
      </template>

      <p v-else-if="terminalMessage && !message" class="approval-terminal" role="status">
        {{ terminalMessage }}
      </p>
    </template>

    <div class="approval-feedback" aria-live="polite" aria-atomic="true">
      <p v-if="message" class="success-message" role="status">{{ message }}</p>
      <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    </div>
  </section>
</template>

<style scoped>
.approval-card {
  display: grid;
  gap: 0.875rem;
  margin-top: 1.125rem;
  padding: 1rem;
  border: 1px solid rgb(234 185 104 / 28%);
  border-radius: 0.875rem;
  background: linear-gradient(145deg, rgb(83 60 25 / 22%), rgb(18 24 40 / 86%));
}

.approval-card[data-status="approved"] {
  border-color: rgb(42 211 158 / 25%);
  background: linear-gradient(145deg, rgb(24 92 72 / 16%), rgb(18 24 40 / 86%));
}

.approval-card[data-status="rejected"],
.approval-card[data-status="expired"],
.approval-card[data-status="cancelled"] {
  border-color: rgb(239 101 101 / 24%);
  background: linear-gradient(145deg, rgb(99 35 39 / 16%), rgb(18 24 40 / 86%));
}

.approval-heading,
.approval-actions,
.approval-validity {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.approval-heading h3 {
  margin-top: 0.3rem;
  color: #dce3f2;
  font-size: 0.875rem;
}

.approval-status {
  flex: 0 0 auto;
  padding: 0.3rem 0.5rem;
  border: 1px solid rgb(234 185 104 / 25%);
  border-radius: 999px;
  color: #e8c887;
  font-size: 0.625rem;
}

.approval-summary {
  padding: 0.75rem;
  border-radius: 0.625rem;
  background: rgb(7 11 20 / 48%);
}

.approval-summary strong {
  color: #e7ebf4;
  font-size: 0.8125rem;
}

.approval-summary p {
  margin: 0.35rem 0 0;
  color: #919db4;
  font-size: 0.6875rem;
  line-height: 1.55;
}

.approval-facts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.5rem;
  margin: 0;
}

.approval-facts div {
  min-width: 0;
  padding: 0.625rem;
  border-radius: 0.5625rem;
  background: rgb(8 13 24 / 50%);
}

.approval-facts dt,
.approval-facts dd {
  margin: 0;
}

.approval-facts dt {
  color: #626f88;
  font-size: 0.5625rem;
}

.approval-facts dd {
  margin-top: 0.25rem;
  overflow-wrap: anywhere;
  color: #aeb8cb;
  font-size: 0.625rem;
  line-height: 1.45;
}

.risk-label {
  color: #e7c27c;
}

.approval-section {
  display: grid;
  gap: 0.5rem;
}

.approval-section h4 {
  margin: 0;
  color: #8592aa;
  font-size: 0.625rem;
}

.approval-resource-list,
.approval-consequences {
  display: grid;
  gap: 0.4rem;
  margin: 0;
  padding: 0;
  list-style: none;
}

.approval-resource-list li {
  display: grid;
  gap: 0.2rem;
  padding: 0.625rem;
  border: 1px solid rgb(126 145 186 / 12%);
  border-radius: 0.5625rem;
  background: rgb(9 14 25 / 44%);
}

.approval-resource-list strong {
  overflow-wrap: anywhere;
  color: #bdc7d9;
  font-size: 0.6875rem;
}

.approval-resource-list span,
.approval-resource-list small {
  color: #68758d;
  font-size: 0.5625rem;
}

.approval-capabilities {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
}

.approval-capabilities span {
  padding: 0.25rem 0.375rem;
  border-radius: 0.375rem;
  color: #91a1c1;
  background: rgb(66 85 131 / 18%);
  font-size: 0.5625rem;
}

.approval-consequences li {
  position: relative;
  padding-left: 0.875rem;
  color: #a79b8c;
  font-size: 0.625rem;
  line-height: 1.5;
}

.approval-consequences li::before {
  position: absolute;
  left: 0.2rem;
  color: #eab968;
  content: "•";
}

.approval-validity {
  padding-top: 0.7rem;
  border-top: 1px solid rgb(125 143 181 / 11%);
  color: #77849d;
  font-size: 0.5625rem;
}

.approval-validity span {
  color: #e6c481;
  font-weight: 700;
}

.approval-reason-field {
  display: grid;
  gap: 0.35rem;
  color: #7f8ca4;
  font-size: 0.625rem;
}

.approval-reason-field textarea {
  min-height: 4rem;
  padding: 0.625rem;
  font-size: 0.6875rem;
}

.approval-actions {
  justify-content: flex-end;
}

.approval-terminal,
.approval-loading,
.approval-empty,
.approval-feedback p {
  margin: 0;
  font-size: 0.625rem;
  line-height: 1.55;
}

.approval-terminal {
  padding: 0.625rem;
  border-radius: 0.5rem;
  color: #9eabc0;
  background: rgb(8 13 24 / 46%);
}

.approval-loading,
.approval-empty {
  color: #738097;
}

.approval-feedback:empty {
  display: none;
}

.success-message {
  color: #86efac;
}

.error-message {
  color: #fca5a5;
}

@media (max-width: 560px) {
  .approval-facts {
    grid-template-columns: 1fr;
  }

  .approval-actions,
  .approval-validity {
    align-items: stretch;
    flex-direction: column;
  }

  .approval-actions button {
    width: 100%;
  }
}
</style>
